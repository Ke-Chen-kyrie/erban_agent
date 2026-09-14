"""系统事件处理模块 —— 从 main.py 提取的 EventProcessor。"""

import asyncio
import time
from typing import Callable, Awaitable

from events.buffer import EventBuffer
from perception import encode_media
from common import PerfMetrics
from agent.utils import set_user_info
from logging_config import get_logger

logger = get_logger(__name__)

# stream_fn 签名: (audio_base64, user_text, media, perf, send_visual, **kwargs) -> tuple[str, bool, bool]
StreamFn = Callable[..., Awaitable[tuple[str, bool, bool]]]


class EventProcessor:
    """处理系统事件：drain → 丢弃动作事件 → 视觉捕获 → Agent 调用 → 恢复用户信息。"""

    def __init__(
        self,
        chat_session,
        event_buffer: EventBuffer,
        video_recorder,
        percept,
        audio_pm,
        asr,
        interrupted,
        interrupt_audio_buffer: list,
        stream_fn: StreamFn,
    ):
        self._chat_session = chat_session
        self._event_buffer = event_buffer
        self._video_recorder = video_recorder
        self._percept = percept
        self._audio_pm = audio_pm
        self._asr = asr
        self._interrupted = interrupted
        self._interrupt_audio_buffer = interrupt_audio_buffer
        self._stream = stream_fn

    async def capture_visual(self) -> tuple | None:
        """为系统事件捕获并编码视频帧。返回 media tuple 或 None。"""
        if not self._video_recorder.is_recording():
            return None
        await asyncio.sleep(0.5)
        frames = await asyncio.get_event_loop().run_in_executor(
            None, self._video_recorder.stop
        )
        if not frames:
            return None
        video_result = await asyncio.get_event_loop().run_in_executor(
            None, encode_media, frames
        )
        if not video_result:
            return None
        b64_list, mime, media_type = video_result
        if media_type == "video":
            return ([b64_list], [])
        else:
            return ([], list(b64_list))

    @staticmethod
    def build_media_from_events(events: list) -> tuple | None:
        """从事件列表中提取图片和视频，构建 media tuple (videos_base64, images_base64)。"""
        images = []
        videos = []
        for e in events:
            if e.image_base64:
                mime = e.image_mime_type or "image/jpeg"
                images.append({"image_base64": e.image_base64, "mime_type": mime})
            if e.video_base64:
                mime = e.video_mime_type or "video/avi"
                videos.append({"video_base64": e.video_base64, "mime_type": mime})
        if not images and not videos:
            return None
        return (videos, images)

    async def process_pending(self) -> str:
        """TTS 后、进入感知前：丢弃动作事件，处理非动作事件。返回 'sleep' / 'done' / 'none'。"""
        self._event_buffer.discard_actions()
        events = self._event_buffer.drain_all()
        if not events:
            return "none"

        texts = [f"[系统事件] 类型: {e.event_name}，描述: {e.description}" for e in events]
        system_text = "\n".join(texts)

        logger.info(f"[system_event] TTS后处理 {len(events)} 个事件")

        # 保存当前用户信息，系统事件处理完后恢复
        saved = (
            self._chat_session.user_name,
            self._chat_session.user_id,
            self._chat_session.user_role,
            self._chat_session.user_description,
        )
        self._chat_session.user_name = "系统"
        self._chat_session.user_id = "system"
        self._chat_session.user_role = ""
        self._chat_session.user_description = ""
        set_user_info("系统", "", "")

        perf = PerfMetrics()
        perf.agent_start_time = time.time()
        video_media = await self.capture_visual()
        event_media = self.build_media_from_events(events)
        media = None
        if video_media and event_media:
            media = (video_media[0], video_media[1] + event_media[1])
        elif video_media:
            media = video_media
        elif event_media:
            media = event_media
        partial_text, was_interrupted, sleep_requested = await self._stream(
            audio_base64="",
            user_text=system_text,
            media=media,
            perf=perf,
            send_visual=bool(media),
        )

        self._chat_session.user_name, self._chat_session.user_id, self._chat_session.user_role, self._chat_session.user_description = saved
        if saved[1]:
            set_user_info(saved[0], saved[2], saved[3])

        if sleep_requested:
            logger.info("[sleep] 系统事件处理后 Agent 请求睡眠")
            self._percept.stop()
            return "sleep"

        if was_interrupted:
            if partial_text:
                await self._chat_session.save_partial_response(partial_text, "")
            self._interrupt_audio_buffer.clear()
            self._interrupted.clear()

        self._audio_pm.drain_audio_queue()
        self._asr.drain_sentences()
        return "done"
