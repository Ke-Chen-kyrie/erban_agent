"""统一事件缓冲 —— UnifiedEvent 数据类 + EventBuffer 线程安全缓冲。

去重策略：基于队列内已有事件 key，相同 key 已在队列中则跳过。
事件被 drain 后自动清除，可重新入队。
"""

import queue
import threading
from dataclasses import dataclass

from logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class UnifiedEvent:
    """统一事件。"""
    event_name: str      # "wave" | "nod" | "fall" | 任意名称
    description: str     # "小明在点头" | "检测到老人摔倒" 等
    image_base64: str = ""       # 可选：base64 编码的图片
    image_mime_type: str = ""    # 可选：图片 MIME 类型，默认 image/jpeg
    video_base64: str = ""       # 可选：base64 编码的视频
    video_mime_type: str = ""    # 可选：视频 MIME 类型，默认 video/avi


class EventBuffer:
    """线程安全统一事件缓冲，支持队列级去重。"""

    def __init__(self, wakeup_event: threading.Event | None = None, on_event: callable = None):
        self._queue: queue.Queue[UnifiedEvent] = queue.Queue()
        self._dedup_keys: set[str] = set()
        self._lock = threading.Lock()
        self._wakeup_event = wakeup_event
        self._on_event = on_event

    # ---- Write ----

    def put(self, event: UnifiedEvent) -> bool:
        """入队。返回 True 表示入队成功，False 表示被去重跳过。"""
        key = event.event_name
        with self._lock:
            if key in self._dedup_keys:
                logger.info(f"[event_buffer] 去重跳过: {key}")
                return False
            self._dedup_keys.add(key)

        self._queue.put(event)
        if self._wakeup_event:
            self._wakeup_event.set()
        if self._on_event:
            try:
                self._on_event()
            except Exception:
                logger.error("[event_buffer] _on_event 回调异常", exc_info=True)
        logger.info(f"[event_buffer] 入队: name={event.event_name}, desc={event.description}")
        return True

    def drain_all(self) -> list[UnifiedEvent]:
        """排空所有事件，清空去重 set。"""
        events: list[UnifiedEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            self._dedup_keys.clear()
        return events

    def pending(self) -> bool:
        """队列中是否还有未 drain 的事件。"""
        return not self._queue.empty()