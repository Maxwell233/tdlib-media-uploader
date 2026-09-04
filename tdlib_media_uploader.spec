# -*- mode: python ; coding: utf-8 -*-

"""PyInstaller one-folder build for the Windows PySide6 application."""

from pathlib import Path

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
        if path.suffix.lower() == ".exe" and "ffmpeg" in path.name.lower():
            return True
    return False


datas = [
    (str(PROJECT_DIR / "config.example.toml"), "."),
    (str(PROJECT_DIR / "VERSION"), "."),
    (str(PROJECT_DIR / "LICENSE"), "."),
    (str(PROJECT_DIR / "ATTRIBUTION"), "."),
    (str(PROJECT_DIR / "THIRD_PARTY_LICENSES.md"), "."),
    (str(PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.ico"), "assets"),
    (str(PROJECT_DIR / "tools" / "README.txt"), "tools"),
]
binaries = []

packaged_ffmpeg = PROJECT_DIR / "tools" / "ffmpeg" / "ffmpeg.exe"
packaged_ffmpeg_license = PROJECT_DIR / "tools" / "ffmpeg" / "LICENSE.txt"
if packaged_ffmpeg.is_file():
    binaries.append((str(packaged_ffmpeg), "tools/ffmpeg"))
if packaged_ffmpeg_license.is_file():
    datas.append((str(packaged_ffmpeg_license), "tools/ffmpeg"))

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
