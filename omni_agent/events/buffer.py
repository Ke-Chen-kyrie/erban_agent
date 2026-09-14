"""统一事件缓冲 —— UnifiedEvent 数据类 + EventBuffer 线程安全缓冲。

去重策略：基于队列内已有事件 key，相同 key 已在队列中则跳过。
事件被 drain 后自动清除，可重新入队。
"""

import queue
import threading
from dataclasses import dataclass

from logging_config import get_logger

logger = get_logger(__name__)

# 与动作事件重名的系统事件类型，在入队时直接丢弃
_ACTION_OVERLAP_NAMES = {
    "waving", "holding_chest", "holding_head", "holding_abdomen",
    "yawning", "rubbing_eye",
    "ok_sign", "thumbs_up", "raising_hand", "v_sign", "shoulder_pat",
    "nod", "shake_head",
}


@dataclass
class UnifiedEvent:
    """统一事件。event_type 为 "action" 时走动作事件管道，其他类型均为通用系统事件。"""
    event_type: str      # "action" | "fall_detected" | "weather" | 任意类型
    event_name: str      # "wave" | "nod" | 任意名称
    description: str     # "小明在点头" | "检测到老人摔倒" 等
    image_base64: str = ""       # 可选：base64 编码的图片
    image_mime_type: str = ""    # 可选：图片 MIME 类型，默认 image/jpeg
    video_base64: str = ""       # 可选：base64 编码的视频
    video_mime_type: str = ""    # 可选：视频 MIME 类型，默认 video/avi


class EventBuffer:
    """线程安全统一事件缓冲，支持队列级去重和按类型排空。"""

    def __init__(self, wakeup_event: threading.Event | None = None):
        self._queue: queue.Queue[UnifiedEvent] = queue.Queue()
        self._dedup_keys: set[str] = set()
        self._lock = threading.Lock()
        self._wakeup_event = wakeup_event

    # ---- Write ----

    def put(self, event: UnifiedEvent) -> bool:
        """入队。返回 True 表示入队成功，False 表示被过滤/去重跳过。"""
        # 非动作事件：event_name 与动作类型重名则丢弃（走 action 管道）
        if event.event_type != "action" and event.event_name in _ACTION_OVERLAP_NAMES:
            logger.info(f"[event_buffer] 丢弃与动作事件重名的事件: type={event.event_type}, name={event.event_name}")
            return False

        key = event.event_name
        with self._lock:
            if key in self._dedup_keys:
                logger.info(f"[event_buffer] 去重跳过: {key}")
                return False
            self._dedup_keys.add(key)

        self._queue.put(event)
        if self._wakeup_event:
            self._wakeup_event.set()
        logger.info(f"[event_buffer] 入队: type={event.event_type}, name={event.event_name}, desc={event.description}")
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

    def drain_by_type(self, event_type: str) -> list[UnifiedEvent]:
        """排空指定类型的事件，不匹配的放回队列。"""
        all_events = self.drain_all()
        result = [e for e in all_events if e.event_type == event_type]
        others = [e for e in all_events if e.event_type != event_type]
        with self._lock:
            for e in others:
                self._dedup_keys.add(e.event_name)
        for e in others:
            self._queue.put(e)
        return result

    def discard_actions(self):
        """丢弃所有动作事件。"""
        discarded = self.drain_by_type("action")
        if discarded:
            logger.info(f"[event_buffer] 丢弃 {len(discarded)} 个动作事件")