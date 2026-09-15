"""机器人表情播放客户端库.

走 rosbridge WebSocket 调 /zj_humanoid/robot/face_show/media_play, 强制循环播放.
连接参数由本库自读 /config/config.yaml 的 emote 段 (CMD_CONFIG 可覆盖根).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Optional

import websocket
import yaml

_DEFAULT_HOST = "192.168.217.100"
_DEFAULT_PORT = 9090
_FACE_SERVICE = "/zj_humanoid/robot/face_show/media_play"

# 表情 key -> {file, name}
EMOTIONS: dict[str, dict[str, str]] = {
    "default": {"file": "1-default.mp4", "name": "默认"},
    "sigh": {"file": "2-sigh.mp4", "name": "叹气"},
    "smile": {"file": "3-smile.mp4", "name": "微笑"},
    "confusion": {"file": "4-confusion.mp4", "name": "困惑"},
    "excited": {"file": "5-excited.mp4", "name": "兴奋"},
    "shy": {"file": "6-shy.mp4", "name": "害羞"},
    "working": {"file": "7-working.mp4", "name": "工作中"},
    "thinking": {"file": "8-thking.mp4", "name": "思考中"},
    "here": {"file": "9-Iamhere.mp4", "name": "我在这里"},
}


def _config_root() -> Path:
    return Path(os.environ.get("CMD_CONFIG", "/config"))


def _emote_section() -> dict:
    cfg_path = _config_root() / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    if not isinstance(data, dict):
        return {}
    return data.get("emote", {}) or {}


def emote_host() -> str:
    return str(_emote_section().get("host", _DEFAULT_HOST))


def emote_port() -> int:
    return int(_emote_section().get("port", _DEFAULT_PORT))


def play_emotion(key: str, timeout: float = 5.0) -> bool:
    """播放指定表情 (强制循环). 成功退 True, 失败/超时退 False."""
    emotion = EMOTIONS.get(key)
    if not emotion:
        print(f"无效表情: {key}", file=sys.stderr)
        return False
    try:
        ws = websocket.create_connection(
            f"ws://{emote_host()}:{emote_port()}", timeout=timeout
        )
    except Exception as e:
        print(f"连接 rosbridge 失败: {e}", file=sys.stderr)
        return False
    call_id = "emote_1"
    ws.send(
        json.dumps(
            {
                "op": "call_service",
                "service": _FACE_SERVICE,
                "args": {"media_path": emotion["file"], "loop": True, "duration": 0.0},
                "id": call_id,
            }
        )
    )
    try:
        ws.settimeout(timeout)
        while True:
            msg = json.loads(ws.recv())
            if msg.get("op") == "service_response" and msg.get("id") == call_id:
                success = msg.get("result", True)
                if success is False:
                    print(f"播放 {emotion['name']} 被机器人拒: {msg.get('values')}", file=sys.stderr)
                    ws.close()
                    return False
                ws.close()
                return True
    except Exception as e:
        print(f"播放 {emotion['name']} 失败: {e}", file=sys.stderr)
        ws.close()
        return False