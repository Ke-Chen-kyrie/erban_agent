from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from .config import AppConfig


class LocalCameraWorker(QObject):
    frame = Signal(object)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._capture: cv2.VideoCapture | None = None
        self._timer: QTimer | None = None
        self._running = False

    @Slot()
    def start(self) -> None:
        try:
            self._capture, index, first = self._open_camera()
        except Exception as exc:
            self.error.emit(str(exc))
            self.stopped.emit()
            return
        self._running = True
        self.status.emit(f"本地摄像头已开启：{index}")
        self.frame.emit(first)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._read_frame)
        self._timer.start(33)

    @Slot()
    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.stop()
        if self._capture:
            self._capture.release()
            self._capture = None
        self.stopped.emit()

    def _read_frame(self) -> None:
        if not self._running or self._capture is None:
            return
        ok, frame = self._capture.read()
        if ok and frame is not None and frame.size > 0:
            self.frame.emit(frame)

    def _open_camera(self) -> tuple[cv2.VideoCapture, int, np.ndarray]:
        errors: list[str] = []
        for index in camera_indexes(self.config.camera_index):
            if is_ir_camera(index):
                continue
            for backend_name, backend in camera_backends():
                capture = cv2.VideoCapture(index, backend)
                if not capture.isOpened():
                    capture.release()
                    errors.append(f"{index}/{backend_name}: 无法打开")
                    continue
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                frame = read_camera_frame(capture)
                if frame is None:
                    capture.release()
                    errors.append(f"{index}/{backend_name}: 无法读取画面")
                    continue
                return capture, index, frame
        devices = ", ".join(str(p) for p in sorted(Path("/dev").glob("video*"))) or "未发现 /dev/video*"
        raise RuntimeError(f"摄像头打开失败。发现设备：{devices}。失败详情：{'；'.join(errors) or '无'}")


class RobotCameraWorker(QObject):
    frame = Signal(object)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, config: AppConfig, topic_key: str) -> None:
        super().__init__()
        self.config = config
        self.topic_key = topic_key
        self._capture: Any = None
        self._timer: QTimer | None = None

    @Slot()
    def start(self) -> None:
        try:
            from foxglove_client import FoxgloveImageCapture
        except ImportError as exc:
            self.error.emit(f"机器人摄像头不可用：{exc}")
            self.stopped.emit()
            return

        topic = self.config.camera_topic_head if self.topic_key == "head" else self.config.camera_topic_up
        label = "头部" if self.topic_key == "head" else "腰部"
        self.status.emit(f"正在连接机器人{label}摄像头...")
        try:
            self._capture = FoxgloveImageCapture(topic, url=self.config.foxglove_bridge_url)
            ok, first = self._capture.read(timeout=15.0)
        except Exception as exc:
            self.error.emit(f"机器人摄像头连接失败：{exc}")
            self.stopped.emit()
            return
        if not ok or first is None:
            self.error.emit(f"机器人{label}摄像头连接失败：15 秒内未收到画面")
            self.stop()
            return
        self.status.emit(f"机器人摄像头已连接（{label}）")
        self.frame.emit(cv2.cvtColor(first, cv2.COLOR_RGB2BGR))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._read_frame)
        self._timer.start(333)

    @Slot()
    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                pass
            self._capture = None
        self.stopped.emit()

    def _read_frame(self) -> None:
        if self._capture is None:
            return
        with self._capture._lock:
            latest = None if self._capture._latest_frame is None else self._capture._latest_frame.copy()
        if latest is not None:
            self.frame.emit(cv2.cvtColor(latest, cv2.COLOR_RGB2BGR))


class CameraController(QObject):
    frame = Signal(object)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()
    _stop_requested = Signal()

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._thread: QThread | None = None
        self._worker: QObject | None = None

    def start_local(self) -> None:
        self.stop()
        self._start_worker(LocalCameraWorker(self.config))

    def start_robot(self, topic_key: str) -> None:
        self.stop()
        self._start_worker(RobotCameraWorker(self.config, topic_key))

    def stop(self) -> None:
        if self._thread is not None:
            if self._worker is not None:
                self._stop_requested.emit()
            if not self._thread.wait(1800):
                self._thread.quit()
                self._thread.wait(700)
        self._thread = None
        self._worker = None

    def _start_worker(self, worker: QObject) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.start)  # type: ignore[attr-defined]
        self._stop_requested.connect(worker.stop)  # type: ignore[attr-defined]
        worker.frame.connect(self.frame)  # type: ignore[attr-defined]
        worker.status.connect(self.status)  # type: ignore[attr-defined]
        worker.error.connect(self.error)  # type: ignore[attr-defined]
        worker.stopped.connect(self.stopped)  # type: ignore[attr-defined]
        worker.stopped.connect(thread.quit)  # type: ignore[attr-defined]
        thread.finished.connect(lambda: self._disconnect_stop(worker))
        thread.finished.connect(worker.deleteLater)
        thread.start()
        self._thread = thread
        self._worker = worker

    def _disconnect_stop(self, worker: QObject) -> None:
        try:
            self._stop_requested.disconnect(worker.stop)  # type: ignore[attr-defined]
        except Exception:
            pass


def camera_indexes(value: Any) -> list[int]:
    text = str(value or "auto").strip().lower()
    if text in {"auto", "*"}:
        discovered: list[int] = []
        for path in sorted(Path("/dev").glob("video*")):
            name = path.name
            if name.startswith("video") and name[5:].isdigit():
                index = int(name[5:])
                if not is_ir_camera(index):
                    discovered.append(index)
        return discovered or [0]
    indexes: list[int] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part:
            digits = "".join(ch for ch in part if ch.isdigit())
            if not digits:
                raise ValueError(f"Invalid camera index: {part}")
            indexes.append(int(digits))
    return indexes or [0]


def camera_backends() -> list[tuple[str, int]]:
    return [("V4L2", cv2.CAP_V4L2), ("ANY", cv2.CAP_ANY)]


def is_ir_camera(index: int) -> bool:
    try:
        name = Path(f"/sys/class/video4linux/video{index}/name").read_text().strip().lower()
        return "ir" in name
    except Exception:
        return False


def read_camera_frame(capture: cv2.VideoCapture) -> np.ndarray | None:
    import time

    for _ in range(15):
        ok, frame = capture.read()
        if ok and frame is not None and frame.size > 0:
            return frame
        time.sleep(0.03)
    return None
