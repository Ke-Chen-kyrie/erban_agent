"""ZenohRelay 运行配置。环境变量从项目根目录的 ``.env`` 加载。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def env(name: str, default=None):
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _as_int(name: str, default: int) -> int:
    return int(env(name, default))


def _as_float(name: str, default: float) -> float:
    return float(env(name, default))


def _as_bool(name: str, default: bool) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def make_zenoh_config(url: str | None = None, listen: str | None = None):
    """创建 Zenoh 配置（固定端口模式，不使用 multicast 自动发现）。

    ``listen`` 指定服务端监听的固定端口（如 ``tcp/0.0.0.0:7447``）；
    ``url`` 指定显式连接的 endpoint。两者都可同时设置；都不设置时仅创建
    一个不参与自动发现的空会话（通常由默认监听地址保证可用）。
    """
    import zenoh

    config = zenoh.Config()
    endpoints = [part.strip() for part in (url or "").split(",") if part.strip()]
    listeners = [part.strip() for part in (listen or "").split(",") if part.strip()]
    config.insert_json5("mode", '"peer"')
    config.insert_json5("scouting/multicast/enabled", "false")
    if listeners:
        config.insert_json5("listen/endpoints", json.dumps(listeners))
    if endpoints:
        config.insert_json5("connect/endpoints", json.dumps(endpoints))
    return config


@dataclass(frozen=True)
class Settings:
    video_source: str
    foxglove_url: str
    foxglove_topic: str
    rosbridge_url: str
    camera_id: int
    camera_width: int
    camera_height: int
    zenoh_topic: str
    zenoh_url: str
    zenoh_listen_url: str
    include_metadata: bool
    jpeg_quality: int
    max_fps: float
    ident_url: str
    ident_timeout: float
    ident_threshold: float
    max_face_num: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            video_source=str(env("VIDEO_SOURCE", "foxglove")).strip().lower(),
            foxglove_url=str(env("FOXGLOVE_URL", "ws://192.168.217.100:8768")),
            foxglove_topic=str(env(
                "FOXGLOVE_TOPIC",
                "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed",
            )),
            rosbridge_url=str(env("ROSBRIDGE_URL", "ws://192.168.217.100:9090")),
            camera_id=_as_int("CAMERA_ID", 0),
            camera_width=_as_int("CAMERA_WIDTH", 0),
            camera_height=_as_int("CAMERA_HEIGHT", 0),
            zenoh_topic=str(env("ZENOH_TOPIC", "camera/annotated")),
            zenoh_url=str(env("ZENOH_URL", "")),
            zenoh_listen_url=str(env("ZENOH_LISTEN_URL", "tcp/0.0.0.0:7447")),
            include_metadata=_as_bool("INCLUDE_METADATA", False),
            jpeg_quality=_as_int("JPEG_QUALITY", 85),
            max_fps=_as_float("MAX_FPS", 2.0),
            ident_url=str(env("IDENT_URL", "http://127.0.0.1:8001")),
            ident_timeout=_as_float("IDENT_TIMEOUT", 30.0),
            ident_threshold=_as_float("IDENT_THRESHOLD", 0.7),
            max_face_num=_as_int("MAX_FACE_NUM", 10),
            log_level=str(env("LOG_LEVEL", "INFO")).upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.video_source not in {"foxglove", "camera", "rosbridge"}:
            raise ValueError("VIDEO_SOURCE 仅支持 foxglove、camera 或 rosbridge")
        if not self.foxglove_topic.startswith("/"):
            raise ValueError("FOXGLOVE_TOPIC 必须是以 / 开头的 ROS 话题")
        if self.camera_id < 0:
            raise ValueError("CAMERA_ID 不能小于 0")
        if self.camera_width < 0 or self.camera_height < 0:
            raise ValueError("CAMERA_WIDTH/CAMERA_HEIGHT 不能小于 0")
        if not self.zenoh_topic.strip("/"):
            raise ValueError("ZENOH_TOPIC 不能为空")
        for listener in filter(None, (part.strip() for part in self.zenoh_listen_url.split(","))):
            proto, sep, address = listener.partition("/")
            host, _, port = address.rpartition(":")
            if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError(
                    f"ZENOH_LISTEN_URL 格式应为 tcp/0.0.0.0:7447，收到 {listener!r}"
                )
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("JPEG_QUALITY 必须在 1..100")
        if self.max_fps < 0:
            raise ValueError("MAX_FPS 不能小于 0；0 表示只受识别耗时限制")
        if self.ident_timeout <= 0:
            raise ValueError("IDENT_TIMEOUT 必须大于 0")
        if not 0 <= self.ident_threshold <= 1:
            raise ValueError("IDENT_THRESHOLD 必须在 0..1")
        if not 1 <= self.max_face_num <= 100:
            raise ValueError("MAX_FACE_NUM 必须在 1..100")
