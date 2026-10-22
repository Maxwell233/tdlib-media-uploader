"""Read-only Windows data-directory ACL diagnostics; never changes permissions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import NamedTuple


class PermissionDiagnostic(NamedTuple):
    status: str
    text: str
    detail: str


# Ask Windows for numeric SIDs so the result is independent of display language.
# ReadData is also ListDirectory. Inherited and inheritance-only read grants
# matter because new application data normally inherits this directory's ACL.
_ACL_COMMAND = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:TDLIB_DATA_PERMISSION_PATH
$descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(), 0)
$readers = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
    Where-Object { $_.AccessControlType -eq 'Allow' -and ([int]$_.FileSystemRights -band 1) -ne 0 } |
    ForEach-Object { $_.IdentityReference.Value })
[pscustomobject]@{
    current_user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    has_dacl = $null -ne $descriptor.DiscretionaryAcl
    readers = $readers
} | ConvertTo-Json -Compress
"""

_SCOPE = "仅检查 data 目录的读取授权，不扫描子文件或更改权限。"
_UNAVAILABLE = PermissionDiagnostic("muted", "无法检查", _SCOPE)


def check_data_directory_permissions(directory: Path) -> PermissionDiagnostic:
    """Report other-account read grants without blocking startup on failure.

    SYSTEM and the local Administrators group are expected Windows principals.
    The result describes ACL grants, not an effective-access audit of individual
    accounts, deny rules, network shares or existing children with separate ACLs.
    """

    if sys.platform != "win32":
        return PermissionDiagnostic("muted", "不适用", "Windows 目录权限检测")
    environment = os.environ.copy()
    environment["TDLIB_DATA_PERMISSION_PATH"] = str(Path(directory).absolute())
    powershell = Path(environment.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _ACL_COMMAND],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3, check=True, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        acl = json.loads(result.stdout)
        current_user = acl["current_user"]
        readers = acl["readers"]
        if not isinstance(current_user, str) or not isinstance(readers, list):
            return _UNAVAILABLE
        trusted = {current_user, "S-1-5-18", "S-1-5-32-544"}
        other_readers = set(readers) - trusted
        if acl["has_dacl"] is False or other_readers:
            return PermissionDiagnostic(
                "warn", "有其他账户的读取授权",
                "可在 data 文件夹的“属性 → 安全”中检查，或将便携版放到个人用户目录。" + _SCOPE,
            )
        if acl["has_dacl"] is not True or not readers:
            return _UNAVAILABLE
        return PermissionDiagnostic(
            "ok", "目录读取授权受限", "读取授权仅涉及当前账户、系统或管理员。" + _SCOPE,
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return _UNAVAILABLE


__all__ = ["PermissionDiagnostic", "check_data_directory_permissions"]
