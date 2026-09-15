"""Ownership index for V1.9 filesystem and process regressions.

The executable assertions intentionally remain in the legacy test modules for
this extraction step.  Several of them patch module globals from
``test_improvements.py`` and a physical move would require changing patch
targets before the new package implementations exist.  This index makes the
future ownership explicit without duplicating assertions or weakening the
current regression suite.
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


FILESYSTEM_PROCESS_REGRESSIONS = (
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_natural_filename_sort_uses_ascending_numeric_runs",
        "filesystem/sorting",
        "legacy-preserved",
        "Natural sorting of filename numeric runs.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_natural_sort_is_deterministic_for_equal_case_and_leading_zero_runs",
        "filesystem/sorting",
        "legacy-preserved",
        "Deterministic case and leading-zero tie breaking.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_cancellable_process_drains_pipes_and_passes_utf8_input",
        "processes/runner",
        "legacy-preserved",
        "Cancellable subprocess I/O and UTF-8 input.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_cancellable_process_drains_large_stdout_and_stderr",
        "processes/runner",
        "legacy-preserved",
        "Large stdout/stderr must not deadlock the child process.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_cancellable_process_cancel_and_timeout_reap_child",
        "processes/runner",
        "legacy-preserved",
        "Cancellation and timeout must reap child processes.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_cancellable_process_kills_child_that_ignores_terminate",
        "processes/runner",
        "legacy-preserved",
        "Escalation when a child ignores termination.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_discovery_retries_scandir_and_stat",
        "filesystem/filesystem",
        "legacy-preserved",
        "Bounded retry for directory enumeration and stat.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_discovery_retry_can_be_cancelled_and_scan_result_separates_warnings",
        "filesystem/filesystem",
        "legacy-preserved",
        "Cancellation and error-versus-warning scan result semantics.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_validate_scan_root_retries_transient_stat",
        "filesystem/filesystem",
        "legacy-preserved",
        "Scan-root validation retry behavior.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_directory_iterator_recovers_after_mid_enumeration_error",
        "filesystem/filesystem",
        "legacy-preserved",
        "Recovery after a transient mid-enumeration failure.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_stability_interval_honours_configured_value",
        "filesystem/readiness",
        "legacy-preserved",
        "Configured stability interval is passed to cancellation-aware sleep.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_ordered_bounded_map_keeps_submitting_behind_slow_head",
        "filesystem/concurrency",
        "legacy-preserved",
        "Bounded ordered concurrency keeps the queue occupied.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_filesystem_error_classification_distinguishes_permanent_failures",
        "filesystem/readiness",
        "legacy-preserved",
        "Transient and permanent filesystem errors remain distinct.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_natural_sort_handles_unicode_numeric_runs_and_folder_names",
        "filesystem/sorting",
        "legacy-preserved",
        "Unicode numeric runs and folder components.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_natural_path_sort_compares_each_directory_component_first",
        "filesystem/sorting",
        "legacy-preserved",
        "Directory components sort before filename components.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_relative_path_sort_compares_directories_before_filenames",
        "filesystem/sorting",
        "legacy-preserved",
        "Relative path ordering preserves directory precedence.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_relative_path_sort_mtime_ties_use_the_same_path_comparator",
        "filesystem/sorting",
        "legacy-preserved",
        "mtime ties use the deterministic relative-path comparator.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_directory_scan_uses_one_stat_per_matching_file",
        "filesystem/filesystem",
        "legacy-preserved",
        "A scan uses its captured snapshot rather than a late stat.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_directory_scan_can_be_cancelled_and_skips_symlinks",
        "filesystem/filesystem",
        "legacy-preserved",
        "Cancellation and symlink exclusion during discovery.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_readiness_supports_multiple_stability_checks",
        "filesystem/readiness",
        "legacy-preserved",
        "Multiple readiness checks preserve the final snapshot.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_macos_network_mounts_are_recognized",
        "filesystem/readiness",
        "legacy-preserved",
        "Network path recognition across macOS and UNC forms.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_macos_mount_type_distinguishes_network_and_local_volumes",
        "filesystem/readiness",
        "legacy-preserved",
        "macOS mount output distinguishes local and network volumes.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_file_mtime_uses_os_stat_and_keeps_fallback",
        "filesystem/filesystem",
        "legacy-preserved",
        "mtime fallback remains available when stat fails.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_extension_filter_preserves_path_suffix_edge_cases",
        "filesystem/filesystem",
        "legacy-preserved",
        "Hidden files and trailing dots keep their suffix semantics.",
    ),
)


RELATED_BUT_DEFERRED = (
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_gui_fallback_uses_the_shared_path_sorter",
        "gui",
        "deferred",
        "GUI adapter coverage; keep with the main-owned GUI migration.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_image_filename_scan_uses_natural_numeric_order",
        "media/image + filesystem/sorting",
        "deferred",
        "Scanner integration remains with the image strategy migration.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_video_filename_scan_uses_natural_numeric_order",
        "media/video + filesystem/sorting",
        "deferred",
        "Scanner integration remains with the video strategy migration.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_all_media_scanners_keep_directory_order_before_basename_order",
        "media/image + media/mixed + media/video",
        "deferred",
        "Cross-strategy scanner integration is not a first-wave unit test.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_mtime_fallback_does_not_probe_ffmpeg_when_media_dates_disabled",
        "media/video + processes/ffmpeg",
        "deferred",
        "Date policy and FFmpeg probing remain with media/process integration.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_disabled_video_dates_uses_filename_only",
        "media/video",
        "deferred",
        "Filename-only video planning is media strategy behavior.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_filename_only_video_list_handles_missing_dates",
        "media/video + app",
        "deferred",
        "Legacy presentation of missing dates remains outside this wave.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_cancelled_video_scan_is_not_reported_as_normal_empty_result",
        "media/video + app",
        "deferred",
        "Cancellation presentation is a later media/application integration test.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_local_staging_copies_a_validated_snapshot",
        "staging",
        "deferred",
        "Staging lifecycle is outside the first filesystem/process extraction.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_staging_cleanup_never_enters_linked_shard_directory",
        "staging",
        "deferred",
        "Staging safety remains with the later staging migration.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_staging_cleanup_removes_only_stale_files",
        "staging",
        "deferred",
        "Staging cleanup is not owned by the first wave.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_jit_revalidation_detects_video_changes_after_scan",
        "media/video + upload/preflight",
        "deferred",
        "Media preflight owns the revalidation decision.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_build_items_probes_missing_media_dates_with_four_workers",
        "media/video",
        "deferred",
        "Media-date probing is a later media/process integration task.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_media_date_reader_uses_one_ffmpeg_invocation",
        "processes/ffmpeg + media/video",
        "deferred",
        "FFmpeg adapter extraction is a later bounded-process task.",
    ),
    RegressionRef(
        "tests/test_improvements.py",
        "ImprovementsTest",
        "test_media_date_failure_is_not_negative_cached",
        "media/video",
        "deferred",
        "Media-date cache behavior is not generic process-runner behavior.",
    ),
    RegressionRef(
        "tests/test_v190_hardening.py",
        "V190HardeningTest",
        "test_direct_entrypoint_uses_shared_instance_lock",
        "app/instance-lock",
        "deferred",
        "Application single-instance ownership remains main-owned.",
    ),
    RegressionRef(
        "tests/test_v190_hardening.py",
        "V190HardeningTest",
        "test_staging_rejects_linked_parent",
        "staging",
        "deferred",
        "Staging path safety is outside this extraction.",
    ),
    RegressionRef(
        "tests/test_v190_hardening.py",
        "V190HardeningTest",
        "test_staging_rejects_linked_configured_base",
        "staging",
        "deferred",
        "Staging path safety is outside this extraction.",
    ),
)


RUNTIME_PATH_TESTS_REVIEWED = (
    RegressionRef(
        "tests/test_data_layout.py",
        "DataLayoutTest",
        "test_source_run_uses_repository_data_telegram_directories",
        "config/paths",
        "deferred",
        "Runtime-data layout belongs to config and application paths.",
    ),
    RegressionRef(
        "tests/test_data_layout.py",
        "DataLayoutTest",
        "test_frozen_windows_uses_executable_data_directory",
        "config/paths + packaging",
        "deferred",
        "Frozen platform path resolution is not filesystem discovery.",
    ),
    RegressionRef(
        "tests/test_data_layout.py",
        "DataLayoutTest",
        "test_frozen_macos_uses_application_support_data_directory",
        "config/paths + packaging",
        "deferred",
        "Frozen platform path resolution is not filesystem discovery.",
    ),
    RegressionRef(
        "tests/test_data_layout.py",
        "DataLayoutTest",
        "test_self_test_reports_tdlib_paths_under_data_dir",
        "app/self-test",
        "deferred",
        "Self-test presentation remains with the application entrypoint.",
    ),
    RegressionRef(
        "tests/test_data_layout.py",
        "DataLayoutTest",
        "test_tdlib_parameter_payload_uses_data_telegram_paths",
        "telegram/client + config/paths",
        "deferred",
        "TDLib path payload is an integration boundary, not discovery.",
    ),
)


ALL_INDEXED = (
    *FILESYSTEM_PROCESS_REGRESSIONS,
    *RELATED_BUT_DEFERRED,
    *RUNTIME_PATH_TESTS_REVIEWED,
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


class FilesystemProcessRegressionIndexTest(unittest.TestCase):
    def test_indexed_legacy_regressions_are_still_executable(self):
        _assert_refs_exist(self, FILESYSTEM_PROCESS_REGRESSIONS)

    def test_deferred_related_and_runtime_path_reviews_are_registered(self):
        _assert_refs_exist(self, (*RELATED_BUT_DEFERRED, *RUNTIME_PATH_TESTS_REVIEWED))
        keys = [(ref.source, ref.test_class, ref.method) for ref in ALL_INDEXED]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
