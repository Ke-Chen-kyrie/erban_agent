# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

JoyAI-VL-Interaction is an 8B-scale real-time video-language interaction model. The model watches a live video stream continuously and decides each second whether to **speak** or stay **silent**. Built on JoyAI-VL-8B and trained on 4M+ time-aligned interaction clips.

This copy is stripped down to CLI-only: webcam capture → webinfer adapter → console output. The adapter handles chunk management, mid-term summaries, and long-term memory compression before calling the main VLM via remote vLLM.

## Commands

```bash
# Install dependencies (aiohttp, openai, Pillow, numpy, opencv-python)
./install.sh

# Start adapter only
./run.sh adapter

# Start adapter + CLI capture
./run.sh all

# Stop
./stop.sh
```

### Using remote vLLM

```bash
MAIN_API_BASE="http://<remote>:7060/v1" \
SUMMARIZER_API_BASE="http://<remote>:8065/v1" \
  ./run.sh all
```

## Architecture

| Component | File | Role |
|-----------|------|------|
| **adapter** | `webinfer/live_adapter.py` | Inference adapter. Manages frame accumulation, chunk memory, mid-term summaries, long-term memory. Proxies to remote vLLM backends via OpenAI-compatible API. |
| **memory** | `webinfer/memory_summarizer.py` | `SummarizerModel` for mid-term and long-term memory compression. |
| **capture** | `capture.py` | CLI video capture. Grabs frames from local camera (OpenCV) or Foxglove/ROS bridge, sends to adapter, prints model responses to console. |

**Data flow**: `capture.py` grabs webcam frames → sends base64 JPEG to adapter at `/v1/chat/completions` → adapter assembles context (current frames + video history summaries + Q&A history + long-term memory) → calls main VLM via remote vLLM → model returns `</silence>` or `</response> text` → printed to console.

## Key Source Files

- `webinfer/live_adapter.py` — Core inference adapter. `StreamingInferAdapter` class owns session state, chunk management, memory summarization, and the OpenAI-compatible `/v1/chat/completions` endpoint.
- `webinfer/memory_summarizer.py` — `SummarizerModel` class for mid-term and long-term memory compression.
- `capture.py` — CLI video capture script. Supports `--source camera` (OpenCV) or `--source foxglove` (ROS bridge). Sends frames to adapter, prints responses.
- `foxglove_client.py` — Foxglove WebSocket client for ROS2 topic subscription and image deserialization. Provides `FoxgloveImageCapture` with a `read()`/`release()` interface.
- `config.py` — Shared config (Foxglove bridge URL).
- `logging_config.py` — Shared logging setup.
- `run.sh` — Single entrypoint: adapter start, adapter+capture orchestration, and all adapter config.
- `stop.sh` — Stop adapter process.
- `install.sh` — Dependency setup using `uv` + `pip`.