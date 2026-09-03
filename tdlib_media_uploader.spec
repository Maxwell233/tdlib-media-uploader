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


datas = [
    (str(PROJECT_DIR / "config.example.toml"), "."),
    (str(PROJECT_DIR / "VERSION"), "."),
    (str(PROJECT_DIR / "COPYRIGHT"), "."),
    (str(PROJECT_DIR / "tools" / "README.txt"), "tools"),
]
binaries = []
hiddenimports = [
    "app_config",
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
    datas.extend(package_datas)
    binaries.extend(package_binaries)
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
