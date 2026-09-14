# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Action Event Detection — 实时视频动作检测系统。摄像头采集 → 远程 vLLM 推理 → 检测到动作时 POST /action_event。

## Commands

```bash
# 安装依赖
uv sync

# 启动（默认摄像头 0）
uv run python main.py

# 指定摄像头
uv run python main.py --camera 1

# Foxglove/ROS 桥接
uv run python main.py --source foxglove

# 远程 vLLM
uv run python main.py --main-api-base http://<remote>:7060/v1
```

## Architecture

1. 摄像头采集帧（OpenCV / Foxglove）
2. 帧攒成 chunk（默认 100 帧），作为上下文发给 VLM
3. VLM 返回 `</silence>` 或 `</response> [label] confidence`
4. 检测到动作 → POST `http://{host}:8770/action_event`

```json
{
    "action_type": "wave",
    "confidence": 0.95,
    "image": "<base64 原图>"
}
```

## Key Source Files

- `main.py` — 唯一入口。采集 + 推理 + 事件推送
- `prompt.py` — System prompt，定义 10 种行为检测标准
- `foxglove_client.py` — Foxglove WebSocket 客户端（ROS2）
- `config.py` — Foxglove bridge URL 配置
- `pyproject.toml` — 依赖配置