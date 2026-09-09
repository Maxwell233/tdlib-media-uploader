#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "macOS 构建必须在 Apple Silicon arm64 runner 或 Mac 上执行。" >&2
    exit 1
fi

VERSION="$(tr -d '[:space:]' < VERSION)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_VENV="$PROJECT_DIR/.build_venv"
BUILD_PYTHON="$BUILD_VENV/bin/python"
BUILD_DIR="$PROJECT_DIR/build"
DIST_DIR="$PROJECT_DIR/dist"
FFMPEG_VERSION="${FFMPEG_VERSION:-7.1.1}"
FFMPEG_ARCHIVE="ffmpeg-${FFMPEG_VERSION}.tar.xz"
FFMPEG_URL="https://ffmpeg.org/releases/${FFMPEG_ARCHIVE}"
FFMPEG_SOURCE_SHA256="733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
FFMPEG_WORK_DIR="$BUILD_DIR/ffmpeg-${FFMPEG_VERSION}"
FFMPEG_STAGE_DIR="$PROJECT_DIR/tools/ffmpeg"

for argument in "$@"; do
    if [[ "$argument" == "--clean" ]]; then
        rm -rf "$BUILD_DIR" "$DIST_DIR"
    fi
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 && [[ ! -x "$PYTHON_BIN" ]]; then
    echo "找不到 Python 3.13。请安装 Python 3.13 或设置 PYTHON_BIN。" >&2
    exit 1
fi
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else "需要 Python 3.13 或更高版本")'

if [[ ! -x "$BUILD_PYTHON" ]]; then
    echo "→ 创建构建虚拟环境 .build_venv"
    "$PYTHON_BIN" -m venv "$BUILD_VENV"
fi

skip_install=false
for argument in "$@"; do
    if [[ "$argument" == "--skip-install" ]]; then
        skip_install=true
    fi
done
if [[ "$skip_install" != true ]]; then
    echo "→ 安装运行与构建依赖"
    "$BUILD_PYTHON" -m pip install --upgrade pip
    "$BUILD_PYTHON" -m pip install --no-cache-dir --upgrade --force-reinstall \
        --no-binary imageio-ffmpeg -r requirements-build.txt
fi

ensure_lgpl_ffmpeg() {
    local ffmpeg_path="$FFMPEG_STAGE_DIR/ffmpeg"
    local license_path="$FFMPEG_STAGE_DIR/LICENSE.txt"
    local version_output=""

    if [[ -x "$ffmpeg_path" && -f "$license_path" ]] && grep -q "GNU LESSER GENERAL PUBLIC LICENSE" "$license_path"; then
        version_output="$("$ffmpeg_path" -version 2>&1 || true)"
        if [[ "$version_output" != *"--enable-gpl"* && "$version_output" != *"--enable-nonfree"* && \
              "$(file -b "$ffmpeg_path")" == *"arm64"* ]]; then
            echo "✓ 复用已验证的 Apple Silicon LGPL FFmpeg"
            return
        fi
    fi

    local cache_dir="$BUILD_DIR/ffmpeg-cache"
    local archive_path="$cache_dir/$FFMPEG_ARCHIVE"
    mkdir -p "$cache_dir" "$FFMPEG_WORK_DIR"
    if [[ -f "$archive_path" ]]; then
        local cached_hash
        cached_hash="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
        if [[ "$cached_hash" != "$FFMPEG_SOURCE_SHA256" ]]; then
            rm -f "$archive_path"
        fi
    fi
    if [[ ! -f "$archive_path" ]]; then
        echo "→ 下载 FFmpeg ${FFMPEG_VERSION} 源码"
        curl --fail --location --retry 3 --retry-delay 2 --output "$archive_path" "$FFMPEG_URL"
    fi
    local archive_hash
    archive_hash="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
    if [[ "$archive_hash" != "$FFMPEG_SOURCE_SHA256" ]]; then
        echo "FFmpeg 源码归档 SHA-256 校验失败：$archive_hash" >&2
        exit 1
    fi

    rm -rf "$FFMPEG_WORK_DIR"
    mkdir -p "$FFMPEG_WORK_DIR"
    tar -xJf "$archive_path" -C "$FFMPEG_WORK_DIR"
    local source_dir="$FFMPEG_WORK_DIR/ffmpeg-${FFMPEG_VERSION}"
    if [[ ! -d "$source_dir" ]]; then
        echo "FFmpeg 源码目录结构异常：$source_dir" >&2
        exit 1
    fi

    echo "→ 编译不含 GPL/nonfree 的 LGPL FFmpeg（仅 arm64）"
    local install_dir="$FFMPEG_WORK_DIR/install"
    mkdir -p "$install_dir"
    (
        cd "$source_dir"
        ./configure \
            --prefix="$install_dir" \
            --disable-debug \
            --disable-doc \
            --disable-ffplay \
            --disable-ffprobe \
            --disable-gpl \
            --disable-nonfree \
            --enable-version3 \
            --enable-static \
            --disable-shared
        make -j"$(sysctl -n hw.ncpu)"
        make install
    )

    mkdir -p "$FFMPEG_STAGE_DIR"
    if ! grep -q "GNU LESSER GENERAL PUBLIC LICENSE" "$source_dir/COPYING.LGPLv3"; then
        echo "FFmpeg 源码缺少预期的 LGPLv3 许可文本。" >&2
        exit 1
    fi
    cp "$install_dir/bin/ffmpeg" "$ffmpeg_path"
    cp "$source_dir/COPYING.LGPLv3" "$license_path"
    cat > "$FFMPEG_STAGE_DIR/BUILD_INFO.txt" <<EOF
FFmpeg version: ${FFMPEG_VERSION}
Source archive: ${FFMPEG_URL}
Source archive SHA-256: $(shasum -a 256 "$archive_path" | awk '{print $1}')
Target architecture: macOS arm64
Configure: --disable-gpl --disable-nonfree --enable-version3 --enable-static --disable-shared
License file: COPYING.LGPLv3 from the source archive
EOF
}

make_icon() {
    local iconset="$BUILD_DIR/tdlib_media_uploader.iconset"
    local icon_path="$BUILD_DIR/tdlib_media_uploader_icon.icns"
    rm -rf "$iconset"
    mkdir -p "$iconset"
    for size in 16 32 128 256 512; do
        sips -z "$size" "$size" "$PROJECT_DIR/assets/tdlib_media_uploader_icon.png" \
            --out "$iconset/icon_${size}x${size}.png" >/dev/null
        sips -z "$((size * 2))" "$((size * 2))" "$PROJECT_DIR/assets/tdlib_media_uploader_icon.png" \
            --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
    done
    if ! iconutil -c icns "$iconset" -o "$icon_path" >/dev/null 2>&1; then
        echo "提示：系统 iconutil 无法生成 .icns，将使用应用内 PNG 图标。" >&2
        return 0
    fi
    printf '%s\n' "$icon_path"
}

ensure_lgpl_ffmpeg
ICON_PATH="$(make_icon)"

echo "→ PyInstaller 生成 macOS .app"
export PYINSTALLER_CONFIG_DIR="$BUILD_DIR/pyinstaller-config"
TDLIB_MACOS_ICON_PATH="$ICON_PATH" "$BUILD_PYTHON" -m PyInstaller \
    --noconfirm --clean --distpath "$DIST_DIR" --workpath "$BUILD_DIR/pyinstaller" \
    "$PROJECT_DIR/tdlib_media_uploader.spec"

APP_PATH="$DIST_DIR/TDLib Media Uploader.app"
APP_EXECUTABLE="$APP_PATH/Contents/MacOS/TDLib Media Uploader"
if [[ ! -x "$APP_EXECUTABLE" ]]; then
    echo "构建完成但没有找到 macOS 应用：$APP_EXECUTABLE" >&2
    exit 1
fi

if [[ "$(file -b "$APP_EXECUTABLE")" != *"arm64"* ]]; then
    echo "应用不是 arm64：$(file -b "$APP_EXECUTABLE")" >&2
    exit 1
fi

PACKAGED_FFMPEG="$(find "$APP_PATH" -type f -name ffmpeg -perm -111 -print -quit)"
if [[ -z "$PACKAGED_FFMPEG" ]]; then
    echo "应用内没有找到 LGPL FFmpeg。" >&2
    exit 1
fi
FFMPEG_OUTPUT="$("$PACKAGED_FFMPEG" -version 2>&1 || true)"
if [[ "$FFMPEG_OUTPUT" == *"--enable-gpl"* || "$FFMPEG_OUTPUT" == *"--enable-nonfree"* ]]; then
    echo "应用内 FFmpeg 含 GPL/nonfree 构建标志。" >&2
    exit 1
fi
if [[ "$(file -b "$PACKAGED_FFMPEG")" != *"arm64"* ]]; then
    echo "应用内 FFmpeg 不是 arm64：$(file -b "$PACKAGED_FFMPEG")" >&2
    exit 1
fi

for notice in LICENSE ATTRIBUTION THIRD_PARTY_LICENSES.md; do
    if [[ ! -f "$APP_PATH/Contents/Resources/$notice" && ! -f "$APP_PATH/$notice" ]]; then
        echo "应用缺少许可/署名文件：$notice" >&2
        exit 1
    fi
done

PACKAGE_NAME="TDLib Media Uploader-v${VERSION}-macos-arm64"
DMG_STAGE_DIR="$BUILD_DIR/dmg/$PACKAGE_NAME"
DMG_PATH="$DIST_DIR/${PACKAGE_NAME}.dmg"
rm -rf "$DMG_STAGE_DIR" "$DMG_PATH" "$DMG_PATH.sha256"
mkdir -p "$DMG_STAGE_DIR"
cp -R "$APP_PATH" "$DMG_STAGE_DIR/"
# A real Applications alias makes the first-run drag-and-drop action obvious
# and avoids asking users to discover the system folder themselves.
ln -s /Applications "$DMG_STAGE_DIR/Applications"
for notice in LICENSE ATTRIBUTION THIRD_PARTY_LICENSES.md; do
    cp "$PROJECT_DIR/$notice" "$DMG_STAGE_DIR/"
done
if [[ -f "$FFMPEG_STAGE_DIR/BUILD_INFO.txt" ]]; then
    cp "$FFMPEG_STAGE_DIR/BUILD_INFO.txt" "$DMG_STAGE_DIR/FFMPEG_BUILD_INFO.txt"
fi

if [[ ! -L "$DMG_STAGE_DIR/Applications" ]]; then
    echo "DMG 暂存目录缺少 Applications 文件夹别名。" >&2
    exit 1
fi
hdiutil create \
    -volname "TDLib Media Uploader V${VERSION}" \
    -srcfolder "$DMG_STAGE_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH" >/dev/null
(
    cd "$DIST_DIR"
    shasum -a 256 "$(basename "$DMG_PATH")" > "$(basename "$DMG_PATH").sha256"
)

echo "✓ macOS arm64 .app 构建完成：$APP_PATH"
echo "✓ macOS arm64 DMG：$DMG_PATH"
echo "✓ SHA-256：$DMG_PATH.sha256"
if ! codesign -dv "$APP_PATH" >/dev/null 2>&1; then
    echo "提示：该构建未签名/未公证，首次打开可能需要右键点按“打开”。"
fi
