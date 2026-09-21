"""V1.9.3 regressions for mixed-media TDLib payload construction."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.telegram import tdlib_common
from tdlib_media_uploader.media import legacy_mixed as mixed_core


class MixedInputFileRegressionTest(unittest.TestCase):
    def test_mixed_video_uses_the_standalone_video_payload_builder(self):
        item = {"path": Path("clip.mp4"), "media_kind": "video"}
        expected = {
            "@type": "inputMessageVideo",
            "video": {"@type": "inputFileId", "id": 1},
        }
        with patch.object(mixed_core.video_core, "input_video", return_value=expected) as builder, \
                patch.object(mixed_core.cfg, "MIXED_GENERATE_THUMBNAIL", False):
            result = mixed_core._mixed_input_video(item, "caption")
        self.assertIs(result, expected)
        builder.assert_called_once_with(
            item,
            "caption",
            generate_thumbnail=False,
            thumbnail_timestamp_seconds=mixed_core.cfg.MIXED_THUMBNAIL_TIMESTAMP_SECONDS,
        )

    def test_mixed_photo_and_video_payloads_keep_required_input_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo_path = root / "photo.jpg"
            video_path = root / "clip.mp4"
            photo_path.write_bytes(b"photo")
            video_path.write_bytes(b"video")
            items = [
                {"path": photo_path, "media_kind": "image"},
                {"path": video_path, "media_kind": "video"},
            ]
            photo_content = {
                "@type": "inputMessagePhoto",
                "photo": {"@type": "inputFileLocal", "path": str(photo_path)},
                "thumbnail": None,
            }
            video_content = {
                "@type": "inputMessageVideo",
                "video": {"@type": "inputFileLocal", "path": str(video_path)},
                "thumbnail": None,
                "cover": None,
            }
            with patch.object(mixed_core.image_core, "input_photo", return_value=photo_content), \
                    patch.object(mixed_core, "_mixed_input_video", return_value=video_content):
                contents, valid, skipped = mixed_core.build_mixed_contents(items, "Album")
            self.assertEqual(len(valid), 2)
            self.assertEqual(skipped, [])
            serialized = json.loads(json.dumps(contents, ensure_ascii=False))
            tdlib_common.validate_input_message_contents(serialized, items=items)
            self.assertEqual(serialized[0]["photo"]["@type"], "inputFileLocal")
            self.assertEqual(serialized[1]["video"]["@type"], "inputFileLocal")

    def test_malformed_input_file_is_rejected_before_journal_or_tdlib(self):
        class Journal:
            def __init__(self):
                self.calls = []

            def unresolved(self, *args, **kwargs):
                return None

            def prepare(self, *args, **kwargs):
                self.calls.append("prepare")

        journal = Journal()
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client.ui = type("UI", (), {"warning": lambda *_args: None})()
        client.cancel_event = threading.Event()
        client.inflight_journal = journal
        client.caption_length_limit = None
        client._safe_diagnose_upload_failure = lambda *_args: None
        client.request = lambda _query: self.fail("malformed payload reached TDLib")
        with self.assertRaises(tdlib_common.InvalidTDLibInputPayload):
            client.send_contents(
                [{"@type": "inputMessageVideo", "video": None}],
                album_key="malformed-mixed",
                kind="mixed",
                items=[{"path": Path("clip.mp4")}],
            )
        self.assertEqual(journal.calls, [])

    def test_valid_mixed_payload_reaches_album_request(self):
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client.caption_length_limit = None
        queries = []
        client.request = lambda query: (
            queries.append(query)
            or {"messages": [{"id": 101}, {"id": 102}]}
        )
        client.wait_for_send_results = lambda _messages: {
            "succeeded": [101, 102],
            "failed": [],
            "pending": [],
        }
        contents = [
            {
                "@type": "inputMessagePhoto",
                "photo": {"@type": "inputFileId", "id": 1},
                "thumbnail": None,
            },
            {
                "@type": "inputMessageVideo",
                "video": {"@type": "inputFileId", "id": 2},
                "thumbnail": None,
                "cover": None,
            },
        ]
        self.assertEqual(client.send_contents(contents, kind="mixed"), [101, 102])
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["@type"], "sendMessageAlbum")
        self.assertEqual(queries[0]["input_message_contents"], contents)


if __name__ == "__main__":
    unittest.main()
