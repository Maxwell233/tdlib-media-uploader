#!/usr/bin/env bash
set -u

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if ! "$PROJECT_DIR/run.sh"; then
    echo
    echo "GUI 启动失败，请查看上面的错误信息。"
    read -r -p "按 Enter 键关闭此窗口。" _
    exit 1
fi
