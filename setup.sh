#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 && [[ ! -x "$PYTHON_BIN" ]]; then
    echo "找不到 Python 3.13。请安装 Python 3.13 或设置 PYTHON_BIN。" >&2
    exit 1
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else "需要 Python 3.13 或更高版本")'

if [[ ! -x ".venv/bin/python" ]]; then
    echo "→ 创建 Python 虚拟环境 .venv"
    "$PYTHON_BIN" -m venv .venv
else
    echo "✓ 已存在 .venv，继续使用。"
fi

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "虚拟环境创建失败：$VENV_PYTHON" >&2
    exit 1
fi

echo "→ 更新 pip 并安装项目依赖"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --no-cache-dir --upgrade --force-reinstall \
    --no-binary imageio-ffmpeg -r requirements.txt

if [[ ! -f config.toml ]]; then
    cp config.example.toml config.toml
    echo "✓ 已从 config.example.toml 创建 config.toml。"
else
    echo "✓ 保留现有 config.toml，不会覆盖你的配置。"
fi

cat <<'EOF'

安装完成：
  1. 双击 setup.command 或运行 ./setup.sh 安装环境；
  2. 双击 run.command 或运行 ./run.sh 启动 GUI；
  3. 首次启动后，在“设置与诊断”填写 Telegram API、目标和目录。

说明：源码运行视频封面需要 LGPL FFmpeg（放入 tools/ffmpeg/ffmpeg 或加入 PATH）；
没有 ExifTool 时，默认会使用文件修改时间作为视频日期。
EOF
