# CLAUDE.md

## Project Overview

MiniCPM-o 事件检测 — 实时视频双工感知，检测到事件打印到控制台。

```
摄像头 + 麦克风
      │
      ▼
MiniCPM-o-4.5（常驻，超时自动重连）
      │
      ▼
文本输出打印到控制台
```

## Commands

```bash
# 安装依赖
uv sync

# 启动（摄像头 0，10fps）
uv run python main.py

# 指定摄像头
uv run python main.py --camera 1

# 调试模式
uv run python main.py --debug

# 禁用音频采集
uv run python main.py --no-audio-capture

# Foxglove/ROS 桥接
uv run python main.py --source foxglove

# 调整帧率 / 指定 MiniCPM-o 服务地址
uv run python main.py --fps 2
uv run python main.py --minicpmo-host <host>
```

所有参数均可通过 `.env` 文件配置，命令行参数优先级更高。

## Key Source Files

| 文件 | 说明 |
|---|---|
| `main.py` | 唯一入口。摄像头采集 + 人脸检测 + CPM WebSocket + 文本打印 |
| `config.py` | 统一配置（摄像头/CPM/人脸/日志） |
| `minicpmo_client.py` | MiniCPM-o WebSocket 客户端 |
| `prompts.py` | CPM 系统提示词 |
| `face_detect.py` | 人脸检测 + PIL 中文标注 |
| `foxglove_client.py` | Foxglove WebSocket + 本地摄像头封装 |
| `logging_config.py` | 统一 Logger 工厂 |

## 环境管理

所有依赖统一在 `pyproject.toml` 中声明，使用 `uv sync` 安装。