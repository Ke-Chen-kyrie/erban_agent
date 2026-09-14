"""手势事件处理模块 —— 从 main.py 提取的 GestureHandler。"""

import asyncio
import time

from perception import GestureEvent, encode_media
from events.buffer import EventBuffer
from common import PerfMetrics
from app.event_processor import EventProcessor, StreamFn
from agent.utils import set_user_info
from config import VIDEO_KEY_FRAME_MAX
from logging_config import get_logger

logger = get_logger(__name__)


class GestureHandler:
    """处理视频 VAD 手势事件。"""

    def __init__(
        self,
        chat_session,
        percept,
        event_buffer: EventBuffer,
        event_processor: EventProcessor,
        audio_pm,
        asr,
        interrupted,
        interrupt_audio_buffer: list,
        stream_fn: StreamFn,
    ):
        self._chat_session = chat_session
        self._percept = percept
        self._event_buffer = event_buffer
        self._event_processor = event_processor
        self._audio_pm = audio_pm
        self._asr = asr
        self._interrupted = interrupted
        self._interrupt_audio_buffer = interrupt_audio_buffer
        self._stream = stream_fn

    async def handle(self, event: GestureEvent) -> bool:
        """处理手势事件，返回 sleep_requested。"""
        self._percept.pause()

        logger.info(f"[gesture] 手势类型: {event.gesture_types}, subjects: {event.subjects}")

        self._chat_session.user_name = "系统"
        self._chat_session.user_id = "system"
        self._chat_session.user_role = ""
        self._chat_session.user_description = ""
        set_user_info("系统", "", "")

        # 编码视频
        media = None
        if event.video_frames:
            loop = asyncio.get_event_loop()
            try:
                video_result = await loop.run_in_executor(None, encode_media, event.video_frames, VIDEO_KEY_FRAME_MAX * 4)
                if video_result:
                    b64_list, mime, media_type = video_result
                    if media_type == "video":
                        media = ([b64_list], [])
                    else:
                        media = ([], list(b64_list))
            except Exception as e:
                logger.error(f"[gesture] 视频编码失败: {e}")

        # 合并事件自带的图片/视频
        event_images = (event.event_images or [])
        event_videos = (event.event_videos or [])
        if event_images or event_videos:
            if media:
                media = (media[0] + event_videos, media[1] + event_images)
            else:
                media = (event_videos, event_images)

        # 发送给 Agent
        perf = PerfMetrics()
        perf.agent_start_time = time.time()

        # 感知阶段触发中断：清空队列，动作事件丢弃，非动作事件合并到手势消息
        all_events = self._event_buffer.drain_all()
        non_action = [e for e in all_events if e.event_type != "action"]
        if len(all_events) != len(non_action):
            logger.info(f"[system_event] 感知阶段丢弃 {len(all_events) - len(non_action)} 个动作事件")
        if non_action:
            event_texts = [f"[系统事件] 类型: {e.event_name}，描述: {e.description}" for e in non_action]
            event_text = "\n".join(event_texts)
            logger.info(f"[system_event] 感知阶段收到 {len(non_action)} 个非动作事件，合并到手势消息")

        gesture_text = f"[系统事件] 类型: {', '.join(event.gesture_types)}，描述: {'，'.join(event.subjects)}"
        if non_action:
            gesture_text = f"{event_text}\n{gesture_text}"

        partial_text, was_interrupted, sleep_requested = await self._stream(
            audio_base64=event.audio_b64,
            user_text=gesture_text,
            media=media,
            perf=perf,
            send_visual=True,
            gesture_mode=True,
        )

        if sleep_requested:
            logger.info("[sleep] Agent 请求睡眠，回到等待唤醒状态")
            self._percept.stop()
            return True

        if was_interrupted:
            if partial_text:
                await self._chat_session.save_partial_response(partial_text, "")
            self._interrupt_audio_buffer.clear()

        # 恢复感知环境
        self._audio_pm.drain_audio_queue()
        self._asr.drain_sentences()
        result = await self._event_processor.process_pending()
        if result == "sleep":
            return True
        self._event_buffer.discard_actions()
        self._percept.resume()
        return False
