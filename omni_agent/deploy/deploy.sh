#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== 检查系统依赖 ==="
missing=""
for dep in alsa-utils ffmpeg portaudio19-dev; do
    if ! dpkg -s "$dep" &>/dev/null; then
        missing="$missing $dep"
    fi
done
if [ -n "$missing" ]; then
    echo "缺少系统依赖:$missing"
    echo "请手动安装: sudo apt install -y$missing"
    exit 1
fi

echo "=== 安装 uv ==="
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "=== 创建环境 ==="
cd "$PROJECT_DIR"
uv venv --python 3.12
uv pip install -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple

echo "=== 配置 .env ==="
if [ ! -f .env ]; then
    cp .env.example .env
    echo "请编辑 $PROJECT_DIR/.env 填入 API Key 后重新运行此脚本"
    exit 1
fi

echo "=== 部署完成，启动应用 ==="
cd "$PROJECT_DIR"
exec uv run python run.py
