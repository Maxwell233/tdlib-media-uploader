"""Offline regressions: no Telegram login or network requests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core import album as metadata
from tdlib_media_uploader.core import logging as app_logging
from tdlib_media_uploader.gui import main_window as gui
from tdlib_media_uploader.gui import config_service, tools, cache_service
from tdlib_media_uploader.core import filesystem_legacy as path_utils
from PySide6.QtWidgets import QApplication, QGroupBox, QHBoxLayout, QScrollArea


class ImprovementsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


    def test_immediate_tdlib_send_failure_is_recorded_as_failed(self):
        from tdlib_media_uploader.telegram import tdlib_common
        from tdlib_media_uploader.core.upload_journal import FAILED, InflightJournal

        class UI:
            def warning(self, _text):
                pass

        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
            client.ui = UI()
            client.inflight_journal = journal
            client.cancel_event = threading.Event()
            client.request = lambda _query: {
                "id": 17,
                "sending_state": {"@type": "messageSendingStateFailed"},
            }
            with patch.object(client, "_safe_diagnose_upload_failure"):
                with self.assertRaises(RuntimeError):
                    client.send_contents(
                        [{
                            "@type": "inputMessagePhoto",
                            "photo": {"@type": "inputFileId", "id": 1},
                        }],
                        album_key="album-key",
                        kind="image",
                    )
            record = journal.get("image", "album-key")
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], FAILED)


    def test_forum_target_identity_ignores_irrelevant_channel_id(self):
        from tdlib_media_uploader.core.upload_journal import normalize_target

        first = normalize_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 999,
            }
        )
        second = normalize_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 888,
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 0,
            },
        )


    def test_channel_target_identity_ignores_irrelevant_forum_topic_id(self):
        from tdlib_media_uploader.core.upload_journal import normalize_target

        first = normalize_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 10,
            }
        )
        second = normalize_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 999,
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            {
                "target_mode": "channel",
                "chat_id": 200,
                "forum_topic_id": 0,
                "channel_chat_id": 200,
            },
        )


    def test_target_normalization_parses_only_mode_relevant_fields(self):
        from tdlib_media_uploader.core.upload_journal import normalize_target

        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "channel",
                    "channel_chat_id": 200,
                    "chat_id": "invalid",
                }
            ),
            {
                "target_mode": "channel",
                "chat_id": 200,
                "forum_topic_id": 0,
                "channel_chat_id": 200,
            },
        )
        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "channel",
                    "channel_chat_id": 0,
                    "chat_id": 200,
                }
            )["chat_id"],
            200,
        )
        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "forum_topic",
                    "chat_id": 100,
                    "forum_topic_id": 10,
                    "channel_chat_id": "invalid",
                }
            )["forum_topic_id"],
            10,
        )
        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "channel",
                    "channel_chat_id": 200,
                    "forum_topic_id": "invalid",
                }
            )["channel_chat_id"],
            200,
        )


    def test_target_identity_changes_for_topic_channel_or_mode(self):
        from tdlib_media_uploader.core.upload_journal import normalize_target

        forum_topic_10 = normalize_target(
            {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10}
        )
        forum_topic_11 = normalize_target(
            {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 11}
        )
        channel_200 = normalize_target(
            {"target_mode": "channel", "channel_chat_id": 200}
        )
        channel_201 = normalize_target(
            {"target_mode": "channel", "channel_chat_id": 201}
        )
        self.assertNotEqual(forum_topic_10, forum_topic_11)
        self.assertNotEqual(channel_200, channel_201)
        self.assertNotEqual(forum_topic_10, channel_200)


    def test_tdlib_upload_failure_logs_source_diagnosis(self):
        from tdlib_media_uploader.telegram import tdlib_common

        class UI:
            def __init__(self):
                self.messages = []

            def warning(self, text):
                self.messages.append(str(text))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
            client.ui = UI()
            client.request = lambda _query: (_ for _ in ()).throw(
                tdlib_common.TDLibError(400, "FILE_READ")
            )
            with patch.object(tdlib_common, "write_app_log") as write:
                with self.assertRaises(tdlib_common.TDLibError):
                    client.send_contents(
                        [{
                            "@type": "inputMessageVideo",
                            "video": {"@type": "inputFileId", "id": 1},
                            "thumbnail": None,
                            "cover": None,
                        }],
                        items=[{"path": path, "scan_size": 5, "scan_mtime_ns": path.stat().st_mtime_ns}],
                    )
            self.assertTrue(write.called)
            self.assertIn("源文件可读取", write.call_args.args[1])
            self.assertTrue(client.ui.messages)
