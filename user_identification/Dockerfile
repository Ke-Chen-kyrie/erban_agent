FROM nvcr.io/nvidia/l4t-jetpack:r36.4.0

LABEL org.opencontainers.image.title="User Identification Service"
LABEL org.opencontainers.image.description="Face and voice biometric authentication service"

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# System dependencies: Python 3.10 + opencv/soundfile/onnxruntime libs
RUN sed -i 's|ports.ubuntu.com|mirrors.ustc.edu.cn|g' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3.10-dev \
    python3-pip \
    libgl1 \
    libglib2.0-0 \
    libsndfile1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.10 /usr/bin/python \
    && ln -sf /usr/bin/python3.10 /usr/bin/python3

WORKDIR /app

# ── Copy project files ──
COPY . /app/

# ── Install voiceprint SDKs ──
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
    /app/voiceprint/xunfei-cloud/naviai_voiceprint-0.3.0 \
    -e /app/voiceprint/campp/sdk \
    -e /app/voiceprint/eres2net/sdk \
    -e /app/voiceprint/ecapa/sdk

# ── Install Python dependencies ──
RUN sed -i '/^-e /d' /app/requirements.txt && \
    pip install --no-cache-dir -r /app/requirements.txt

# Ensure data directories exist
RUN mkdir -p /app/data/SQLite /app/data/storage/faces /app/data/chroma_db

# ── Install onnxruntime (same approach as deploy/deploy.sh) ──
# Must run AFTER all pip installs because transitive dependencies
# (voiceprint SDKs, insightface, etc.) may pull in CPU-only onnxruntime.
# CUDA / cuDNN / TensorRT are already provided by the L4T base image.
ARG ONNXRUNTIME_MODE=cpu
RUN if [ "${ONNXRUNTIME_MODE}" = "gpu" ]; then \
        echo ">>> GPU mode: reinstalling onnxruntime-gpu"; \
        pip uninstall onnxruntime onnxruntime-gpu -y 2>/dev/null || true; \
        rm -rf /usr/local/lib/python3.10/site-packages/onnxruntime /usr/local/lib/python3.10/site-packages/onnxruntime_gpu*; \
        _ARCH=$(uname -m); \
        if [ "$_ARCH" = "x86_64" ]; then \
            pip install --no-cache-dir onnxruntime-gpu; \
        elif [ "$_ARCH" = "aarch64" ]; then \
            _WHL=$(ls /app/onnxruntime_gpu-*-cp310-cp310-linux_aarch64.whl 2>/dev/null | head -1); \
            if [ -n "$_WHL" ]; then \
                pip install --no-cache-dir "$_WHL"; \
            else \
                echo "!!! No aarch64 .whl found, falling back to onnxruntime (CPU)"; \
                pip install --no-cache-dir onnxruntime; \
            fi; \
        fi; \
    else \
        echo ">>> CPU mode: keeping onnxruntime (CPU)"; \
    fi; \
    rm -f /app/onnxruntime_gpu-*.whl

EXPOSE 8001

CMD ["python", "main.py"]