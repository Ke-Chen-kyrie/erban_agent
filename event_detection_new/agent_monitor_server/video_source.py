"""Task-scoped video capture with a latest-frame buffer."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import cv2

from .config import MonitorConfig


@dataclass(frozen=True)
class CapturedFrame:
    sequence: int
    image: Any
    captured_at: str
    bgr_input: bool


VideoSourceFactory = Callable[[], tuple[Any, bool, str]]


def create_video_source(config: MonitorConfig) -> tuple[Any, bool, str]:
    source = config.video_source
    if source == "camera":
        camera_id = int(os.getenv("CAMERA_ID", "0"))
        capture = cv2.VideoCapture(camera_id)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"cannot open camera {camera_id}")
        width = int(os.getenv("CAMERA_WIDTH", "0"))
        height = int(os.getenv("CAMERA_HEIGHT", "0"))
        if width > 0:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height > 0:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        return capture, True, f"Camera {camera_id}"
    if source == "foxglove":
        from foxglove_client import FoxgloveImageCapture

        topic = os.getenv(
            "FOXGLOVE_TOPIC", "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed"
        )
        url = os.getenv("FOXGLOVE_BRIDGE_URL", "ws://192.168.217.100:8768")
        return FoxgloveImageCapture(topic=topic, url=url or None), False, f"Foxglove topic={topic}"
    if source == "rosbridge":
        from foxglove_client import ROSBRIDGE_URL, RosbridgeImageCapture

        topic = os.getenv(
            "FOXGLOVE_TOPIC", "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed"
        )
        url = os.getenv("ROSBRIDGE_URL", ROSBRIDGE_URL)
        return RosbridgeImageCapture(topic=topic, url=url or None), False, f"ROSBridge topic={topic}"
    if source == "zenoh":
        from zenoh_client import DEFAULT_TOPIC, ZenohImageCapture

        topic = os.getenv("ZENOH_TOPIC", DEFAULT_TOPIC)
        url = os.getenv("ZENOH_URL", "").strip() or None
        fps = float(os.getenv("ZENOH_CLIENT_FPS", "2"))
        return ZenohImageCapture(topic=topic, url=url, fps=fps), False, f"Zenoh topic={topic}"
    raise ValueError(f"unsupported VIDEO_SOURCE: {source}")


class LatestFrameCapture:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        factory: VideoSourceFactory | None = None,
    ) -> None:
        self._config = config
        self._factory = factory or (lambda: create_video_source(config))
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._source: Any = None
        self._latest: CapturedFrame | None = None
        self._sequence = 0
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    @property
    def last_error(self) -> str | None:
        with self._condition:
            return self._last_error

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._source, self._bgr_input, self.description = self._factory()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._config.video_source == "camera":
                    ok, image = self._source.read()
                else:
                    ok, image = self._source.read(timeout=2.0 if self._config.video_source == "zenoh" else 1.0)
            except Exception as exc:
                with self._condition:
                    self._last_error = f"video_read_failed:{type(exc).__name__}"
                ok, image = False, None
            if not ok or image is None:
                self._stop.wait(0.05)
                continue
            with self._condition:
                self._last_error = None
                self._sequence += 1
                self._latest = CapturedFrame(
                    sequence=self._sequence,
                    image=image,
                    captured_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    bgr_input=self._bgr_input,
                )
                self._condition.notify_all()
            if self._config.frame_interval > 0:
                self._stop.wait(self._config.frame_interval)

    def latest(self, after_sequence: int, timeout: float = 0.0) -> CapturedFrame | None:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while self._latest is None or self._latest.sequence <= after_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._latest

    def clear(self) -> None:
        with self._condition:
            self._latest = None

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        source, self._source = self._source, None
        self._thread = None
        if source is not None:
            source.release()
        self.clear()
