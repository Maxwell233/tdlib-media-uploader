"""Ownership index for V1.9 Telegram regressions.

The legacy tests remain the executable source of truth during the first-wave
extraction.  Target and limit assertions are listed as Telegram candidates;
tests that also touch journals, state, media scanning, or GUI remain indexed
as cross-boundary until those owners have stable V2 implementations.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RegressionRef:
    source: str
    test_class: str
    method: str
    owner: str
    disposition: str
    note: str


TELEGRAM_UNIT_REGRESSIONS = (
    RegressionRef(
        "tests/test_regression_matrix.py",
        "ImprovementsTest",
        "test_forum_target_identity_ignores_irrelevant_channel_id",
        "telegram/target",
        "legacy-preserved",
        "Forum-topic identity ignores channel-only fields.",
    ),
    RegressionRef(
        "tests/test_regression_matrix.py",
        "ImprovementsTest",
        "test_channel_target_identity_ignores_irrelevant_forum_topic_id",
        "telegram/target",
        "legacy-preserved",
        "Channel identity ignores forum-topic-only fields.",
    ),
    RegressionRef(
        "tests/test_regression_matrix.py",
        "ImprovementsTest",
        "test_target_normalization_parses_only_mode_relevant_fields",
        "telegram/target",
        "legacy-preserved",
        "Canonical target normalization is mode-aware.",
    ),
    RegressionRef(
        "tests/test_regression_matrix.py",
        "ImprovementsTest",
        "test_target_identity_changes_for_topic_channel_or_mode",
        "telegram/target",
        "legacy-preserved",
        "Relevant target changes produce a different identity.",
    ),
    RegressionRef(
        "tests/test_runtime_hardening.py",
        "V190HardeningTest",
        "test_telegram_video_boundaries_are_exact_and_shared",
        "telegram/limits",
        "legacy-preserved",
        "Standard and Premium byte boundaries are exact.",
    ),
    RegressionRef(
        "tests/test_runtime_hardening.py",
        "V190HardeningTest",
        "test_runtime_caption_limit_blocks_before_tdlib_request",
        "telegram/limits + send_result",
        "legacy-preserved",
        "Runtime caption validation prevents an invalid request.",
    ),
    RegressionRef(
        "tests/test_runtime_hardening.py",
        "V190HardeningTest",
        "test_runtime_caption_limit_above_legacy_1024_is_allowed",
        "telegram/limits + send_result",
        "legacy-preserved",
        "A runtime limit above the historical soft limit is honored.",
    ),
)


CROSS_BOUNDARY_REGRESSIONS = (
    RegressionRef(
        "tests/test_upload_journal.py",
        "UploadJournalTest",
        "test_unknown_journal_blocks_until_manual_reconciliation",
        "upload/journal + telegram/send_result",
        "migrated",
        "Durable UNKNOWN recovery belongs to the upload journal owner.",
    ),
    RegressionRef(
        "tests/test_upload_journal.py",
        "UploadJournalTest",
        "test_confirmed_record_blocks_until_finalization",
        "upload/journal + telegram/send_result",
        "migrated",
        "Journal finalization is not transport-only behavior.",
    ),
    RegressionRef(
        "tests/test_regression_matrix.py",
        "ImprovementsTest",
        "test_immediate_tdlib_send_failure_is_recorded_as_failed",
        "telegram/send_result + upload/journal",
        "deferred",
        "Failure classification and durable journal writes are coupled.",
    ),
    RegressionRef(
        "tests/test_upload_reconciliation.py",
        "UploadReconciliationTest",
        "test_inflight_kind_can_be_inferred_for_manual_reconciliation",
        "upload/journal + telegram/send_result",
        "migrated",
        "Legacy kind inference is part of durable recovery, not transport-only behavior.",
    ),
    RegressionRef(
        "tests/test_upload_reconciliation.py",
        "UploadReconciliationTest",
        "test_manual_sent_reconciliation_writes_checkpoint_before_removing_journal",
        "upload/state + upload/journal",
        "migrated",
        "Manual reconciliation crosses state and journal ownership.",
    ),
    RegressionRef(
        "tests/test_upload_reconciliation.py",
        "UploadReconciliationTest",
        "test_manual_sent_state_failure_keeps_journal",
        "upload/state + upload/journal",
        "migrated",
        "Recovery ordering must stay with the upload lifecycle.",
    ),
    RegressionRef(
        "tests/test_upload_journal.py",
        "UploadJournalTest",
        "test_target_scoping_keeps_legacy_records_conservative",
        "upload/journal + telegram/target",
        "migrated",
        "Target scope is exercised through durable journal lookup.",
    ),
    RegressionRef(
        "tests/test_upload_reconciliation.py",
        "UploadReconciliationTest",
        "test_legacy_reconciliation_uses_kind_target_instead_of_global_target",
        "upload/journal + telegram/target",
        "migrated",
        "Legacy reconciliation spans all media kinds and target selection.",
    ),
    RegressionRef(
        "tests/test_upload_reconciliation.py",
        "UploadReconciliationTest",
        "test_journal_target_takes_precedence_over_state_fallback_target",
        "upload/journal + telegram/target",
        "migrated",
        "Journal and state fallback precedence is an integration rule.",
    ),
    RegressionRef(
        "tests/test_upload_journal.py",
        "UploadJournalTest",
        "test_forum_unknown_record_matches_after_irrelevant_channel_change",
        "upload/journal + telegram/target",
        "migrated",
        "Canonical target matching is verified through journal recovery.",
    ),
    RegressionRef(
        "tests/test_upload_journal.py",
        "UploadJournalTest",
        "test_channel_unknown_record_matches_after_irrelevant_topic_change",
        "upload/journal + telegram/target",
        "migrated",
        "Canonical target matching is verified through journal recovery.",
    ),
    RegressionRef(
        "tests/test_upload_journal.py",
        "UploadJournalTest",
        "test_version_two_target_record_uses_canonical_identity_for_fallback",
        "upload/journal + telegram/target",
        "migrated",
        "Versioned journal compatibility remains with journal migration.",
    ),
    RegressionRef(
        "tests/test_regression_matrix.py",
        "ImprovementsTest",
        "test_tdlib_upload_failure_logs_source_diagnosis",
        "telegram/send_result + processes/runner",
        "deferred",
        "Transport failure diagnosis also reads the local source file.",
    ),
    RegressionRef(
        "tests/test_runtime_hardening.py",
        "V190HardeningTest",
        "test_video_and_mixed_scanners_apply_the_same_video_limits",
        "media/video + media/mixed + telegram/limits",
        "deferred",
        "Scanner policy and Telegram limits are intentionally cross-boundary.",
    ),
    RegressionRef(
        "tests/test_regression_matrix.py",
        "ImprovementsTest",
        "test_media_scan_applies_telegram_size_limits",
        "media/image + media/video + telegram/limits",
        "deferred",
        "Media scanners own the decision to skip or defer oversize files.",
    ),
)


REVIEWED_NOT_TELEGRAM = (
    RegressionRef(
        "tests/test_runtime_hardening.py",
        "V190HardeningTest",
        "test_caption_editor_uses_soft_limit_but_runtime_validation_stays_authoritative",
        "gui + album_metadata",
        "deferred",
        "The editor is GUI-owned; only runtime validation is Telegram-owned.",
    ),
    RegressionRef(
        "tests/test_runtime_hardening.py",
        "V190HardeningTest",
        "test_filename_description_is_capped_by_runtime_limit",
        "album_metadata + media",
        "deferred",
        "Filename description composition is not transport ownership.",
    ),
    RegressionRef(
        "tests/test_data_layout.py",
        "DataLayoutTest",
        "test_tdlib_parameter_payload_uses_data_telegram_paths",
        "config/paths + app",
        "deferred",
        "TDLib data directories belong to runtime path configuration.",
    ),
)


ALL_INDEXED = (
    *TELEGRAM_UNIT_REGRESSIONS,
    *CROSS_BOUNDARY_REGRESSIONS,
    *REVIEWED_NOT_TELEGRAM,
)


def _method_names(source: Path, test_class: str) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == test_class:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def _assert_refs_exist(testcase: unittest.TestCase, refs: tuple[RegressionRef, ...]) -> None:
    missing: list[str] = []
    for ref in refs:
        source = REPO_ROOT / ref.source
        if not source.is_file():
            missing.append(f"{ref.source}: source file missing")
            continue
        if ref.method not in _method_names(source, ref.test_class):
            missing.append(f"{ref.source}:{ref.test_class}.{ref.method}")
    testcase.assertFalse(missing, "stale regression index entries: " + ", ".join(missing))


class TelegramRegressionIndexTest(unittest.TestCase):
    def test_indexed_telegram_regressions_are_still_executable(self):
        _assert_refs_exist(self, TELEGRAM_UNIT_REGRESSIONS)

    def test_cross_boundary_and_reviewed_entries_are_registered(self):
        _assert_refs_exist(self, (*CROSS_BOUNDARY_REGRESSIONS, *REVIEWED_NOT_TELEGRAM))
        keys = [(ref.source, ref.test_class, ref.method) for ref in ALL_INDEXED]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
