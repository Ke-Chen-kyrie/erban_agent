"""本地摄像头最新帧捕获，返回格式与 FoxgloveImageCapture 一致。"""

from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np


class LocalCameraCapture:
    """后台持续读取本地摄像头，只缓存最新 RGB 帧。"""

    def __init__(self, camera_id: int = 0, width: int = 0, height: int = 0) -> None:
        capture = cv2.VideoCapture(camera_id)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"无法打开本地摄像头 /dev/video{camera_id}")
        if width > 0:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height > 0:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        self._capture = capture
        self._condition = threading.Condition()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_sequence = 0
        self._timestamp_ns = 0
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def read(self, timeout: float = 1.0) -> Tuple[bool, Optional[np.ndarray]]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._running and self._latest_frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, None
                self._condition.wait(remaining)
            if self._latest_frame is None:
                return False, None
            return True, self._latest_frame.copy()

    def read_next(
        self, after_sequence: int = 0, timeout: float = 1.0
    ) -> tuple[bool, Optional[np.ndarray], int, int]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._running and self._frame_sequence <= after_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, None, after_sequence, 0
                self._condition.wait(remaining)
            if self._latest_frame is None or self._frame_sequence <= after_sequence:
                return False, None, after_sequence, 0
            return (
                True,
                self._latest_frame.copy(),
                self._frame_sequence,
                self._timestamp_ns,
            )

    @property
    def frame_sequence(self) -> int:
        with self._condition:
            return self._frame_sequence

    def release(self) -> None:
        self._running = False
        with self._condition:
            self._condition.notify_all()
        self._capture.release()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def _capture_loop(self) -> None:
        while self._running:
            success, bgr = self._capture.read()
            if not success or bgr is None:
                if self._running:
                    time.sleep(0.05)
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            with self._condition:
                self._latest_frame = rgb
                self._frame_sequence += 1
                self._timestamp_ns = time.time_ns()
                self._condition.notify_all()

        with self._condition:
            self._condition.notify_all()
