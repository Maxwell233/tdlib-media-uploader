# -*- mode: python ; coding: utf-8 -*-

"""PyInstaller one-folder build for the Windows and macOS PySide6 application."""

import os
import sys
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE
from PyInstaller.building.osx import BUNDLE
from PyInstaller.utils.hooks import collect_all, collect_submodules


PROJECT_DIR = Path(SPEC).resolve().parent
SRC_DIR = PROJECT_DIR / "src"
PACKAGE_NAME = "tdlib_media_uploader"
PACKAGE_DIR = SRC_DIR / PACKAGE_NAME


def collect_package(name: str):
    try:
        return collect_all(name)
    except Exception:
        # Some packages (notably a single-file native extension) do not expose
        # package metadata to collect_all.  The explicit hidden import below
        # still lets Analysis include the module in that case.
        return [], [], []


def is_embedded_ffmpeg(item) -> bool:
    """Exclude imageio-ffmpeg's wheel binary from the portable build.

    The Python wrapper remains useful, but the wheel's bundled executable is
    not trusted for redistribution because its codec build flags can enable
    GPL components.  build_exe.ps1 stages a separately verified LGPL build.
    """

    for value in item[:2]:
        path = Path(str(value))
        parts = {part.lower() for part in path.parts}
        if "ffmpeg" in path.name.lower() and (
            path.suffix.lower() == ".exe"
            or "binaries" in parts
            or "imageio_ffmpeg" in parts
        ):
            return True
    return False


RESOURCE_DATA = (
    (PROJECT_DIR / "resources" / "default_config.toml", "resources"),
    (PROJECT_DIR / "VERSION", "."),
    (PROJECT_DIR / "LICENSE", "."),
    (PROJECT_DIR / "ATTRIBUTION", "."),
    (PROJECT_DIR / "THIRD_PARTY_LICENSES.md", "."),
    (PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.ico", "assets"),
    (PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.png", "assets"),
    (PROJECT_DIR / "tools" / "README.txt", "tools"),
)

datas = []
for source, destination in RESOURCE_DATA:
    if source.is_file():
        datas.append((str(source), destination))
binaries = []

packaged_ffmpeg = PROJECT_DIR / "tools" / "ffmpeg" / (
    "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
)
packaged_ffmpeg_license = PROJECT_DIR / "tools" / "ffmpeg" / "LICENSE.txt"
if packaged_ffmpeg.is_file():
    binaries.append((str(packaged_ffmpeg), "tools/ffmpeg"))
if packaged_ffmpeg_license.is_file():
    datas.append((str(packaged_ffmpeg_license), "tools/ffmpeg"))
ffmpeg_build_info = PROJECT_DIR / "tools" / "ffmpeg" / "BUILD_INFO.txt"
if ffmpeg_build_info.is_file():
    datas.append((str(ffmpeg_build_info), "tools/ffmpeg"))

# Keep the package's public modules explicit for deterministic builds.
# ``collect_submodules`` below also covers packages added during the staged V2
# migration without requiring another spec change for every new module.
PACKAGE_HIDDENIMPORTS = [
    "tdlib_media_uploader",
    "tdlib_media_uploader.app",
    "tdlib_media_uploader.contracts",
    "tdlib_media_uploader.config",
    "tdlib_media_uploader.config.model",
    "tdlib_media_uploader.config.loader",
    "tdlib_media_uploader.config.paths",
    "tdlib_media_uploader.core",
    "tdlib_media_uploader.core.models",
    "tdlib_media_uploader.core.sorting",
    "tdlib_media_uploader.core.filesystem",
    "tdlib_media_uploader.core.filesystem_legacy",
    "tdlib_media_uploader.core.readiness",
    "tdlib_media_uploader.core.concurrency",
    "tdlib_media_uploader.core.album",
    "tdlib_media_uploader.core.identity",
    "tdlib_media_uploader.core.upload_state",
    "tdlib_media_uploader.core.upload_journal",
    "tdlib_media_uploader.core.logging",
    "tdlib_media_uploader.core.instance_lock",
    "tdlib_media_uploader.core.self_test",
    "tdlib_media_uploader.gui",
    "tdlib_media_uploader.gui.application",
    "tdlib_media_uploader.gui.main_window",
    "tdlib_media_uploader.gui.events",
    "tdlib_media_uploader.gui.integration",
    "tdlib_media_uploader.gui.models",
    "tdlib_media_uploader.gui.workers",
    "tdlib_media_uploader.gui.pages",
    "tdlib_media_uploader.gui.pages.home",
    "tdlib_media_uploader.gui.pages.task",
    "tdlib_media_uploader.gui.pages.upload",
    "tdlib_media_uploader.gui.pages.video",
    "tdlib_media_uploader.gui.pages.image",
    "tdlib_media_uploader.gui.pages.mixed",
    "tdlib_media_uploader.media",
    "tdlib_media_uploader.media.image",
    "tdlib_media_uploader.media.mixed",
    "tdlib_media_uploader.media.video",
    "tdlib_media_uploader.media.legacy_image",
    "tdlib_media_uploader.media.legacy_mixed",
    "tdlib_media_uploader.media.legacy_video",
    "tdlib_media_uploader.processes",
    "tdlib_media_uploader.processes.runner",
    "tdlib_media_uploader.telegram",
    "tdlib_media_uploader.telegram.client",
    "tdlib_media_uploader.telegram.auth",
    "tdlib_media_uploader.telegram.target",
    "tdlib_media_uploader.telegram.limits",
    "tdlib_media_uploader.telegram.send_result",
    "tdlib_media_uploader.telegram.tdlib_common",
    "tdlib_media_uploader.upload",
    "tdlib_media_uploader.upload.engine",
    "tdlib_media_uploader.upload.planner",
    "tdlib_media_uploader.upload.preflight",
    "tdlib_media_uploader.upload.staging",
]

hiddenimports = [
    "tdjson",
    "PIL",
    "imageio_ffmpeg",
]
hiddenimports.extend(PACKAGE_HIDDENIMPORTS)
try:
    hiddenimports.extend(collect_submodules(PACKAGE_NAME))
except Exception:
    # The explicit package list above keeps the current build usable if a
    # future optional package cannot be enumerated by PyInstaller's hook.
    pass

for package_name in ("tdjson", "imageio_ffmpeg"):
    package_datas, package_binaries, package_hiddenimports = collect_package(package_name)
    datas.extend(item for item in package_datas if not is_embedded_ffmpeg(item))
    binaries.extend(item for item in package_binaries if not is_embedded_ffmpeg(item))
    hiddenimports.extend(package_hiddenimports)


a = Analysis(
    [str(PACKAGE_DIR / "app.py")],
    pathex=[str(SRC_DIR), str(PROJECT_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

is_macos = sys.platform == "darwin"
if is_macos:
    mac_icon = Path(os.environ.get("TDLIB_MACOS_ICON_PATH", ""))
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="TDLib Media Uploader",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=str(mac_icon) if mac_icon.is_file() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        a.zipfiles,
        strip=False,
        upx=False,
        name="TDLib Media Uploader",
    )
    app = BUNDLE(
        coll,
        name="TDLib Media Uploader.app",
        icon=str(mac_icon) if mac_icon.is_file() else None,
        bundle_identifier="com.maxwell233.tdlib-media-uploader",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="TDLib Media Uploader",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=str(PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.ico"),
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        a.zipfiles,
        strip=False,
        upx=False,
        name="TDLib Media Uploader",
    )
