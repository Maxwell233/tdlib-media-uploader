"""Qt-free adapters between the GUI window and the V2 upload lifecycle.

This module connects the three V2 media strategies to the durable V1.9 state,
journal and TDLib helpers. Legacy media modules still own media-specific
probing and input construction; the V2 engine owns plan traversal, send state
transitions and checkpoints.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import inspect
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..core.filesystem_legacy import stable_path

from ..contracts import UploadContext
from ..core.models import (
    AlbumPlan,
    AuthEvent,
    BatchStatus,
    LogEvent,
    MediaItem,
    ProgressEvent,
    ScanResult,
    UploadRunResult,
)
from ..media import ImageStrategy, MixedStrategy, VideoStrategy
from ..telegram.send_result import SendResult
from ..upload.engine import (
    UploadCancelled,
    UploadEngine,
)


_LEGACY_MODULES = {
    "image": "tdlib_media_uploader.media.legacy_image",
    "mixed": "tdlib_media_uploader.media.legacy_mixed",
    "video": "tdlib_media_uploader.media.legacy_video",
}
_STRATEGIES = {
    "image": ImageStrategy,
    "mixed": MixedStrategy,
    "video": VideoStrategy,
}
_STATE_DIR_NAMES = {
    "image": "IMAGE_STATE_DIR",
    "mixed": "MIXED_STATE_DIR",
    "video": "VIDEO_STATE_DIR",
}
_ROOT_NAMES = {
    "image": "IMAGE_DIR",
    "mixed": "MIXED_DIR",
    "video": "VIDEO_DIR",
}
_DEVICE_MODELS = {
    "image": "TDLib Image Album Uploader",
    "mixed": "TDLib Mixed Album Uploader",
    "video": "TDLib Video Album Uploader",
}
_MISSING = object()


class V2IntegrationUnavailable(ImportError):
    """Raised when the GUI must use its dependency-free preview fallback."""


def _call_supported(function: Callable[..., Any], args: Sequence[Any] = (), **kwargs: Any) -> Any:
    """Call a collaborator while tolerating small test/legacy signatures."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)

    parameters = signature.parameters
    positional = list(args)
    accepted: dict[str, Any] = {}
    accepts_any_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    for name, value in kwargs.items():
        parameter = parameters.get(name)
        if parameter is not None and parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        elif parameter is not None or accepts_any_keyword:
            accepted[name] = value
    return function(*positional, **accepted)


def _normalize_kind(kind: str) -> str:
    value = str(kind).strip().lower()
    if value not in _LEGACY_MODULES:
        raise ValueError(f"未知媒体类型：{kind}")
    return value


def _load_legacy(kind: str) -> Any:
    try:
        return importlib.import_module(_LEGACY_MODULES[_normalize_kind(kind)])
    except (ImportError, ModuleNotFoundError) as error:
        raise V2IntegrationUnavailable(str(error)) from error


def _state_directory(kind: str, runtime_paths: Any | None) -> Path | None:
    if runtime_paths is None:
        return None
    value = getattr(runtime_paths, _STATE_DIR_NAMES[_normalize_kind(kind)], None)
    return Path(value) if value is not None else None


def _new_state(legacy: Any, target: Mapping[str, Any]) -> Any:
    state_type = getattr(legacy, "UploadState", None)
    if not callable(state_type):
        raise TypeError("legacy uploader 缺少 UploadState()")
    try:
        return state_type(target=dict(target or {}))
    except TypeError:
        # Lightweight embedding fakes often preserve the original no-arg
        # constructor.  The production facade accepts the target keyword.
        return state_type()


@contextmanager
def _legacy_root_scope(legacy: Any, config: Any, kind: str, root: Path):
    """Align the legacy state constructor with the explicit V2 source root."""

    owner = getattr(legacy, "cfg", None) or config
    attribute = _ROOT_NAMES[_normalize_kind(kind)]
    if not hasattr(owner, attribute) and hasattr(legacy, attribute):
        owner = legacy
    previous = getattr(owner, attribute, _MISSING)
    try:
        if previous is not _MISSING:
            setattr(owner, attribute, Path(root))
        yield
    finally:
        if previous is not _MISSING:
            setattr(owner, attribute, previous)


class GuiCancelToken:
    """Expose the shared worker event through the V2 cancellation protocol."""

    def __init__(self, event: threading.Event | None = None):
        self.event = event or threading.Event()

    def is_cancelled(self) -> bool:
        return self.event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise UploadCancelled("任务已取消")


class GuiEventSink:
    """Translate GUI-free V2 events into legacy GUI callback methods."""

    def __init__(
        self,
        ui: Any | None = None,
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        kind: str = "",
    ):
        self.ui = ui
        self.progress_callback = progress_callback
        self.kind = str(kind).strip().lower()

    def _ui_message(self, level: str, message: str) -> None:
        if self.ui is None:
            return
        method_name = {
            "DEBUG": "log",
            "INFO": "info",
            "SUCCESS": "success",
            "WARNING": "warning",
            "ERROR": "error",
        }.get(str(level).upper(), "info")
        method = getattr(self.ui, method_name, None)
        if callable(method):
            method(message)

    def emit(self, event: Any) -> None:
        if isinstance(event, LogEvent):
            self._ui_message(event.level, str(event.message))
            return
        if isinstance(event, AuthEvent):
            if event.message:
                self._ui_message("INFO", event.message)
            return
        if not isinstance(event, ProgressEvent):
            return

        payload = {
            "phase": event.phase,
            "completed": int(event.completed),
            "total": int(event.total),
            "path": event.path,
            "message": event.message,
        }
        if self.progress_callback is not None:
            self.progress_callback(payload)
        if self.ui is not None and event.phase in {"preflight", "scan"}:
            total = max(0, int(event.total))
            completed = max(0, int(event.completed))
            ratio = min(completed / total, 1.0) if total else 0.0
            progress = getattr(self.ui, "progress", None)
            if callable(progress):
                progress(
                    kind=self.kind.upper(),
                    ratio=ratio,
                    speed=0,
                    eta=None,
                    detail=event.message,
                    album_number=0,
                    album_total=0,
                    done_files=completed,
                    total_files=total,
                    done_bytes=0,
                    total_bytes=0,
                )


@dataclass(frozen=True, slots=True)
class V2ScanBundle:
    """The V2 scan objects needed to render the migration-period preview."""

    kind: str
    source_root: Path
    target: Mapping[str, Any]
    strategy: Any
    legacy: Any
    state: Any
    scan_result: ScanResult
    plans: tuple[AlbumPlan, ...]
    missing: tuple[Path, ...] = ()


def _strategy(kind: str, legacy: Any, config: Any, source_root: Path) -> Any:
    strategy_type = _STRATEGIES[_normalize_kind(kind)]
    return strategy_type(
        legacy_module=legacy,
        config=config,
        source_root=source_root,
    )


def scan_v2(
    kind: str,
    *,
    source_root: Path,
    target: Mapping[str, Any],
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    config: Any | None = None,
    runtime_paths: Any | None = None,
) -> V2ScanBundle:
    """Run one V2 strategy scan and construct complete Album plans."""

    normalized = _normalize_kind(kind)
    if config is None:
        config = importlib.import_module("tdlib_media_uploader.config.loader")
    activate = getattr(config, "activate_target", None)
    if callable(activate):
        activate(normalized)

    legacy = _load_legacy(normalized)
    state_dir = _state_directory(normalized, runtime_paths)
    if state_dir is not None:
        legacy.STATE_DIR = state_dir
    root = Path(source_root)
    strategy = _strategy(normalized, legacy, config, root)
    token = GuiCancelToken(cancel_event)
    sink = GuiEventSink(progress_callback=progress_callback, kind=normalized)
    with _legacy_root_scope(legacy, config, normalized, root):
        state = _new_state(legacy, target)
    try:
        scan_result = strategy.scan(
            root,
            cancel_token=token,
            event_sink=sink,
        )
    except BaseException as error:
        if token.is_cancelled() or bool(getattr(error, "cancelled", False)):
            scan_result = ScanResult(cancelled=True)
        else:
            raise

    if scan_result.cancelled or token.is_cancelled():
        return V2ScanBundle(
            normalized,
            root,
            dict(target or {}),
            strategy,
            legacy,
            state,
            ScanResult(cancelled=True),
            (),
        )

    plans = tuple(
        strategy.build_plans(
            scan_result,
            target=dict(target or {}),
            state=state,
        )
    )
    missing = tuple(Path(value) for value in getattr(strategy, "last_missing", ()) or ())
    return V2ScanBundle(
        normalized,
        root,
        dict(target or {}),
        strategy,
        legacy,
        state,
        scan_result,
        plans,
        missing,
    )


def _legacy_item(item: MediaItem) -> dict[str, Any]:
    metadata = dict(item.metadata) if isinstance(item.metadata, Mapping) else {}
    metadata.update(
        {
            "path": item.path,
            "source_root": item.source_root,
            "media_kind": item.media_kind,
            "scan_size": item.snapshot.size,
            "scan_mtime_ns": item.snapshot.mtime_ns,
            "capture_time": item.capture_time,
            "group_name": item.group_name,
            "month_key": item.month_key,
            "date_tag": item.date_tag,
            "fallback": item.fallback,
        }
    )
    return metadata


def legacy_item(item: MediaItem) -> dict[str, Any]:
    """Expose the V2-to-legacy item translation for GUI upload adapters."""

    if not isinstance(item, MediaItem):
        raise TypeError("legacy_item() 需要 MediaItem")
    return _legacy_item(item)


class _ConfirmedStateAdapter:
    """Checkpoint adapter that cleans temporary media only after persistence."""

    def __init__(self, state: Any, cleanup: Callable[[Sequence[Any]], None] | None = None):
        self.state = state
        self.cleanup = cleanup
        self.path = getattr(state, "path", "")

    def is_completed(self, item: Any) -> bool:
        return bool(self.state.is_completed(item))

    def mark_album_completed(self, items: Sequence[Any], message_ids=None) -> None:
        self.state.mark_album_completed(items, message_ids)
        if self.cleanup is not None:
            try:
                self.cleanup(items)
            except Exception:
                # The durable source checkpoint is already safe.  A stale
                # temporary artifact can be pruned on the next run and must
                # not leave a confirmed Telegram Album unreconciled.
                pass


class GuiUploadProgress:
    """TDLib file-update progress renderer for V2 Album sends."""

    def __init__(self, kind: str, all_items: Sequence[Any] = (), completed_paths=(), ui: Any = None):
        self.kind = _normalize_kind(kind)
        self.ui = ui
        self.sizes: dict[str, int] = {}
        self.paths: dict[str, Path] = {}
        for item in all_items or ():
            self._register_item(item)
        self.total_files = len(self.sizes)
        self.total_bytes = sum(self.sizes.values())
        completed = {stable_path(value) for value in (completed_paths or ())}
        self.completed_keys = {key for key in self.sizes if key in completed}
        self.completed_files = len(self.completed_keys)
        self.completed_bytes = sum(self.sizes[key] for key in self.completed_keys)
        self.current_keys: dict[str, str] = {}
        self.current_uploaded: dict[str, int] = {}
        self.file_id_to_key: dict[int, str] = {}
        self.album_number = 0
        self.album_total = 0
        self.album_label = ""
        self.samples = deque()
        self.last_draw = 0.0
        self.lock = threading.RLock()

    @staticmethod
    def _path(value: Any) -> Path | None:
        if isinstance(value, MediaItem):
            return Path(value.path)
        if isinstance(value, Mapping):
            value = value.get("path")
        if value is None:
            return None
        return Path(value)

    @staticmethod
    def _size(value: Any, path: Path) -> int:
        if isinstance(value, MediaItem):
            return int(value.snapshot.size)
        if isinstance(value, Mapping):
            raw = value.get("scan_size", value.get("size"))
            if raw is not None:
                try:
                    return max(0, int(raw))
                except (TypeError, ValueError):
                    pass
        try:
            return max(0, int(path.stat().st_size))
        except OSError:
            return 0

    def _register_item(self, value: Any) -> str | None:
        path = self._path(value)
        if path is None:
            return None
        key = stable_path(path)
        if key not in self.sizes:
            self.sizes[key] = self._size(value, path)
            self.paths[key] = path
            self.total_files = len(self.sizes)
            self.total_bytes = sum(self.sizes.values())
        return key

    def begin_album(self, items: Sequence[Any], plan: AlbumPlan, total_albums: int) -> None:
        with self.lock:
            keys = [self._register_item(item) for item in items]
            self.current_keys = {
                stable_path(path): stable_path(path)
                for path in (self._path(item) for item in items)
                if path is not None
            }
            self.current_uploaded = {
                key: 0 for key in keys if key is not None
            }
            self.file_id_to_key = {}
            self.album_number = int(plan.number)
            self.album_total = int(total_albums)
            self.album_label = str(plan.group_label or "")
            self.samples.clear()
            self.last_draw = 0.0
        album = getattr(self.ui, "album", None)
        if callable(album):
            rows = []
            for item in items:
                path = self._path(item)
                if path is None:
                    continue
                media = item.media_kind if isinstance(item, MediaItem) else (
                    item.get("media_kind", self.kind) if isinstance(item, Mapping) else self.kind
                )
                rows.append(f"{media} · {self.sizes.get(stable_path(path), 0)} B · {path}")
            album(
                kind=self.kind.upper(),
                title=f"{self.album_label} · Album {plan.number} · {self.album_total}",
                subtitle=f"{len(items)} 个媒体 · Caption={plan.caption or '无'}",
                rows=rows,
            )
        self.draw(force=True)

    @staticmethod
    def _media_file(message: Any) -> Mapping[str, Any] | None:
        if not isinstance(message, Mapping):
            return None
        content = message.get("content") or {}
        content_type = content.get("@type")
        if content_type == "messageVideo":
            return content.get("video", {}).get("video")
        if content_type == "messagePhoto":
            sizes = content.get("photo", {}).get("sizes", [])
            if sizes:
                return max(
                    sizes,
                    key=lambda value: int(value.get("width", 0)) * int(value.get("height", 0)),
                ).get("photo")
        return None

    def register_messages(self, messages, items):
        with self.lock:
            for message, item in zip(messages or (), items or ()):
                path = self._path(item)
                file_obj = self._media_file(message)
                if path is None or file_obj is None:
                    continue
                key = self._register_item(item)
                if key is None:
                    continue
                file_id = file_obj.get("id")
                if file_id is not None:
                    try:
                        self.file_id_to_key[int(file_id)] = key
                    except (TypeError, ValueError):
                        pass
                self._apply_file(file_obj)

    def _apply_file(self, file_obj: Mapping[str, Any]) -> None:
        local_path = (file_obj.get("local") or {}).get("path", "")
        key = stable_path(local_path) if local_path else None
        if key not in self.current_keys:
            try:
                key = self.file_id_to_key.get(int(file_obj.get("id")))
            except (TypeError, ValueError):
                key = None
        if key is None:
            return
        remote = file_obj.get("remote") or {}
        if remote.get("is_uploading_completed"):
            uploaded = self.sizes.get(key, 0)
        else:
            try:
                uploaded = int(remote.get("uploaded_size", 0) or 0)
            except (TypeError, ValueError):
                uploaded = 0
        self.current_uploaded[key] = max(
            self.current_uploaded.get(key, 0),
            min(uploaded, self.sizes.get(key, uploaded)),
        )

    def handle_update(self, update: Mapping[str, Any]) -> None:
        if not isinstance(update, Mapping) or update.get("@type") != "updateFile":
            return
        file_obj = update.get("file")
        if not isinstance(file_obj, Mapping):
            return
        with self.lock:
            self._apply_file(file_obj)
        self.draw()

    def _speed(self, uploaded: int) -> float:
        now = time.monotonic()
        self.samples.append((now, uploaded))
        while len(self.samples) > 2 and now - self.samples[0][0] > 3:
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        old_time, old_bytes = self.samples[0]
        duration = now - old_time
        return max(0.0, (uploaded - old_bytes) / duration) if duration > 0 else 0.0

    def draw(self, force: bool = False) -> None:
        if self.ui is None:
            return
        with self.lock:
            now = time.monotonic()
            if not force and now - self.last_draw < 0.15:
                return
            self.last_draw = now
            current = sum(self.current_uploaded.values())
            done_bytes = self.completed_bytes + current
            ratio = min(done_bytes / self.total_bytes, 1.0) if self.total_bytes else 0.0
            speed = self._speed(done_bytes)
            eta = (self.total_bytes - done_bytes) / speed if speed > 0 else None
            done_files = self.completed_files
            total_files = self.total_files
            payload = {
                "kind": self.kind.upper(),
                "ratio": ratio,
                "speed": speed,
                "eta": eta,
                "detail": self.album_label,
                "album_number": self.album_number,
                "album_total": self.album_total,
                "done_files": done_files,
                "total_files": total_files,
                "done_bytes": done_bytes,
                "total_bytes": self.total_bytes,
            }
        progress = getattr(self.ui, "progress", None)
        if callable(progress):
            progress(**payload)

    def finish_album(self, items: Sequence[Any]) -> None:
        with self.lock:
            keys = [self._register_item(item) for item in items]
            for key in keys:
                if key is None or key in self.completed_keys:
                    continue
                self.completed_keys.add(key)
                self.completed_files += 1
                self.completed_bytes += self.sizes.get(key, 0)
            self.current_keys = {}
            self.current_uploaded = {}
            self.file_id_to_key = {}
        self.draw(force=True)
        finish = getattr(self.ui, "finish", None)
        if callable(finish):
            finish()


class TDLibSender:
    """V2 sender adapter that delegates one already-built Album to TDLib."""

    def __init__(
        self,
        client: Any,
        progress: GuiUploadProgress,
        ui: Any,
        *,
        total_albums: int = 0,
    ):
        self.client = client
        self.progress = progress
        self.ui = ui
        self.total_albums = int(total_albums)

    def send_contents(
        self,
        contents,
        *,
        target=None,
        plan: AlbumPlan | None = None,
        context=None,
        caption_limit=None,
        timeout=None,
    ) -> SendResult:
        del target, context, caption_limit, timeout
        if plan is None:
            raise TypeError("TDLibSender 需要 AlbumPlan")
        items = tuple(plan.pending_items)
        self.progress.begin_album(items, plan, self.total_albums)
        legacy_items = tuple(_legacy_item(item) for item in items)
        try:
            value = _call_supported(
                self.client.send_contents,
                (tuple(contents),),
                progress=self.progress,
                items=legacy_items,
            )
            if isinstance(value, SendResult):
                result = value
            elif isinstance(value, Mapping):
                status = value.get("status", BatchStatus.CONFIRMED)
                try:
                    status = BatchStatus(str(status).upper())
                except ValueError:
                    status = BatchStatus.UNKNOWN
                result = SendResult(
                    status,
                    tuple(int(value) for value in value.get("succeeded", value.get("succeeded_ids", ())) or ()),
                    tuple(int(value) for value in value.get("failed", value.get("failed_ids", ())) or ()),
                    tuple(int(value) for value in value.get("pending", value.get("pending_ids", ())) or ()),
                    str(value.get("error")) if value.get("error") else None,
                )
            else:
                result = SendResult(
                    BatchStatus.CONFIRMED,
                    succeeded_ids=tuple(int(value) for value in (value or ())),
                )
            if result.status is BatchStatus.CONFIRMED:
                self.progress.finish_album(items)
            return result
        except BaseException as error:
            # TDLib request errors are observed before Telegram accepts the
            # request; tell UploadEngine so it can persist FAILED instead of
            # conservatively escalating a definite request rejection to
            # UNKNOWN.  Cancellation/timeouts remain ambiguous by design.
            if type(error).__name__ == "TDLibError":
                try:
                    error.submitted = False
                except Exception:
                    pass
            finish = getattr(self.ui, "finish", None)
            if callable(finish):
                finish()
            raise


def _premium_checker(strategy: Any, client: Any):
    """Add the legacy Premium rule to the generic per-item preflight hook."""

    def check(item: MediaItem, *, context=None):
        metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
        if item.media_kind == "video" and metadata.get("requires_premium"):
            if getattr(client, "is_premium", None) is not True:
                return {
                    "status": "FAILED",
                    "reason": "视频超过约 2 GB，需要 Telegram Premium 才能上传",
                }
        checker = getattr(strategy, "preflight_item", None)
        if callable(checker):
            return checker(item, context=context)
        return {"status": "READY"}

    return check


def _cleanup_for(legacy: Any, kind: str) -> Callable[[Sequence[Any]], None] | None:
    cleanup = getattr(legacy, "cleanup_confirmed_staging", None)
    if not callable(cleanup):
        return None
    return lambda items: cleanup(
        [
            item.path
            if isinstance(item, MediaItem)
            else item.get("path")
            if isinstance(item, Mapping)
            else item
            for item in items
        ]
    )


def run_v2_upload(
    kind: str,
    *,
    ui: Any,
    source_root: Path,
    target: Mapping[str, Any],
    cancel_event: threading.Event | None = None,
    preview_result: Mapping[str, Any] | None = None,
    config: Any | None = None,
    runtime_paths: Any | None = None,
    client_factory: Callable[..., Any] | None = None,
    engine_factory: Callable[..., Any] | None = None,
) -> UploadRunResult:
    """Run one GUI upload through ``UploadEngine`` and a legacy TDLib client."""

    normalized = _normalize_kind(kind)
    if config is None:
        config = importlib.import_module("tdlib_media_uploader.config.loader")
    activate = getattr(config, "activate_target", None)
    if callable(activate):
        activate(normalized)
    legacy = _load_legacy(normalized)
    state_dir = _state_directory(normalized, runtime_paths)
    if state_dir is not None:
        legacy.STATE_DIR = state_dir
    root = Path(source_root)
    strategy = _strategy(normalized, legacy, config, root)
    token = GuiCancelToken(cancel_event)
    sink = GuiEventSink(ui, kind=normalized)

    raw_items = tuple((preview_result or {}).get("items", ()) or ())
    completed_paths = tuple((preview_result or {}).get("completed_paths", ()) or ())
    progress = GuiUploadProgress(normalized, raw_items, completed_paths, ui)

    if client_factory is None:
        from ..telegram.tdlib_common import TDJsonClient  # noqa: PLC0415

        client_factory = TDJsonClient
    client = _call_supported(
        client_factory,
        (ui, _DEVICE_MODELS[normalized]),
    )
    callback_added = False
    try:
        login = getattr(client, "login", None)
        if callable(login):
            login()
        refresh = getattr(client, "refresh_account_limits", None)
        if callable(refresh):
            refresh()
        fast_options = getattr(client, "set_fast_options", None)
        if callable(fast_options):
            fast_options()
        validate_target = getattr(client, "validate_target", None)
        if callable(validate_target):
            validate_target()

        add_callback = getattr(client, "add_update_callback", None)
        if callable(add_callback):
            add_callback(progress.handle_update)
            callback_added = True

        with _legacy_root_scope(legacy, config, normalized, root):
            state = _new_state(legacy, target)
        state_adapter = _ConfirmedStateAdapter(
            state,
            _cleanup_for(legacy, normalized),
        )
        from ..core.upload_journal import InflightJournal  # noqa: PLC0415

        journal_root = getattr(runtime_paths, "UPLOAD_INFLIGHT_DIR", None) if runtime_paths is not None else None
        journal = InflightJournal(Path(journal_root) if journal_root is not None else None)
        caption_limit = getattr(client, "caption_length_limit", None) or 1024
        sender = TDLibSender(
            client,
            progress,
            ui,
            total_albums=int((preview_result or {}).get("album_count", 0) or 0),
        )
        context = UploadContext(
            source_root=root,
            target=dict(target or {}),
            cancel_token=token,
            event_sink=sink,
            kind=normalized,
            state=state_adapter,
            journal=journal,
            sender=sender,
            preflight=_premium_checker(strategy, client),
            metadata={
                "caption_limit": int(caption_limit),
                "caption_length_limit": int(caption_limit),
            },
        )
        if engine_factory is None:
            engine_factory = UploadEngine
        engine = engine_factory(
            sender=sender,
            state=state_adapter,
            journal=journal,
            preflight=context.preflight,
        )
        return engine.run(
            strategy,
            source_root=root,
            target=dict(target or {}),
            cancel_token=token,
            event_sink=sink,
            context=context,
        )
    finally:
        if callback_added:
            remove_callback = getattr(client, "remove_update_callback", None)
            if callable(remove_callback):
                remove_callback(progress.handle_update)
        close = getattr(client, "close", None)
        if callable(close):
            close()
        for method_name in ("cleanup_staging_cache", "cleanup_compressed_images"):
            method = getattr(legacy, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
        if normalized == "mixed":
            try:
                image_legacy = importlib.import_module("tdlib_media_uploader.media.legacy_image")
                cleanup_compressed = getattr(image_legacy, "cleanup_compressed_images", None)
                if callable(cleanup_compressed):
                    cleanup_compressed()
            except Exception:
                pass


__all__ = [
    "GuiCancelToken",
    "GuiEventSink",
    "GuiUploadProgress",
    "TDLibSender",
    "V2IntegrationUnavailable",
    "V2ScanBundle",
    "legacy_item",
    "run_v2_upload",
    "scan_v2",
]
