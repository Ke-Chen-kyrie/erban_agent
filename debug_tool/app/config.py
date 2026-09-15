from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    api_base: str = "http://172.16.2.135:8001"
    shell_proxy_url: str = "http://192.168.217.100:8088/run"
    request_timeout: int = 30
    robot_command_timeout: int = 600
    camera_index: str | int = "auto"
    foxglove_bridge_url: str = "ws://192.168.217.100:8768"
    camera_topic_head: str = "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed"
    camera_topic_up: str = "/zj_humanoid/sensor/realsense_up/color/image_raw/compressed"
    audio_samplerate: int = 16000
    face_detect_match_threshold: float = 0.5

    def with_api_base(self, value: str) -> "AppConfig":
        return replace(self, api_base=value.strip().rstrip("/"))


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    try:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return AppConfig()
    return AppConfig(
        api_base=str(data.get("api_base", AppConfig.api_base)).rstrip("/"),
        shell_proxy_url=str(data.get("shell_proxy_url", AppConfig.shell_proxy_url)).rstrip("/"),
        request_timeout=int(data.get("request_timeout", AppConfig.request_timeout)),
        robot_command_timeout=int(data.get("robot_command_timeout", AppConfig.robot_command_timeout)),
        camera_index=data.get("camera_index", AppConfig.camera_index),
        foxglove_bridge_url=str(data.get("foxglove_bridge_url", AppConfig.foxglove_bridge_url)),
        camera_topic_head=str(data.get("camera_topic_head", AppConfig.camera_topic_head)),
        camera_topic_up=str(data.get("camera_topic_up", AppConfig.camera_topic_up)),
        audio_samplerate=int(data.get("audio_samplerate", AppConfig.audio_samplerate)),
        face_detect_match_threshold=float(
            data.get("face_detect_match_threshold", AppConfig.face_detect_match_threshold)
        ),
    )
