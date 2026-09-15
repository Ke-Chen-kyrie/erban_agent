"""以 FoxgloveImageCapture 风格读取 ZenohRelay 标注图像。

``read(timeout)`` 返回 ``(success, RGB numpy.ndarray)``。客户端只订阅服务端
输出，不选择服务端使用本地摄像头还是 ROS/Foxglove 话题。
"""

from __future__ import annotations

import json
import logging
import os
import struct
import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np


logger = logging.getLogger(__name__)
DEFAULT_TOPIC = "camera/annotated"
_META_MAGIC = b"ZRM1"
_META_HEADER = struct.Struct(">4sI")


def _make_zenoh_config(url: str | None):
    import zenoh

    config = zenoh.Config()
    endpoints = [part.strip() for part in (url or "").split(",") if part.strip()]
    if endpoints:
        config.insert_json5("connect/endpoints", json.dumps(endpoints))
    else:
        config.insert_json5("mode", '"peer"')
        config.insert_json5("scouting/multicast/enabled", "true")
    return config


def _decode_frame(payload: bytes) -> tuple[bytes, dict | None]:
    if len(payload) < _META_HEADER.size or not payload.startswith(_META_MAGIC):
        return payload, None
    magic, length = _META_HEADER.unpack_from(payload)
    end = _META_HEADER.size + length
    if magic != _META_MAGIC or end > len(payload):
        raise ValueError("无效的 ZenohRelay metadata payload")
    metadata = json.loads(payload[_META_HEADER.size:end].decode("utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("ZenohRelay metadata 必须是 JSON object")
    return payload[end:], metadata


class ZenohImageCapture:
    """订阅并缓存最新标注帧，返回 RGB 图像。"""

    def __init__(
        self,
        topic: str = DEFAULT_TOPIC,
        url: str | None = None,
        fps: float = 0.0,
    ) -> None:
        if fps < 0:
            raise ValueError("fps 不能小于 0；0 表示不限制客户取用频率")
        self._topic = topic
        self._url = url if url is not None else os.getenv("ZENOH_URL", "")
        self._fps = fps
        self._min_read_interval = 1.0 / fps if fps > 0 else 0.0
        self._last_read_at = 0.0
        self._condition = threading.Condition()
        self._start_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_meta: Optional[dict] = None
        self._frame_sequence = 0
        self._session = None
        self._subscriber = None
        self._running = False

    def read(self, timeout: float = 1.0) -> Tuple[bool, Optional[np.ndarray]]:
        """返回 ``(success, RGB image)``，与 Foxglove 捕获接口一致。"""
        if not self._running:
            self._connect_sync()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._running:
                remaining = deadline - time.monotonic()
                if self._latest_frame is None:
                    if remaining <= 0:
                        return False, None
                    self._condition.wait(remaining)
                    continue

                throttle = self._min_read_interval - (
                    time.monotonic() - self._last_read_at
                )
                if throttle > 0:
                    if remaining <= 0:
                        return False, None
                    self._condition.wait(min(throttle, remaining))
                    continue

                self._last_read_at = time.monotonic()
                return True, self._latest_frame.copy()
            return False, None

    @property
    def latest_meta(self) -> Optional[dict]:
        with self._condition:
            return dict(self._latest_meta) if self._latest_meta is not None else None

    @property
    def frame_sequence(self) -> int:
        with self._condition:
            return self._frame_sequence

    def release(self) -> None:
        with self._start_lock:
            self._running = False
            with self._condition:
                self._condition.notify_all()

            subscriber, self._subscriber = self._subscriber, None
            session, self._session = self._session, None
            if subscriber is not None:
                try:
                    subscriber.undeclare()
                except Exception as exc:
                    logger.debug("取消 Zenoh 订阅失败: %s", exc)
            if session is not None:
                try:
                    session.close()
                except Exception as exc:
                    logger.debug("关闭 Zenoh session 失败: %s", exc)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def _connect_sync(self) -> None:
        with self._start_lock:
            if self._running:
                return
            import zenoh

            session = None
            try:
                session = zenoh.open(_make_zenoh_config(self._url))
                self._running = True
                subscriber = session.declare_subscriber(self._topic, self._on_sample)
            except Exception:
                self._running = False
                if session is not None:
                    session.close()
                raise
            self._session = session
            self._subscriber = subscriber
            logger.info(
                "已订阅 Zenoh topic=%s via %s",
                self._topic,
                self._url or "LAN multicast",
            )

    def _on_sample(self, sample) -> None:
        try:
            jpeg, metadata = _decode_frame(bytes(sample.payload))
            bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise ValueError("JPEG 解码失败")
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        except Exception as exc:
            logger.warning("丢弃无效 Zenoh 图像 payload: %s", exc)
            return

        with self._condition:
            if not self._running:
                return
            self._latest_frame = rgb
            self._latest_meta = metadata
            self._frame_sequence += 1
            self._condition.notify_all()


__all__ = ["DEFAULT_TOPIC", "ZenohImageCapture"]
