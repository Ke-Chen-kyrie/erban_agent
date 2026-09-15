FROM python:3.11-slim

LABEL org.opencontainers.image.title="ZenohRelay"
LABEL org.opencontainers.image.description="Foxglove/摄像头人脸识别标注与 Zenoh 局域网分发"

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_HTTP_TIMEOUT=300

# OpenCV (cv2) 需要 libgl1 + libglib2.0-0；中文人脸姓名标注需要 CJK 字体
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── 安装 uv，用 uv.lock 锁定依赖（与 event_detection_new 同方案）──
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# 先只拷贝清单文件，让依赖层在源码改动时可复用缓存。
# --no-install-project 把项目本身推迟到下面 COPY 源码之后。
# .env 不拷入镜像（.dockerignore 排除），运行时由 docker-compose env_file 注入。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ── 拷贝源码 ──
COPY . /app/

# 运行配置全部来自环境变量（docker-compose env_file 注入 .env）
CMD ["/app/.venv/bin/python", "server.py"]