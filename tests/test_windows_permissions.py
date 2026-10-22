"""Windows ACL diagnostics stay read-only and never prevent using the app."""

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tdlib_media_uploader.config import paths
from tdlib_media_uploader.core.windows_permissions import (
    PermissionDiagnostic,
    check_data_directory_permissions,
)


class WindowsPermissionTest(unittest.TestCase):
    def _check_acl(self, readers, *, has_dacl=True):
        completed = SimpleNamespace(stdout=json.dumps({
            "current_user": "S-1-5-21-1001", "has_dacl": has_dacl, "readers": readers,
        }))
        with patch("sys.platform", "win32"), patch(
            "tdlib_media_uploader.core.windows_permissions.subprocess.run", return_value=completed
        ) as run:
            result = check_data_directory_permissions(Path("data"))
        return result, run

    def test_current_user_system_and_admin_read_grants_are_expected(self):
        diagnostic, run = self._check_acl(["S-1-5-21-1001", "S-1-5-18", "S-1-5-32-544"])
        self.assertEqual(diagnostic.status, "ok")
        self.assertEqual(run.call_args.kwargs["env"]["TDLIB_DATA_PERMISSION_PATH"], str(Path("data").absolute()))
        self.assertIn("Get-Acl -LiteralPath", run.call_args.args[0][-1])
        self.assertNotIn("Set-Acl", run.call_args.args[0][-1])

    def test_other_account_read_grants_and_unrestricted_dacl_are_reported(self):
        for readers, has_dacl in ((["S-1-1-0"], True), (["S-1-5-32-545"], True),
                                  (["S-1-5-21-1002"], True), ([], False)):
            with self.subTest(readers=readers, has_dacl=has_dacl):
                diagnostic, _ = self._check_acl(readers, has_dacl=has_dacl)
                self.assertEqual(diagnostic.status, "warn")
                self.assertIn("属性", diagnostic.detail)

    def test_unavailable_powershell_timeout_or_unreadable_acl_is_only_a_diagnostic(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("powershell", 3),
                      subprocess.CalledProcessError(1, "powershell")):
            with self.subTest(error=type(error).__name__), patch("sys.platform", "win32"), patch(
                "tdlib_media_uploader.core.windows_permissions.subprocess.run", side_effect=error
            ):
                diagnostic = check_data_directory_permissions(Path("data"))
                self.assertEqual(diagnostic.text, "无法检查")
                self.assertEqual(diagnostic.status, "muted")
        with patch("sys.platform", "win32"), patch(
            "tdlib_media_uploader.core.windows_permissions.subprocess.run", return_value=SimpleNamespace(stdout="not json")
        ):
            self.assertEqual(check_data_directory_permissions(Path("data")).text, "无法检查")

    def test_non_windows_does_not_launch_powershell(self):
        with patch("sys.platform", "darwin"), patch(
            "tdlib_media_uploader.core.windows_permissions.subprocess.run"
        ) as run:
            self.assertEqual(check_data_directory_permissions(Path("data")).status, "muted")
        run.assert_not_called()

    def test_startup_records_data_directory_warning_without_rejecting_startup(self):
        diagnostic = PermissionDiagnostic("warn", "有其他账户的读取授权", "检查文件夹权限")
        with patch("sys.platform", "win32"), patch(
            "tdlib_media_uploader.core.persistence.ensure_private_directory"
        ), patch(
            "tdlib_media_uploader.core.windows_permissions.check_data_directory_permissions", return_value=diagnostic
        ) as check, patch("tdlib_media_uploader.core.logging.write_app_log") as log, patch.object(
            paths, "DATA_PERMISSION_DIAGNOSTIC", None
        ):
            self.assertEqual(paths.ensure_data_dirs(), paths.DATA_DIR)
            check.assert_called_once_with(paths.DATA_DIR)
            self.assertEqual(paths.DATA_PERMISSION_DIAGNOSTIC, diagnostic)
            log.assert_called_once_with("WARNING", diagnostic.text, source="data-permissions")

    def test_settings_displays_the_startup_result(self):
        from PySide6.QtWidgets import QApplication
        from tdlib_media_uploader.gui.settings.environment_license_panel import EnvironmentLicensePanel

        app = QApplication.instance() or QApplication([])
        diagnostic = PermissionDiagnostic("warn", "有其他账户的读取授权", "检查文件夹权限")
        with patch("sys.platform", "win32"), patch.object(paths, "DATA_PERMISSION_DIAGNOSTIC", diagnostic):
            panel = EnvironmentLicensePanel()
            try:
                self.assertIn(diagnostic.text, panel.data_permissions_label.text())
                self.assertEqual(panel.data_permissions_label.toolTip(), diagnostic.detail)
                self.assertEqual(panel.data_permissions_label.property("status"), "warn")
            finally:
                panel.deleteLater()


if __name__ == "__main__":
    unittest.main()
