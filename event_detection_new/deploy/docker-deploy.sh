#!/usr/bin/env bash
set -euo pipefail

# 事件检测容器部署脚本。
#
# 用法：
#   bash deploy/docker-deploy.sh              # 构建镜像 + 创建容器（保持停止，交给大屏一键启动）
#   bash deploy/docker-deploy.sh --build-only # 只构建镜像，不创建容器
#   bash deploy/docker-deploy.sh --run        # 构建 + 创建并立即启动容器
#
# 说明：
#   - 容器「不开机自启」：默认只创建（docker create），大屏「事件检测」按钮负责
#     docker start / docker stop。
#   - 容器使用 --network host，与宿主机共享网络；镜像构建也走 --network host
#     （目标机器 Docker DNS 可能异常，与 user_identification 同策略）。
#   - 创建后如需查看日志：docker logs event-detection

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

IMAGE_NAME="event-detection:latest"
CONTAINER_NAME="event-detection"
MONITOR_CONTAINER_NAME="agent-monitor-server"
BUILD_ONLY=false
START_NOW=false

for arg in "$@"; do
    case "$arg" in
        --build-only) BUILD_ONLY=true ;;
        --run) START_NOW=true ;;
        *) echo "未知选项: $arg"; echo "用法: bash deploy/docker-deploy.sh [--build-only] [--run]"; exit 1 ;;
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
    echo "!!! 缺少 .env，请先基于 event_detection_new/.env 配置运行参数"
    exit 1
fi

# ── 3. 构建镜像 ──
echo ">>> 构建镜像 $IMAGE_NAME ..."
docker build --network host -t "$IMAGE_NAME" .
if [ "$BUILD_ONLY" = true ]; then
    echo ">>> 镜像构建完成（--build-only，未创建容器）"
    exit 0
fi

# ── 4. 创建容器（保持停止，由大屏一键启动控制）──
# 用 `docker container inspect` 只检查容器；`docker inspect` 会命中同名镜像导致误判。
if docker container inspect "$CONTAINER_NAME" &>/dev/null; then
    echo ">>> 容器 $CONTAINER_NAME 已存在，先删除再重建（保证配置最新）"
    docker rm -f "$CONTAINER_NAME" >/dev/null
fi
if docker container inspect "$MONITOR_CONTAINER_NAME" &>/dev/null; then
    echo ">>> 容器 $MONITOR_CONTAINER_NAME 已存在，先删除再重建（保证配置最新）"
    docker rm -f "$MONITOR_CONTAINER_NAME" >/dev/null
fi
echo ">>> 创建容器 $CONTAINER_NAME（--network host，env_file 注入 .env）..."
# 仅 VIDEO_SOURCE=camera 时才把宿主机 /dev/video0 映射进容器，否则容器里
# VideoCapture(0) 打不开设备、进程直接退出。其它模式（foxglove/rosbridge/zenoh）
# 不需要摄像头，硬映射会让 docker 在设备不可见时报
# "error gathering device information ... no such file or directory" 而无法启动。
# CAMERA_ID 变了就改这个设备节点。
DEVICE_ARGS=()
if grep -qiE '^VIDEO_SOURCE *= *camera' .env; then
    DEVICE_ARGS+=(--device /dev/video0:/dev/video0)
fi
docker create \
    --name "$CONTAINER_NAME" \
    --network host \
    --env-file .env \
    "${DEVICE_ARGS[@]}" \
    -v "$PROJECT_DIR/logs:/app/logs" \
    -v "$PROJECT_DIR/result:/app/result" \
    -v "$PROJECT_DIR/certs:/app/certs" \
    "$IMAGE_NAME"

echo ">>> 创建容器 $MONITOR_CONTAINER_NAME（与事件检测共用镜像和 .env）..."
docker create \
    --name "$MONITOR_CONTAINER_NAME" \
    --network host \
    --env-file .env \
    -v "$PROJECT_DIR/logs:/app/logs" \
    -v "$PROJECT_DIR/result:/app/result" \
    -v "$PROJECT_DIR/certs:/app/certs" \
    "$IMAGE_NAME" \
    /app/.venv/bin/python -m agent_monitor_server

if [ "$START_NOW" = true ]; then
    echo ">>> 启动容器 ..."
    docker start "$CONTAINER_NAME"
    docker start "$MONITOR_CONTAINER_NAME"
fi

echo ""
echo ">>> 完成。容器已创建（未启动），可在 erban_dashboard 大屏点击「事件检测」启动，"
echo ">>> 或手动执行: docker start $CONTAINER_NAME"
echo ">>> 动作监测 Server: docker start $MONITOR_CONTAINER_NAME"
echo ">>> 查看日志: docker logs -f $CONTAINER_NAME"
echo ">>> 停止: docker stop $CONTAINER_NAME"
