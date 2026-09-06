# -*- mode: python ; coding: utf-8 -*-

"""PyInstaller one-folder build for the Windows and macOS PySide6 application."""

import os
import sys
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE
from PyInstaller.building.osx import BUNDLE
from PyInstaller.utils.hooks import collect_all


PROJECT_DIR = Path(SPEC).resolve().parent


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


datas = []
for source, destination in [
    (PROJECT_DIR / "config.example.toml", "."),
    (PROJECT_DIR / "VERSION", "."),
    (PROJECT_DIR / "LICENSE", "."),
    (PROJECT_DIR / "ATTRIBUTION", "."),
    (PROJECT_DIR / "THIRD_PARTY_LICENSES.md", "."),
    (PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.ico", "assets"),
    (PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.png", "assets"),
    (PROJECT_DIR / "tools" / "README.txt", "tools"),
]:
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

hiddenimports = [
    "app_config",
    "album_metadata",
    "path_utils",
    "tdlib_common",
    "tdlib_image_album_uploader",
    "tdlib_video_album_uploader",
    "tdlib_video_app",
    "tdjson",
    "PIL",
    "imageio_ffmpeg",
]

for package_name in ("tdjson", "imageio_ffmpeg"):
    package_datas, package_binaries, package_hiddenimports = collect_package(package_name)
    datas.extend(item for item in package_datas if not is_embedded_ffmpeg(item))
    binaries.extend(item for item in package_binaries if not is_embedded_ffmpeg(item))
    hiddenimports.extend(package_hiddenimports)


a = Analysis(
    [str(PROJECT_DIR / "gui_app.py")],
    pathex=[str(PROJECT_DIR)],
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
