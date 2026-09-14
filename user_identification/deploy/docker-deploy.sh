#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   bash deploy/docker-deploy.sh              # CPU build (x86_64 / aarch64)
#   bash deploy/docker-deploy.sh --gpu        # GPU build (x86_64 / aarch64)
#   bash deploy/docker-deploy.sh --build-only # only build, don't start
#   bash deploy/docker-deploy.sh --gpu --network-host  # GPU + host network (Docker DNS broken)

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

GPU_MODE=false
BUILD_ONLY=false
NETWORK_HOST=false

for arg in "$@"; do
    case "$arg" in
        --gpu) GPU_MODE=true ;;
        --build-only) BUILD_ONLY=true ;;
        --network-host) NETWORK_HOST=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# ── 1. Check Docker ──
if ! command -v docker &>/dev/null; then
    echo "!!! Docker not found, please install Docker first"
    exit 1
fi

# ── 2. .env ──
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo ">>> .env created from .env.example — please edit it with your credentials"
    else
        echo "!!! .env.example not found, please create .env manually"
        exit 1
    fi
fi

# ── 3. Build ──
COMPOSE_FILES="-f docker-compose.yml"
if [ "$GPU_MODE" = true ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.gpu.yml"
    echo ">>> Building Docker image with GPU support..."
    echo "    x86_64 → onnxruntime-gpu (pip) | aarch64 → onnxruntime-gpu (.whl)"
else
    echo ">>> Building Docker image (CPU, all architectures)..."
fi

if [ "$NETWORK_HOST" = true ]; then
    echo ">>> Using --network host (Docker DNS bypass)"
    BUILD_ARG=""
    [ "$GPU_MODE" = true ] && BUILD_ARG="--build-arg ONNXRUNTIME_MODE=gpu"
    docker build --network host $BUILD_ARG -t user-identification:latest .
else
    docker compose $COMPOSE_FILES build
fi

# ── 4. Start ──
if [ "$BUILD_ONLY" = true ]; then
    echo ">>> Build complete (--build-only, skipping start)"
    exit 0
fi

echo ">>> Starting container..."
docker compose $COMPOSE_FILES up -d

echo ">>> Service running at http://0.0.0.0:8001"
echo ">>> Check health: curl http://localhost:8001/api/health"