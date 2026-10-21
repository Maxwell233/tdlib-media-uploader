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


def is_embedded_ffmpeg(item) -> bool:
    """Exclude imageio-ffmpeg's wheel binary from the portable build.

    The Python wrapper remains useful, but the wheel's bundled executable is
    not trusted for redistribution because its codec build flags can enable
    GPL components.  The platform workflow stages a separately verified LGPL
    build before invoking this spec.
    """

    # Inspect the source file, since a destination directory can itself be
    # named imageio_ffmpeg and says nothing about whether this is executable.
    path = Path(str(item[0]))
    name = path.name.lower()
    return "imageio_ffmpeg" in {part.lower() for part in path.parts} and (
        name in {"ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"}
        or name.startswith(("ffmpeg-", "ffprobe-"))
    )


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
packaged_ffprobe = PROJECT_DIR / "tools" / "ffmpeg" / (
    "ffprobe.exe" if os.name == "nt" else "ffprobe"
)
packaged_ffmpeg_license = PROJECT_DIR / "tools" / "ffmpeg" / "LICENSE.txt"
if packaged_ffmpeg.is_file():
    binaries.append((str(packaged_ffmpeg), "tools/ffmpeg"))
if packaged_ffprobe.is_file():
    binaries.append((str(packaged_ffprobe), "tools/ffmpeg"))
if packaged_ffmpeg_license.is_file():
    datas.append((str(packaged_ffmpeg_license), "tools/ffmpeg"))
ffmpeg_build_info = PROJECT_DIR / "tools" / "ffmpeg" / "BUILD_INFO.txt"
if ffmpeg_build_info.is_file():
    datas.append((str(ffmpeg_build_info), "tools/ffmpeg"))

# Include dynamic GUI/media imports without maintaining a second module list.
hiddenimports = [
    "tdjson",
    "PIL",
    "imageio_ffmpeg",
    *collect_submodules(PACKAGE_NAME),
]

for package_name in ("tdjson", "imageio_ffmpeg"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
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
