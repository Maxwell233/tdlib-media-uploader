"""Behavioral regressions from the repository review; no Telegram account needed."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from tdlib_media_uploader.core.models import AlbumPlan, FileSnapshot, MediaItem, ScanResult
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.telegram import tdlib_common
from tdlib_media_uploader.processes.runner import run_cancellable_process


def item_for(path, root):
    stat = path.stat()
    return MediaItem(path=path, source_root=root, media_kind="image",
        snapshot=FileSnapshot(str(path), stat.st_size, stat.st_mtime_ns))


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.kwargs = dict(kind="image", source_root=self.root, state_dir=self.root / "state")
        self.items = [{"path": self.root / f"{i}.jpg", "size": 10, "mtime_ns": 1} for i in range(3)]

    def test_existing_json_is_read_updated_and_reset_without_migration(self):
        state = UploadState(**self.kwargs)
        self.assertEqual(state.path.suffix, ".json")
        state.mark_album_completed(self.items[:1], [101])
        reopened = UploadState(**self.kwargs)
        self.assertTrue(reopened.is_completed(self.items[0]))
        reopened.mark_album_completed(self.items[1:2], [202])
        saved = json.loads(state.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["completed"][state.signature(self.items[1])]["message_id"], 202)
        self.assertEqual(list(state.state_dir.iterdir()), [state.path])
        reset = UploadState(**self.kwargs, reset=True)
        self.assertFalse(reset.data["completed"])
        self.assertFalse(UploadState(**self.kwargs).data["completed"])

    def test_album_failure_preserves_disk_and_memory_and_allows_retry(self):
        for operation in ("json.dump", "os.fsync", "os.replace"):
            with self.subTest(operation=operation):
                state = UploadState(**self.kwargs, reset=True)
                state.mark_album_completed(self.items[:1], [101])
                original = state.path.read_bytes()
                memory = json.dumps(state.data, sort_keys=True)
                with patch(f"tdlib_media_uploader.core.upload_state.{operation}", side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        state.mark_album_completed(self.items[1:], [202, 303])
                self.assertEqual(state.path.read_bytes(), original)
                self.assertEqual(json.dumps(state.data, sort_keys=True), memory)
                self.assertFalse(state.path.with_suffix(".json.tmp").exists())
                for actual in (state, UploadState(**self.kwargs)):
                    self.assertTrue(actual.is_completed(self.items[0]))
                    self.assertFalse(actual.is_completed(self.items[1]))
                    self.assertFalse(actual.is_completed(self.items[2]))
                state.mark_album_completed(self.items[1:], [202, 303])
                self.assertEqual(len(UploadState(**self.kwargs).data["completed"]), 3)

    def test_thousand_files_round_trip_in_batches(self):
        state = UploadState(**self.kwargs)
        items = [{"path": self.root / f"file-{i}.jpg", "size": 10, "mtime_ns": 1} for i in range(1000)]
        for start in range(0, len(items), 10):
            state.mark_album_completed(items[start:start + 10], range(start + 1, start + 11))
        reopened = UploadState(**self.kwargs)
        self.assertEqual(len(reopened.data["completed"]), 1000)
        for index, item in enumerate(items):
            self.assertTrue(reopened.is_completed(item))
            self.assertEqual(reopened.data["completed"][reopened.signature(item)]["message_id"], index + 1)

    def test_invalid_json_is_preserved_and_can_be_retried(self):
        state = UploadState(**self.kwargs)
        for broken in ("{broken", json.dumps({"version": 2, "completed": []})):
            with self.subTest(broken=broken):
                state.path.write_text(broken, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    UploadState(**self.kwargs)
                self.assertEqual(state.path.read_text(), broken)
        state.path.write_text(json.dumps(state.data), encoding="utf-8")
        self.assertEqual(UploadState(**self.kwargs).data["completed"], {})

    def test_failed_reset_preserves_existing_checkpoint(self):
        state = UploadState(**self.kwargs)
        state.mark_album_completed(self.items[:1], [101])
        original = state.path.read_bytes()
        with patch("tdlib_media_uploader.core.upload_state.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                UploadState(**self.kwargs, reset=True)
        self.assertEqual(state.path.read_bytes(), original)
        self.assertTrue(UploadState(**self.kwargs).is_completed(self.items[0]))


class DeliveryTests(unittest.TestCase):
    def client(self):
        client = object.__new__(tdlib_common.TDJsonClient)
        client.cancel_event = threading.Event()
        client.stop_event = threading.Event()
        client.closed_event = threading.Event()
        client.send_condition = threading.Condition()
        client.send_events = {}
        client.close_lock = threading.Lock()
        client.close_sent = False
        client.send_raw = Mock()
        client.receiver_thread = Mock()
        client.receiver_thread.is_alive.return_value = False
        return client

    def test_reverse_confirmations_and_immediate_success_keep_original_order(self):
        client = self.client()
        messages = [{"id": -1, "sending_state": {"@type": "messageSendingStatePending"}},
                    {"id": 202},
                    {"id": -3, "sending_state": {"@type": "messageSendingStatePending"}}]
        client.send_events[-3] = ("success", {"message": {"id": 303}})
        entered = threading.Event()
        original_wait = client.send_condition.wait
        def wait(timeout=None):
            entered.set()
            return original_wait(timeout)
        client.send_condition.wait = wait
        results = []
        thread = threading.Thread(target=lambda: results.append(client.wait_for_send_results(messages, timeout=2)))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with client.send_condition:
                client.send_events[-1] = ("success", {"message": {"id": 101}})
                client.send_condition.notify_all()
        finally:
            thread.join(3)
        self.assertEqual(results[0]["succeeded"], [101, 202, 303])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = UploadState(kind="image", source_root=root, state_dir=root / "state")
            state.mark_album_completed([{"path": root / f"{i}.jpg", "size": 1, "mtime_ns": 1} for i in range(3)], results[0]["succeeded"])
            self.assertEqual({r["relative_path"]: r["message_id"] for r in UploadState(kind="image", source_root=root, state_dir=root / "state").data["completed"].values()}, {"0.jpg": 101, "1.jpg": 202, "2.jpg": 303})

    def test_missing_message_id_is_unknown_not_a_definite_failure(self):
        client = self.client()
        with self.assertRaises(tdlib_common.SendResultUnknown):
            client.wait_for_send_results([{"sending_state": {"@type": "messageSendingStatePending"}}], timeout=0)

    def test_close_timeout_keeps_receiver_available_until_actual_closed_update(self):
        client = self.client()
        client.client_id = 42
        client.auth_queue = queue.Queue()
        client.pending = {}
        client.pending_lock = threading.Lock()
        client.update_callbacks = []
        client.ui = SimpleNamespace(warning=Mock())
        with patch.object(tdlib_common.TDJsonClient, "_active_client", client):
            with self.assertRaises(TimeoutError):
                client.close(timeout=0)
            self.assertFalse(client.stop_event.is_set())
            self.assertIs(tdlib_common.TDJsonClient.unclosed_instance(), client)
            update = json.dumps({"@type": "updateAuthorizationState", "@client_id": 42,
                "authorization_state": {"@type": "authorizationStateClosed"}})
            with patch.object(tdlib_common.tdjson, "td_receive", return_value=update):
                client._receiver_loop()
            self.assertTrue(client.closed_event.is_set())
            self.assertIsNone(tdlib_common.TDJsonClient.unclosed_instance())
            client.close(timeout=0)
            client.close(timeout=0)
            client.send_raw.assert_called_once_with({"@type": "close"})

    def test_close_waits_for_delayed_closed(self):
        client = self.client()
        requested = threading.Event()
        client.send_raw.side_effect = lambda _: requested.set()
        finished = threading.Event()
        def close():
            client.close(timeout=2)
            finished.set()
        thread = threading.Thread(target=close)
        thread.start()
        try:
            self.assertTrue(requested.wait(1))
            self.assertFalse(finished.is_set())
            self.assertFalse(client.stop_event.is_set())
        finally:
            client.closed_event.set()
            thread.join(3)
        self.assertTrue(finished.is_set())


class MediaTests(unittest.TestCase):
    def test_small_transparent_inputs_composite_configured_background(self):
        from tdlib_media_uploader.media import image_prepare
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(image_prepare, "IMAGE_COMPRESSION_CACHE_DIR", root / "cache"):
                for mode, suffix in (("RGBA", "png"), ("P", "png"), ("RGBA", "webp")):
                    for background, expected in (("#00FF00", (0, 255, 0)), ("#FFFFFF", (255, 255, 255))):
                        with self.subTest(mode=mode, suffix=suffix, background=background):
                            source = root / f"{mode}.{suffix}"
                            img = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
                            if mode == "P":
                                img = Image.new("P", (64, 64), 0)
                                img.info["transparency"] = 0
                            img.save(source)
                            prepared = image_prepare.prepare_image_for_telegram(source, background=background)
                            self.assertTrue(prepared.transformed)
                            with Image.open(prepared.upload_path) as output:
                                self.assertEqual(output.mode, "RGB")
                                self.assertTrue(all(abs(a-b) <= 2 for a, b in zip(output.getpixel((0, 0)), expected)))

    def test_image_probe_cache_reuses_full_info_but_invalidates_changed_source(self):
        from tdlib_media_uploader.media import legacy_image
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "photo.jpg"
            Image.new("RGB", (64, 64), "red").save(source)
            with patch.object(legacy_image, "probe_image", wraps=legacy_image.probe_image) as probe:
                first = legacy_image.cached_image_probe(source)
                self.assertEqual(legacy_image.image_info(source), (64, 64))
                self.assertIs(legacy_image.cached_image_probe(source), first)
                self.assertEqual(probe.call_count, 1)
                Image.new("RGB", (320, 240), "blue").save(source)
                self.assertEqual(legacy_image.image_info(source), (320, 240))
                self.assertEqual(probe.call_count, 2)

    def test_prepared_artifact_and_source_are_both_validated(self):
        from tdlib_media_uploader.upload.prepared_media import capture_prepared_media, SourceChanged
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"; source.write_bytes(b"source")
            output = root / "prepared.jpg"; output.write_bytes(b"prepared")
            item = item_for(source, root)
            contents = [{"photo": {"@type": "inputFileLocal", "path": str(output)}}]
            prepared = capture_prepared_media((item,), contents)
            output.write_bytes(b"changed")
            with self.assertRaises(SourceChanged):
                prepared.validate()
            prepared = capture_prepared_media((item,), contents)
            source.unlink()
            with self.assertRaises(SourceChanged):
                prepared.validate()

    def test_album_source_change_during_real_image_preparation_never_submits(self):
        from tdlib_media_uploader.media import legacy_image
        from tdlib_media_uploader.media.image import ImageStrategy
        from tdlib_media_uploader.core.concurrency import CancellationToken
        from tdlib_media_uploader.upload.engine import UploadContext, UploadEngine
        from tdlib_media_uploader.config.snapshot import snapshot_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "a.jpg", root / "b.jpg"]
            for path in paths:
                Image.new("RGB", (64, 64), "red").save(path)
            items = tuple(item_for(path, root) for path in paths)
            plan = AlbumPlan(key="source-change", kind="image", source_root=root,
                group_label="1", number=1, items=items, pending_items=items, caption="")
            state = UploadState(kind="image", source_root=root, state_dir=root / "state")
            real = legacy_image.input_photo
            def mutate_previous(path, *args, **kwargs):
                result = real(path, *args, **kwargs)
                if path == paths[1]:
                    Image.new("RGB", (320, 240), "blue").save(paths[0])
                return result
            config = snapshot_config(legacy_image.cfg)
            config.STAGING_MODE = "off"
            config.STAGING_ENABLED = False
            sender = Mock()
            with patch.object(legacy_image, "cfg", config), patch.object(legacy_image, "input_photo", mutate_previous):
                strategy = ImageStrategy(legacy_module=legacy_image, source_root=root)
                context = UploadContext(source_root=root, target={}, cancel_token=CancellationToken(),
                    event_sink=SimpleNamespace(emit=lambda _: None), kind="image", scan_result=ScanResult(items=items),
                    plans=(plan,), state=state, sender=sender, preflight=strategy.preflight_item)
                result = UploadEngine().run(strategy, context=context)
            self.assertEqual(result.status, "PARTIAL")
            sender.send_contents.assert_not_called()
            self.assertFalse(state.data["completed"])
            self.assertEqual(len(result.deferred_items), 2)

    def test_preview_reads_one_caption_file_for_one_thousand_albums(self):
        from tdlib_media_uploader.core import album
        from tdlib_media_uploader.gui import models
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captions = root / "captions.json"
            captions.write_text(json.dumps({f"p{i}": {"custom_text": str(i)} for i in range(1000)}))
            items = tuple(MediaItem(path=root / f"{i}.jpg", source_root=root, media_kind="image",
                snapshot=FileSnapshot(str(root / f"{i}.jpg"), 10, 1)) for i in range(1000))
            plans = tuple(AlbumPlan(key=f"p{i}", kind="image", source_root=root, group_label=str(i), number=i+1,
                items=(item,), pending_items=(item,), caption="caption") for i, item in enumerate(items))
            bundle = SimpleNamespace(kind="image", source_root=root, target={}, state=SimpleNamespace(path=""),
                legacy=SimpleNamespace(), scan_result=ScanResult(items=items), plans=plans, missing=())
            original = Path.read_text
            reads = []
            def read(path, *args, **kwargs):
                if path == captions:
                    reads.append(path)
                return original(path, *args, **kwargs)
            with patch.object(album, "path_for", return_value=captions), patch.object(Path, "read_text", read):
                result = models.scan_result(bundle)
            self.assertEqual(len(reads), 1)
            self.assertTrue(result)


class BoundedOutputTests(unittest.TestCase):
    def test_large_dual_stream_output_is_bounded_with_and_without_cancellation(self):
        for event in (None, threading.Event()):
            with self.subTest(cancel_event=event):
                result = run_cancellable_process([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*1000000); sys.stderr.buffer.write(b'y'*1000000)"],
                    capture_output=True, max_output_bytes=1024, cancel_event=event, timeout=5)
                self.assertEqual(result.stdout, b"x" * 1024)
                self.assertEqual(result.stderr, b"y" * 1024)

    def test_unicode_limit_is_bytes_and_does_not_split_character(self):
        result = run_cancellable_process([sys.executable, "-c", "import sys; sys.stdout.buffer.write('中文测试'.encode('utf-8'))"],
            capture_output=True, text=True, encoding="utf-8", max_output_bytes=7, timeout=5)
        self.assertEqual(result.stdout, "中文")
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 7)

    def test_limited_capture_honors_nonzero_exit(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            run_cancellable_process([sys.executable, "-c", "import sys; print('failure'); sys.exit(3)"],
                capture_output=True, max_output_bytes=3, check=True, timeout=5)
        self.assertEqual(caught.exception.output, b"fai")


class BackgroundMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_cleanup_can_stop_mid_walk_and_preserves_remaining_files(self):
        from tdlib_media_uploader.gui.cache_service import clear_cache
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one", "two"):
                (root / name).mkdir()
                (root / name / "cache.bin").write_bytes(b"cache")
            event = threading.Event()
            def progress(path):
                if Path(path) != root:
                    event.set()
            removed, errors = clear_cache(("test",), {"test": ("Test", root)}, cancel_event=event, progress=progress)
            self.assertTrue(event.is_set())
            self.assertFalse(removed)
            self.assertFalse(errors)
            self.assertEqual(len(list(root.rglob("*.bin"))), 2)

    def test_main_window_keeps_cleanup_off_ui_thread_and_blocks_conflicting_work(self):
        from tdlib_media_uploader.gui.main_window import MainWindow
        from tdlib_media_uploader.gui import cache_service
        from PySide6.QtCore import QEventLoop, QTimer
        entered = threading.Event()
        release = threading.Event()
        observed_threads = []
        def clear(*args, **kwargs):
            observed_threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return [], []
        window = MainWindow()
        loop = QEventLoop()
        try:
            with patch.object(cache_service, "clear_cache", clear), patch("tdlib_media_uploader.gui.main_window.QMessageBox.information"):
                window._finish_cache_clear(("thumb_cache",), reset_scan=False)
                worker = window._cache_clear_worker
                self.assertTrue(entered.wait(1))
                self.assertNotEqual(observed_threads, [threading.get_ident()])
                self.assertFalse(window._can_change_configuration())
                self.assertFalse(window._cache_operation_allowed())
                self.assertFalse(window._scan("image"))
                self.assertFalse(window._start_upload("image"))
                self.assertTrue(window.settings_page.storage_panel._cleaning)
                window._cancel_cache_clear()
                self.assertTrue(worker.cancel_event.is_set())
                worker.finished.connect(loop.quit)
                release.set()
                QTimer.singleShot(3000, loop.quit)
                loop.exec()
                self.assertIsNone(window._cache_clear_worker)
                self.assertFalse(window.settings_page.storage_panel._cleaning)
        finally:
            release.set()
            if window._cache_clear_worker is not None:
                window._cache_clear_worker.wait(3000)
            window.deleteLater()

    def test_close_waits_for_deletion_worker(self):
        from tdlib_media_uploader.gui.main_window import MainWindow
        worker = Mock()
        worker.isRunning.return_value = True
        worker.wait.return_value = False
        window = SimpleNamespace(_cache_clear_worker=worker, statusBar=Mock())
        event = Mock()
        MainWindow.closeEvent(window, event)
        worker.request_stop.assert_called_once()
        worker.wait.assert_called_once_with(1000)
        event.ignore.assert_called_once()
        event.accept.assert_not_called()


class PreparationBoundaryTests(unittest.TestCase):
    def test_batch_preflight_creates_one_pool_and_reuses_probed_images(self):
        from concurrent.futures import ThreadPoolExecutor
        from tdlib_media_uploader.media import legacy_image
        from tdlib_media_uploader.media.image import ImageStrategy
        from tdlib_media_uploader.gui.integration import _premium_checker
        from tdlib_media_uploader.upload.preflight import preflight_items
        from tdlib_media_uploader.config.snapshot import snapshot_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = []
            for index in range(10):
                path = root / f"{index}.jpg"
                Image.new("RGB", (64, 64), "red").save(path)
                items.append(item_for(path, root))
            config = snapshot_config(legacy_image.cfg, overrides={"IMAGE_DIR": root,
                "STABILITY_CHECKS_LOCAL": 1, "STABILITY_INTERVAL_LOCAL_SECONDS": 0,
                "IO_WORKERS_LOCAL": 2, "STAGING_MODE": "off", "STAGING_ENABLED": False})
            with patch.object(legacy_image, "cfg", config), patch.object(legacy_image, "ThreadPoolExecutor", wraps=ThreadPoolExecutor) as pool, patch.object(legacy_image, "probe_image", wraps=legacy_image.probe_image) as probe:
                strategy = ImageStrategy(legacy_image)
                result = preflight_items(items, _premium_checker(strategy, SimpleNamespace()))
                self.assertEqual(result.ready_items, tuple(items))
                pool.assert_called_once()
                self.assertEqual(pool.call_args.kwargs["max_workers"], 2)
                for item in items:
                    legacy_image.input_photo(item.path)
                self.assertEqual(probe.call_count, len(items))

    def test_image_changed_during_encoding_does_not_publish_cache(self):
        from tdlib_media_uploader.media import image_prepare
        from tdlib_media_uploader.core.source_snapshot import SourceChanged
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "image.png"
            Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(source)
            encode = image_prepare.encode_jpeg_under_limit
            def mutate(*args, **kwargs):
                result = encode(*args, **kwargs)
                Image.new("RGBA", (80, 80), "blue").save(source)
                return result
            with patch.object(image_prepare, "IMAGE_COMPRESSION_CACHE_DIR", root / "cache"), patch.object(image_prepare, "encode_jpeg_under_limit", mutate):
                with self.assertRaises(SourceChanged):
                    image_prepare.prepare_image_for_telegram(source)
            self.assertFalse(list((root / "cache").glob("*.jpg")))

    def test_video_changed_during_remux_does_not_publish_reuse_manifest(self):
        from tdlib_media_uploader.media import video_prepare
        from tdlib_media_uploader.media.video_probe import VideoMediaInfo
        from tdlib_media_uploader.core.source_snapshot import SourceChanged
        from dataclasses import replace
        info = VideoMediaInfo(container="mkv", video_codec="h264", audio_codec="aac",
            width=64, height=48, duration=1, fps=25, has_video_stream=True,
            compatibility="remux", has_audio_stream=True, recommended_action="remux")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"before")
            def remux(source_path, destination, **kwargs):
                destination.write_bytes(b"processed")
                source_path.write_bytes(b"changed during remux")
                return destination
            with patch.object(video_prepare, "VIDEO_PROCESSED_CACHE_DIR", root / "cache"), patch.object(video_prepare, "remux_video_lossless", remux), patch.object(video_prepare, "probe_video", return_value=replace(info, compatibility="native")):
                with self.assertRaises(SourceChanged):
                    video_prepare.prepare_video_for_telegram(source, info=info, generate_thumbnail=False)
            self.assertFalse(list((root / "cache").rglob("manifest.json")))

    def test_task_config_is_readonly_and_detached_from_mutable_settings(self):
        from tdlib_media_uploader.config.snapshot import snapshot_config
        original = SimpleNamespace(IMAGE_DIR=Path("original"), OPTIONS={"colors": ["red"]})
        frozen = snapshot_config(original, immutable=True)
        original.OPTIONS["colors"].append("blue")
        self.assertEqual(frozen.OPTIONS["colors"], ("red",))
        with self.assertRaises(AttributeError):
            frozen.IMAGE_DIR = Path("other")
        with self.assertRaises(TypeError):
            frozen.OPTIONS["colors"] = ()
        clone = snapshot_config(frozen, immutable=True)
        self.assertEqual(clone.OPTIONS["colors"], ("red",))

    def test_unclosed_native_client_blocks_new_client_before_native_calls(self):
        active = SimpleNamespace(closed_event=threading.Event())
        with patch.object(tdlib_common.TDJsonClient, "_active_client", active), patch.object(tdlib_common.tdjson, "td_create_client_id") as create:
            with self.assertRaisesRegex(RuntimeError, "尚未关闭"):
                tdlib_common.TDJsonClient(Mock(), "test")
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
