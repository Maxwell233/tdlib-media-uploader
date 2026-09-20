"""Production upload adapters exercised with temporary media and an offline TDLib transport."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image

from tdlib_media_uploader.config import loader as cfg
from tdlib_media_uploader.config.snapshot import snapshot_config
from tdlib_media_uploader.core import album
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.gui import integration, models
from tdlib_media_uploader.media import legacy_image
from tdlib_media_uploader.telegram import tdlib_common as td
from tdlib_media_uploader.upload.reconciliation import ReconciliationService, SourceRootRequired

TARGET = {'target_mode': 'channel', 'chat_id': -1001, 'channel_chat_id': -1001}


class OfflineClient(td.TDJsonClient):
    """Keep real content validation and send transitions, replace network I/O."""
    sent = []
    outcome = None
    journal_root = None
    submitted_records = []

    def __init__(self, ui, device_model, *, config=None):
        self.ui = ui
        self._config = config
        self.cancel_event = threading.Event()
        self.caption_length_limit = 1024
        self.is_premium = True
        self.update_callbacks = []
        self.inflight_journal = None

    def login(self): pass
    def refresh_account_limits(self): pass
    def set_fast_options(self): pass
    def validate_target(self): pass
    def close(self): pass

    def request(self, query, timeout=None):
        self.sent.append(query)
        contents = query.get('input_message_contents', [query.get('input_message_content')])
        messages = [{'id': i + 100} for i, _ in enumerate(contents)]
        return messages[0] if query['@type'] == 'sendMessage' else {'messages': messages}

    def wait_for_send_results(self, messages, timeout=None):
        self.submitted_records.extend(json.loads(p.read_text()) for p in self.journal_root.glob('*.json'))
        if self.outcome is not None:
            raise self.outcome
        return {'succeeded': [m['id'] for m in messages], 'failed': [], 'pending': []}


class UploadConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'images'; self.source.mkdir()
        self.photo = self.source / 'one.jpg'
        Image.new('RGB', (12, 12), 'red').save(self.photo)
        self.runtime = SimpleNamespace(IMAGE_STATE_DIR=self.root/'state', UPLOAD_INFLIGHT_DIR=self.root/'journal')
        self.config = snapshot_config(cfg, 'image', TARGET)
        self.config.IMAGE_DIR = self.source
        self.config.IMAGE_RESET_STATE = False
        self.config.STAGING_MODE = 'off'; self.config.STAGING_ENABLED = False
        self.config.IMAGE_COMPRESS_OVERSIZE = False
        self.config.IMAGE_ALBUM_SIZE = 10
        for name, value in [('STABILITY_CHECKS_LOCAL', 1), ('STABILITY_INTERVAL_LOCAL_SECONDS', 0)]:
            setattr(self.config, name, value)
        for obj, name, value in [(legacy_image, 'cfg', self.config),
                                 (legacy_image, 'STATE_DIR', self.runtime.IMAGE_STATE_DIR),
                                 (album, 'PROJECT_DIR', self.root/'captions')]:
            patcher = patch.object(obj, name, value); patcher.start(); self.addCleanup(patcher.stop)
        OfflineClient.sent = []; OfflineClient.outcome = None
        OfflineClient.journal_root = self.runtime.UPLOAD_INFLIGHT_DIR
        OfflineClient.submitted_records = []

    def preview(self):
        bundle = integration.scan_v2('image', source_root=self.source, target=TARGET,
                                     config=self.config, runtime_paths=self.runtime)
        return models.scan_result(bundle)

    def run_upload(self, preview):
        return integration.run_v2_upload('image', ui=td.HeadlessUI(), source_root=self.source,
            target=TARGET, config=self.config, runtime_paths=self.runtime,
            preview_result=preview, client_factory=OfflineClient)

    def test_confirmed_preview_does_not_discover_added_files_and_keeps_caption_edit(self):
        preview = self.preview()
        Image.new('RGB', (12, 12), 'blue').save(self.source/'new.jpg')
        preview['caption_overrides'] = {preview['plans'][0].key: 'edited title'}
        with patch.object(legacy_image, 'scan_images', side_effect=AssertionError('must not rescan')):
            result = self.run_upload(preview)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(len(OfflineClient.sent), 1)
        content = OfflineClient.sent[0]['input_message_content']
        self.assertEqual(Path(content['photo']['path']), self.photo)
        self.assertEqual(content['caption']['text'], 'edited title')
        self.assertEqual(OfflineClient.submitted_records[0]['status'], 'SUBMITTED')
        self.assertEqual(OfflineClient.submitted_records[0]['message_ids'], [100])
        self.assertIn('source_root', OfflineClient.submitted_records[0]['items'][0])

    def test_replaced_preview_file_is_not_uploaded(self):
        preview = self.preview()
        self.photo.write_bytes(b'replacement')
        self.run_upload(preview)
        self.assertEqual(OfflineClient.sent, [])

    def test_target_is_explicit_even_if_global_configuration_changes(self):
        preview = self.preview()
        with patch.object(td.cfg, 'CHAT_ID', -9999), patch.object(td.cfg, 'TARGET_MODE', 'forum_topic'):
            result = self.run_upload(preview)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(OfflineClient.sent[0]['chat_id'], TARGET['chat_id'])
        self.assertIsNone(OfflineClient.sent[0]['topic_id'])

    def test_partial_outcome_preserves_ids_and_blocks_regrouped_files(self):
        preview = self.preview()
        OfflineClient.outcome = td.SendResultUnknown('timeout', {'succeeded': [], 'failed': [], 'pending': [100]})
        result = self.run_upload(preview)
        self.assertEqual(result.status, 'UNKNOWN')
        journal = InflightJournal(self.runtime.UPLOAD_INFLIGHT_DIR)
        record = journal.list_unresolved()[0]
        self.assertEqual(record['pending_ids'], [100])
        self.assertEqual(record['message_ids'], [100])
        self.config.IMAGE_ALBUM_NUMBER_START += 1
        regrouped = self.preview()
        self.assertNotEqual(regrouped['plans'][0].key, preview['plans'][0].key)
        OfflineClient.outcome = None; OfflineClient.sent = []
        self.assertEqual(self.run_upload(regrouped).status, 'UNKNOWN')
        self.assertEqual(OfflineClient.sent, [])
        self.assertIsNone(journal.unresolved_for_items('image', record['items'],
                         target={'target_mode':'channel','chat_id':-2002}))

    def test_recovery_uses_original_root_without_resetting_other_checkpoints(self):
        preview = self.preview()
        OfflineClient.outcome = td.SendResultUnknown('timeout', {'succeeded': [], 'failed': [], 'pending':[100]})
        self.run_upload(preview)
        journal = InflightJournal(self.runtime.UPLOAD_INFLIGHT_DIR)
        record = journal.list_unresolved()[0]
        self.config.IMAGE_DIR = self.root/'different'; self.config.IMAGE_RESET_STATE = True
        ReconciliationService(journal).reconcile_inflight(record['album_key'], kind='image', sent=True, target=TARGET)
        state = UploadState(kind='image', source_root=self.source, target=TARGET, state_dir=self.runtime.IMAGE_STATE_DIR)
        self.assertTrue(state.is_completed(record['items'][0]))
        self.assertEqual(journal.list_unresolved(), [])

    def test_legacy_record_requires_explicit_root_and_keeps_guard(self):
        journal = InflightJournal(self.runtime.UPLOAD_INFLIGHT_DIR)
        record = journal.prepare('image','old',[{'path':self.photo}],target=TARGET)
        service = ReconciliationService(journal)
        with self.assertRaises(SourceRootRequired):
            service.reconcile_inflight('old',kind='image',sent=True,target=TARGET)
        self.assertIsNotNone(journal.unresolved('image','old',TARGET))
        service.reconcile_inflight('old',kind='image',sent=True,target=TARGET,source_root=self.source)
        state = UploadState(kind='image',source_root=self.source,target=TARGET,state_dir=self.runtime.IMAGE_STATE_DIR)
        self.assertTrue(state.is_completed(record['items'][0]))

    def test_all_failed_results_remain_safe_to_retry(self):
        preview = self.preview()
        OfflineClient.outcome = td.SendResultFailed('rejected', {'succeeded': [], 'failed':[100], 'pending':[]})
        self.assertEqual(self.run_upload(preview).status, 'FAILED')
        journal = InflightJournal(self.runtime.UPLOAD_INFLIGHT_DIR)
        self.assertEqual(journal.list_unresolved(), [])
        OfflineClient.outcome = None
        self.assertEqual(self.run_upload(preview).status, 'COMPLETED')

    def test_failure_after_submission_keeps_known_message_ids(self):
        preview = self.preview()
        OfflineClient.outcome = RuntimeError('connection lost')
        self.assertEqual(self.run_upload(preview).status, 'UNKNOWN')
        record = InflightJournal(self.runtime.UPLOAD_INFLIGHT_DIR).list_unresolved()[0]
        self.assertEqual(record['message_ids'], [100])

    def test_incomplete_album_response_keeps_available_ids(self):
        Image.new('RGB', (12, 12), 'blue').save(self.source/'two.jpg')
        preview = self.preview()
        with patch.object(OfflineClient, 'request', return_value={'messages': [{'id': 100}]}):
            self.assertEqual(self.run_upload(preview).status, 'UNKNOWN')
        record = InflightJournal(self.runtime.UPLOAD_INFLIGHT_DIR).list_unresolved()[0]
        self.assertEqual(record['message_ids'], [100])
        self.assertEqual(record['pending_ids'], [100])

    def test_authorization_wait_has_a_real_deadline(self):
        client = object.__new__(td.TDJsonClient)
        client.cancel_event = threading.Event()
        client.request = lambda *args, **kwargs: {}
        client.auth_queue = queue.Queue()
        with patch.object(td.time, 'monotonic', side_effect=[0, 121]):
            with self.assertRaisesRegex(TimeoutError, '授权状态超时'):
                client.login()
