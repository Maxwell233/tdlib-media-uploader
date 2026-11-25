"""Warm local Telegram media before ordered message submission."""

from copy import deepcopy
from pathlib import Path
from threading import Event, Lock
from time import monotonic

from ..core.compat import call_supported
from ..core.concurrency import (
    CancellationRequested,
    UploadPreparationExecutor,
    cancelable_sleep,
    is_cancelled,
    ordered_bounded_map,
    raise_if_cancelled,
)
from ..core.models import LogEvent, PreliminaryProgressEvent


class _PreliminaryFiles:
    def __init__(self, cancel_file, totals, report, release):
        self.cancel_file = cancel_file
        self.report = report
        self.release = release
        self.totals = totals
        self.uploaded = [0] * len(totals)
        self.ids = set()
        self.lock = Lock()
        self.closed = Event()

    def register(self, file_id):
        with self.lock:
            if not self.closed.is_set():
                self.ids.add(file_id)
                return
        self._cancel(file_id)

    def update(self, index, uploaded):
        with self.lock:
            updated = min(uploaded, self.totals[index])
            if self.closed.is_set() or updated <= self.uploaded[index]:
                return
            delta = updated - self.uploaded[index]
            self.uploaded[index] = updated
            self.report(delta)

    def _cancel(self, file_id):
        try:
            self.cancel_file(file_id)
        except Exception:
            pass  # No message has been created by preliminary upload.

    def close(self):
        with self.lock:
            self.closed.set()
            ids, self.ids = self.ids, set()
        for file_id in ids:
            self._cancel(file_id)
        self.release(self)


class PreparedRemoteAlbum:
    def __init__(self, contents, files):
        self.contents = contents
        self._files = files

    def close(self):
        self._files.close()


class PreliminaryUploader:
    """Share a file-concurrency limit across all preparing Albums in a job."""

    def __init__(self, client, max_workers, timeout):
        self.client = client
        self.max_workers = max_workers
        self.stall_timeout = timeout
        self.executor = UploadPreparationExecutor(max_workers, thread_name_prefix="tdlib-preliminary")
        self.stopped = Event()
        self.lock = Lock()
        self.batches = set()
        self.uploaded = 0
        self.total = 0
        self.started_at = None
        generation = getattr(client, "preliminary_generation", None)
        self.generation_args = {} if generation is None else {"generation": generation}

    def _cancel_file(self, file_id):
        self.client.cancel_preliminary_upload_file(file_id, **self.generation_args)

    def _release(self, uploads):
        with self.lock:
            self.batches.discard(uploads)

    def close(self):
        self.stopped.set()
        with self.lock:
            batches = tuple(self.batches)
        for uploads in batches:
            uploads.close()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def prepare(self, contents, *, context):
        def stopped():
            token = context.cancel_token
            stop_after_current = getattr(token, "stop_after_current", None)
            return (self.stopped.is_set() or is_cancelled(token)
                    or (callable(stop_after_current) and stop_after_current()))

        raise_if_cancelled(stopped)
        # TDLib accepts an inputFileId for the main Photo/Video. The linked
        # inputThumbnail explicitly requires a fresh local input for each send.
        payloads = deepcopy(tuple(dict(content) for content in contents))
        files = []
        try:
            for content in payloads:
                is_photo = content["@type"] == "inputMessagePhoto"
                field, file_type = ("photo", "fileTypePhoto") if is_photo else ("video", "fileTypeVideo")
                parent = content[field]
                source = parent[field]
                if source["@type"] == "inputFileLocal":
                    files.append((parent, field, source, file_type, Path(source["path"]).stat().st_size))
        except (KeyError, OSError, TypeError):
            return None
        if not files:
            return None

        def report(delta):
            with self.lock:
                self.uploaded += delta
                elapsed = max(0.001, monotonic() - self.started_at)
                event = PreliminaryProgressEvent(self.uploaded, self.total, self.uploaded / elapsed)
            context.event_sink.emit(event)

        totals = [row[-1] for row in files]
        uploads = _PreliminaryFiles(self._cancel_file, totals, report, self._release)
        with self.lock:
            raise_if_cancelled(stopped)
            self.batches.add(uploads)
            self.total += sum(totals)
            if self.started_at is None:
                self.started_at = monotonic()

        def upload(work):
            index, (_, _, source, file_type, size) = work
            cancelled = lambda: stopped() or uploads.closed.is_set()
            raise_if_cancelled(cancelled)
            state = call_supported(self.client.preliminary_upload_file, (source, file_type),
                                   cancel_token=cancelled, **self.generation_args)
            file_id = state["id"]
            uploads.register(file_id)
            deadline = monotonic() + self.stall_timeout
            last_uploaded = 0
            while not cancelled():
                remote = state.get("remote") or {}
                uploaded = int(remote.get("uploaded_size") or 0)
                if remote.get("is_uploading_completed"):
                    uploaded = size
                if uploaded > last_uploaded:
                    deadline = monotonic() + self.stall_timeout
                    last_uploaded = uploaded
                    uploads.update(index, uploaded)
                if uploaded >= size:
                    return file_id
                remaining = deadline - monotonic()
                if remaining <= 0 or not cancelable_sleep(
                    min(0.25, self.stall_timeout / 2, remaining), cancel_token=cancelled,
                ):
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                state = call_supported(self.client.preliminary_file_status, (file_id,), timeout=remaining,
                                       cancel_token=cancelled, **self.generation_args)
            raise_if_cancelled(cancelled)
            raise TimeoutError("TDLib 预上传在设定时限内没有新增已上传字节")

        try:
            report(0)
            ids = tuple(ordered_bounded_map(self.executor, enumerate(files), upload, self.max_workers,
                                           cancel_token=stopped))
        except CancellationRequested:
            uploads.close()
            raise
        except Exception as error:
            uploads.close()
            raise_if_cancelled(stopped)
            context.event_sink.emit(LogEvent("INFO", f"预上传不可用，本组使用直接上传：{error}", "tdlib"))
            return None

        for (parent, field, *_), file_id in zip(files, ids, strict=True):
            parent[field] = {"@type": "inputFileId", "id": file_id}
        return PreparedRemoteAlbum(payloads, uploads)
