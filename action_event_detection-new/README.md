# Action Event Detection

实时视频动作检测系统。摄像头采集 → 远程 vLLM 推理 → 检测到动作时 POST /action_event。

## 快速开始

```bash
# 安装依赖
uv sync

# 配置 .env 后启动
uv run python main.py
```

## 配置

所有配置通过 `.env` 文件或命令行参数设置：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MAIN_API_BASE` | 远程 vLLM 端点 | `http://127.0.0.1:7060/v1` |
| `MAIN_MODEL` | 模型名称 | `streamingharness-8b` |
| `VIDEO_SOURCE` | 视频源：`camera` / `foxglove` | `camera` |
| `CAMERA_ID` | 摄像头设备 ID | `0` |
| `CAPTURE_INTERVAL` | 采集间隔（秒） | `1.0` |
| `CHUNK` | 上下文窗口帧数 | `100` |
| `ACTION_EVENT_HOST` | 事件推送 Host | `127.0.0.1` |
| `ACTION_EVENT_PORT` | 事件推送 Port | `8770` |

## 推送事件格式

检测到动作时，POST 到 `http://{host}:{port}/action_event`：

```json
{
    "action_type": "wave",
    "confidence": 0.95,
    "image": "<base64 原图 JPEG>"
}
```

## 支持的行为

| 标签 | 行为 |
|------|------|
| `wave` | 挥手 |
| `chest_pain` | 捂胸 |
| `head_pain` | 捂头 |
| `abdomen_pain` | 捂腹 |
| `mouth_open` | 张嘴 |
| `eye_rub` | 揉眼 |
| `ok` | OK 手势 |
| `thumbs_up` | 点赞 |
| `raise_hand` | 举手 |
| `stop` | 停止 |