"""视频录制 —— 通过 Foxglove WebSocket 接收 ROS2 相机帧并写入视频文件."""

import asyncio
import queue
import threading
import time
from typing import Optional

import cv2

from config import FOXGLOVE_BRIDGE_URL, CAMERA_TOPIC_HEAD, CAMERA_FPS, CAMERA_SOURCE, LOCAL_CAMERA_INDEX, LOCAL_CAMERA_WIDTH, LOCAL_CAMERA_HEIGHT
from logging_config import get_logger

logger = get_logger(__name__)


class VideoRecorder:
    """录制相机视频，帧存内存不落盘。支持 Foxglove (ROS2) 和本地摄像头两种来源。"""

    def __init__(self, ws_url: str = FOXGLOVE_BRIDGE_URL,
                 camera_topic: str = CAMERA_TOPIC_HEAD,
                 fps: float = CAMERA_FPS,
                 camera_source: str = CAMERA_SOURCE,
                 camera_index: int = LOCAL_CAMERA_INDEX):
        self.ws_url = ws_url
        self.camera_topic = camera_topic
        self.fps = fps
        self.camera_source = camera_source
        self.camera_index = camera_index
        self._recording = threading.Event()
        self._frames: list = []
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._open_error: Optional[str] = None
        self._frame_queue: queue.Queue = queue.Queue(maxsize=50)
        self._recv_thread: Optional[threading.Thread] = None
        self._frame_callbacks: list = []
        if camera_source == "foxglove":
            logger.info(f"Foxglove Bridge: {self.ws_url}")
            logger.info(f"相机话题: {self.camera_topic}")
        else:
            logger.info(f"本地摄像头: /dev/video{self.camera_index}")

    def start(self):
        self._recording.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

        self._ready.clear()
        self._open_error = None
        self._frames = []
        # 清空残留帧队列，避免上一轮会话的旧帧污染新会话
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break
        self._recording.set()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)

    def stop(self) -> Optional[list]:
        """停止录制，返回帧列表（BGR numpy arrays），无数据则返回 None。"""
        self._ready.wait(timeout=3)
        self._recording.clear()
        if self._thread:
            self._thread.join(timeout=5)
        if self._open_error:
            logger.error(f"[video] stop() 相机打开失败: {self._open_error}")
            return None
        frames = self._frames
        self._frames = []
        if frames:
            logger.info(f"[video] stop() 返回 {len(frames)} 帧 ({frames[0].shape[1]}x{frames[0].shape[0]})")
            return frames
        logger.warning("[video] stop() 无帧数据")
        return None

    def is_recording(self) -> bool:
        return self._recording.is_set()

    def clear_frames(self):
        """清空已收集的帧缓冲和帧队列，不停止录制/连接。"""
        self._frames = []
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def add_frame_callback(self, cb):
        """注册帧回调，每收到一帧时调用 cb(bgr_frame)。"""
        if cb not in self._frame_callbacks:
            self._frame_callbacks.append(cb)

    def remove_frame_callback(self, cb):
        """取消已注册的帧回调。"""
        if cb in self._frame_callbacks:
            self._frame_callbacks.remove(cb)

    def _frame_receiver_loop(self):
        """后台线程：通过 Foxglove 接收 ROS2 相机帧，按 fps 频率放入队列。"""
        from video.foxglove_client import FoxgloveClient, decode_ros_image
        from common.async_utils import run_async_in_thread

        async def _run():
            client = FoxgloveClient(self.ws_url)
            try:
                await client.connect()
                logger.info(f"[video] Foxglove 已连接, topics 数量: {len(client.topics)}")
                if self.camera_topic not in client.topics:
                    self._open_error = f"Foxglove 话题不存在: {self.camera_topic}"
                    self._recording.clear()
                    self._ready.set()
                    logger.error(f"[video] {self._open_error}")
                    return

                await client.subscribe(self.camera_topic)
                logger.info(f"[video] 已订阅话题: {self.camera_topic}")
                self._ready.set()

                interval = 1.0 / self.fps
                frame_count = 0
                while self._recording.is_set():
                    result = await client.recv_message(timeout=0.1)
                    if result is not None:
                        topic, msg, _ = result
                        if topic == self.camera_topic:
                            img = decode_ros_image(msg)
                            if img is not None:
                                frame = img[:, :, ::-1]
                                try:
                                    self._frame_queue.put_nowait(frame)
                                    frame_count += 1
                                except queue.Full:
                                    pass
                    await asyncio.sleep(interval)
                logger.info(f"[video] _frame_receiver_loop 退出, 共接收 {frame_count} 帧")
            except Exception as e:
                self._open_error = f"Foxglove 相机接收异常: {e}"
                self._recording.clear()
                self._ready.set()
                logger.error(f"[video] _frame_receiver_loop 异常: {e}")
            finally:
                await client.close()

        run_async_in_thread(_run())

    def _local_camera_loop(self):
        """后台线程：通过 cv2.VideoCapture 读取本地摄像头帧，按 fps 频率放入队列。"""
        import cv2

        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            self._open_error = f"无法打开本地摄像头 /dev/video{self.camera_index}"
            self._recording.clear()
            self._ready.set()
            logger.error(f"[video] {self._open_error}")
            return

        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, LOCAL_CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, LOCAL_CAMERA_HEIGHT)
        self._ready.set()
        logger.info(f"[video] 本地摄像头已打开: /dev/video{self.camera_index}")

        interval = 1.0 / self.fps
        frame_count = 0
        while self._recording.is_set():
            ret, frame = cap.read()
            if ret and frame is not None:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                try:
                    self._frame_queue.put_nowait(frame_rgb)
                    frame_count += 1
                except queue.Full:
                    pass
            time.sleep(interval)

        cap.release()
        logger.info(f"[video] _local_camera_loop 退出, 共接收 {frame_count} 帧")

    def _record_loop(self):
        """从帧队列读取图像，存入内存列表。"""
        logger.info("[video] _record_loop 启动（内存模式）")
        if self.camera_source == "local":
            self._recv_thread = threading.Thread(target=self._local_camera_loop, daemon=True)
        else:
            self._recv_thread = threading.Thread(target=self._frame_receiver_loop, daemon=True)
        self._recv_thread.start()
        self._ready.wait(timeout=5)

        if self._open_error:
            logger.warning(f"[video] _record_loop 提前退出: _open_error={self._open_error!r}")
            self._recording.clear()
            return

        try:
            first_frame = self._frame_queue.get(timeout=2)
            frame_height, frame_width = first_frame.shape[:2]
            logger.info(f"[video] _record_loop 收到首帧: {frame_width}x{frame_height}")
        except queue.Empty:
            self._open_error = "超时未收到相机帧"
            self._recording.clear()
            logger.warning("[video] _record_loop 超时未收到相机帧")
            return

        self._frames.append(first_frame)
        frame_count = 1
        for cb in self._frame_callbacks:
            try:
                cb(first_frame)
            except Exception as e:
                logger.error(f"[video] frame callback error: {e}")
        try:
            while self._recording.is_set():
                try:
                    frame = self._frame_queue.get(timeout=0.5)
                    self._frames.append(frame)
                    frame_count += 1
                    for cb in self._frame_callbacks:
                        try:
                            cb(frame)
                        except Exception as e:
                            logger.error(f"[video] frame callback error: {e}")
                except queue.Empty:
                    continue
        finally:
            self._recording.clear()
            logger.info(f"[video] _record_loop 结束, 共收集 {frame_count} 帧")