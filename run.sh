#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "尚未完成环境安装，请先运行 ./setup.sh 或双击 setup.command。" >&2
    exit 1
fi

if [[ ! -f config.toml ]]; then
    cp config.example.toml config.toml
    echo "已根据 config.example.toml 创建 config.toml；请在 GUI 设置页填写信息。"
fi

exec "$VENV_PYTHON" "$PROJECT_DIR/gui_app.py"
