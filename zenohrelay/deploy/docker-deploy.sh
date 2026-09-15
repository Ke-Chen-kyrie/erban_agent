#!/usr/bin/env bash
set -euo pipefail

# ZenohRelay 容器部署脚本。
#
# 用法：
#   bash deploy/docker-deploy.sh               # 构建 + 后台启动
#   bash deploy/docker-deploy.sh --build-only  # 只构建镜像
#   bash deploy/docker-deploy.sh --down        # 停止并删除容器
#
# 说明：
#   - 基础镜像 python:3.11-slim，x86_64 / aarch64 都能构建（本仓库 uv.lock 已锁定多平台依赖）。
#   - 容器 --network host 与宿主机共享网络，.env 里的 127.0.0.1:8001（身份识别）、
#     Foxglove/rosbridge 地址、以及 Zenoh multicast 自动发现都原样可达。
#   - restart: unless-stopped + systemctl enable docker → 开机自启动。

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

BUILD_ONLY=false
DOWN=false

for arg in "$@"; do
    case "$arg" in
        --build-only) BUILD_ONLY=true ;;
        --down) DOWN=true ;;
        *) echo "未知选项: $arg"; echo "用法: bash deploy/docker-deploy.sh [--build-only] [--down]"; exit 1 ;;
    esac
done

# ── 1. 检查 Docker ──
if ! command -v docker &>/dev/null; then
    echo "!!! 未找到 docker 命令，请先安装 Docker"
    exit 1
fi
if ! docker info &>/dev/null; then
    echo "!!! Docker daemon 未运行，请先启动 Docker"
    exit 1
fi

# ── 2. .env ──
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo ">>> .env 已从 .env.example 创建，请编辑 VIDEO_SOURCE 等配置"
    else
        echo "!!! 缺少 .env.example，请手动创建 .env"
        exit 1
    fi
fi

# ── 3. 停止 ──
if [ "$DOWN" = true ]; then
    echo ">>> 停止并删除容器（docker compose down）..."
    docker compose down
    exit 0
fi

# ── 4. 构建 ──
echo ">>> 构建镜像 zenoh-relay:latest ..."
# --network host：目标机器 Docker DNS 可能异常，构建时直接走宿主网络
docker build --network host -t zenoh-relay:latest .

if [ "$BUILD_ONLY" = true ]; then
    echo ">>> 镜像构建完成（--build-only，未启动容器）"
    exit 0
fi

# ── 5. 启动 ──
echo ">>> 启动容器（restart: unless-stopped）..."
docker compose up -d

echo ""
echo ">>> 完成。日志: docker compose logs -f"
echo ">>> 停止: bash deploy/docker-deploy.sh --down"
echo ">>> 暂时停用（保留容器）: docker compose stop"