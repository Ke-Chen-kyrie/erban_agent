FROM docker.m.daocloud.io/library/python:3.11-slim

LABEL org.opencontainers.image.title="Event Detection"
LABEL org.opencontainers.image.description="Real-time anomaly event detection for elderly care monitoring"

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_HTTP_TIMEOUT=300

# System dependencies: OpenCV (cv2) needs libgl1 + libglib2.0-0;
# pyaudio needs libportaudio2 (runtime) + portaudio19-dev (build) + build-essential.
# apt 换阿里云镜像源，避免 deb.debian.org 在国内构建时 apt-get update 超时（可达 500s+）。
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libportaudio2 \
    portaudio19-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install uv (project uses uv.lock for locked dependencies) ──
# daocloud ghcr 镜像源 403，直连 ghcr.io 拉取
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# ── Install Python dependencies from lockfile ──
# Copy only the build manifests first so dependency layer stays cached across
# source edits. --no-install-project defers the project itself until COPY below.
# `.env` is NOT copied (see .dockerignore) — it is injected at runtime by
# docker-compose via `env_file`, exactly like user_identification.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ── Copy source code ──
COPY . /app/

# Adapter and Agent monitor server ports (runtime values remain configurable).
EXPOSE 8771 8781

# Run directly from the venv created by uv sync (no uv invocation overhead at
# runtime). Config comes from environment variables injected via env_file.
CMD ["/app/.venv/bin/python", "main.py"]
