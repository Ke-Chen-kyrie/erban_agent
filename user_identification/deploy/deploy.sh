#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   bash deploy/deploy.sh                    # local deployment (server + all voiceprint SDKs)
#   INSTALL_CLIENT=1 bash deploy/deploy.sh   # + test client deps (camera/mic)
#
# For Docker deployment, use: bash deploy/docker-deploy.sh

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# ── 1. Check uv ──
if ! command -v uv &>/dev/null; then
    echo ">>> uv not found, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1091
    source "$HOME/.local/env" 2>/dev/null || source "$HOME/.cargo/env" 2>/dev/null || true
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    echo ">>> uv installed: $(uv --version)"
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

# ── 3. Create venv & install all dependencies ──
_PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
if [ -d .venv ]; then
    _VENV_PYVER=$(source .venv/bin/activate 2>/dev/null && python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    if [ "$_VENV_PYVER" != "3.10" ]; then
        echo ">>> .venv Python version is $_VENV_PYVER, expected 3.10 — recreating"
        rm -rf .venv
    fi
fi
if [ ! -d .venv ]; then
    uv venv --python 3.10
fi
source .venv/bin/activate
export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
uv pip install -r requirements.txt

# ── 3.1 Auto-detect onnxruntime GPU support ──
_ARCH=$(uname -m)
_CUDA_OK=0
if command -v nvidia-smi &>/dev/null; then
    _CUDA_OK=1
elif [ -d /usr/local/cuda ]; then
    _CUDA_OK=1
fi

if [ "$_CUDA_OK" = "1" ]; then
    echo ">>> CUDA detected, removing CPU onnxruntime if present"
    uv pip uninstall onnxruntime onnxruntime-gpu -y 2>/dev/null || true
	    _SITE_PKGS=$(python -c "import site; print(site.getsitepackages()[0])")
	    rm -rf "$_SITE_PKGS/onnxruntime" "$_SITE_PKGS/onnxruntime_gpu"*
    if [ "$_ARCH" = "x86_64" ]; then
        echo ">>> CUDA detected (x86_64), installing onnxruntime-gpu"
        uv pip install --reinstall onnxruntime-gpu
    elif [ "$_ARCH" = "aarch64" ]; then
        echo ">>> CUDA detected (ARM64), looking for onnxruntime-gpu .whl"
        _WHL=$(ls onnxruntime_gpu-*-cp310-cp310-linux_aarch64.whl 2>/dev/null | head -1)
        if [ -n "$_WHL" ]; then
            uv pip install --reinstall "$_WHL"
        else
            echo "!!! No aarch64 .whl found, falling back to onnxruntime (CPU)"
            uv pip install onnxruntime
        fi
    fi
else
    echo ">>> No CUDA detected, installing onnxruntime (CPU)"
    uv pip install onnxruntime
fi

# ── 4. Optional: test client deps ──
if [ "${INSTALL_CLIENT:-0}" = "1" ]; then
    uv pip install pyaudio scipy
    echo ">>> Test client dependencies installed"
fi

# ── 5. Run server ──
echo ">>> Starting server on http://0.0.0.0:8001"
exec python main.py