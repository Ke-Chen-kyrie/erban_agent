"""摄像头采集线程 —— 独立线程读帧 + 人脸检测，写入队列供异步消费者读取。"""

import base64
import queue
import threading
import time
from typing import Optional

import cv2

from vision.face_utils import detect_and_draw_sync
from vision.identity_client import IdentificationClient
from logging_config import get_logger

logger = get_logger(__name__)


class CameraCapture:
    """独立线程采集摄像头帧，检测人脸后写入队列。

    队列容量 1，满时丢弃最旧帧（始终提供最新帧）。
    get_frame() 线程安全，供异步协程消费。
    """

    def __init__(
        self,
        source: str = "local",
        camera_index: int = 0,
        camera_width: int = 640,
        camera_height: int = 480,
        camera_fps: float = 5.0,
        foxglove_url: str = "",
        camera_topic: str = "",
        zenoh_url: str = "",
        zenoh_topic: str = "",
        face_client: Optional[IdentificationClient] = None,
    ):
        self._source = source
        self._camera_index = camera_index
        self._camera_width = camera_width
        self._camera_height = camera_height
        self._camera_fps = camera_fps
        self._foxglove_url = foxglove_url
        self._camera_topic = camera_topic
        self._zenoh_url = zenoh_url
        self._zenoh_topic = zenoh_topic
        self._face_client = face_client

        self._queue: queue.LifoQueue = queue.LifoQueue(maxsize=1)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def get_frame(self, timeout: float = 0.1) -> Optional[str]:
        """获取最新帧（base64 JPEG），非阻塞。"""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    # ── 内部 ────────────────────────────────────────────────────────

    def _put_frame(self, img_b64: Optional[str]):
        """写入队列，满时丢弃最旧帧。"""
        if img_b64 is None:
            return
        while True:
            try:
                self._queue.put_nowait(img_b64)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def _run(self):
        if self._source == "foxglove":
            self._run_foxglove()
        elif self._source == "zenoh":
            self._run_zenoh()
        else:
            self._run_local()

    def _run_local(self):
        import cv2 as cv

        cap = cv.VideoCapture(self._camera_index)
        cap.set(cv.CAP_PROP_FRAME_WIDTH, self._camera_width)
        cap.set(cv.CAP_PROP_FRAME_HEIGHT, self._camera_height)

        try:
            while not self._stop_event.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.1)
                    continue
                if self._face_client:
                    img_b64 = detect_and_draw_sync(self._face_client, frame)
                    self._put_frame(img_b64)
        finally:
            cap.release()

    def _run_foxglove(self):
        from foxglove_client import FoxgloveImageCapture

        topic = self._camera_topic or "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed"
        cap = FoxgloveImageCapture(topic=topic, url=self._foxglove_url)
        logger.info("[CameraCapture] foxglove started: %s", topic)

        try:
            while not self._stop_event.is_set():
                success, frame = cap.read(timeout=0.1)
                if not success or frame is None:
                    time.sleep(0.01)
                    continue
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                if self._face_client:
                    img_b64 = detect_and_draw_sync(self._face_client, frame_bgr)
                else:
                    _, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    img_b64 = base64.b64encode(buf).decode()
                self._put_frame(img_b64)
        finally:
            cap.release()

    def _run_zenoh(self):
        from zenoh_client import ZenohImageCapture

        topic = self._zenoh_topic or "camera/annotated"
        cap = ZenohImageCapture(topic=topic, url=self._zenoh_url or None, fps=self._camera_fps)

        try:
            while not self._stop_event.is_set():
                ret, frame = cap.read(timeout=0.1)
                if not ret or frame is None:
                    time.sleep(0.1)
                    continue
                # zenoh 帧已服务端预标注，直接编码
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                _, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
                img_b64 = base64.b64encode(buf).decode()
                self._put_frame(img_b64)
        finally:
            cap.release()