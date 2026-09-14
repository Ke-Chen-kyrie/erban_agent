"""语音事件处理模块 —— 从 main.py 提取的 SpeechHandler。"""

import asyncio
import base64
import time

from perception import SpeechEvent, encode_media
from events.buffer import EventBuffer
from common import PerfMetrics
from app.speaker_id import SpeakerIdentifier
from app.event_processor import EventProcessor, StreamFn
from agent.agent import get_messages
from agent.router import detect_directed_intent
from agent.utils import (
    set_user_info, reset_shared_state,
    set_router_start_time, set_router_end_time,
)
from logging_config import get_logger

logger = get_logger(__name__)


class SpeechHandler:
    """处理音频 VAD 语音事件：5 路并行任务 + Agent 应答。"""

    def __init__(
        self,
        chat_session,
        percept,
        event_buffer: EventBuffer,
        event_processor: EventProcessor,
        speaker_id: SpeakerIdentifier,
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
        self._speaker_id = speaker_id
        self._audio_pm = audio_pm
        self._asr = asr
        self._interrupted = interrupted
        self._interrupt_audio_buffer = interrupt_audio_buffer
        self._stream = stream_fn

    async def handle(self, event: SpeechEvent) -> bool:
        """处理语音事件，返回 sleep_requested。"""
        self._percept.pause()

        # 感知阶段触发中断：清空队列，动作事件丢弃，非动作事件合并到用户消息
        all_events = self._event_buffer.drain_all()
        non_action = [e for e in all_events if e.event_type != "action"]
        if len(all_events) != len(non_action):
            logger.info(f"[system_event] 感知阶段丢弃 {len(all_events) - len(non_action)} 个动作事件")
        if non_action:
            event_texts = [f"[系统事件] 类型: {e.event_name}，描述: {e.description}" for e in non_action]
            event_text = "\n".join(event_texts)
            event.sentence = f"{event_text}\n{event.sentence}"
            logger.info(f"[system_event] 感知阶段收到 {len(non_action)} 个非动作事件，合并到用户消息")

        reset_shared_state()

        # --- 5 路并行任务 ---

        # 1. 声纹识别
        async def _identify_bg(wav_bytes: bytes):
            user_id = await self._speaker_id.identify(wav_bytes)
            self._chat_session.user_id = user_id
            logger.info(f"[ident] 识别结果: user_id={user_id}")
            user_name, user_role, user_description = await self._speaker_id.get_user_info(user_id)
            self._chat_session.user_name = user_name
            self._chat_session.user_role = user_role
            self._chat_session.user_description = user_description
            set_user_info(user_name, user_role, user_description)

        voiceprint_times = [0.0, 0.0]

        async def _voiceprint_timed(wav_bytes):
            voiceprint_times[0] = time.time()
            await _identify_bg(wav_bytes)
            voiceprint_times[1] = time.time()

        voiceprint_task = asyncio.create_task(
            _voiceprint_timed(base64.b64decode(event.audio_b64) if event.audio_b64 else b"")
        )

        # 2. 视频编码
        video_encode_times = [0.0, 0.0]

        async def _encode_bg(frames: list):
            if not frames:
                return None
            video_encode_times[0] = time.time()
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(None, encode_media, frames)
            except Exception as e:
                logger.error(f"[video] encode_media 异常: {type(e).__name__}: {e}")
                return None
            finally:
                video_encode_times[1] = time.time()
            return result

        video_task = asyncio.create_task(_encode_bg(event.video_frames))

        # 3. 预路由
        async def _pre_route_bg(user_text: str):
            set_router_start_time(time.time())
            result = await self._chat_session.pre_route(user_text)
            set_router_end_time(time.time())
            return result

        route_task = asyncio.create_task(_pre_route_bg(event.sentence))

        # 4. 对话对象判断
        directed_times = [0.0, 0.0]

        async def _directed_timed(user_text: str):
            directed_times[0] = time.time()
            messages = await get_messages(self._chat_session.thread_id)
            names = "、".join(u.get("name", "") for u in self._speaker_id.possible_speakers if u.get("name"))
            result = await detect_directed_intent(user_text, messages, known_names=names)
            directed_times[1] = time.time()
            return result

        directed_task = asyncio.create_task(_directed_timed(event.sentence))

        # 等待全部完成
        results = await asyncio.gather(
            voiceprint_task, video_task, route_task, directed_task,
            return_exceptions=True,
        )
        voiceprint_start, voiceprint_end = voiceprint_times[0], voiceprint_times[1]
        video_encode_start, video_encode_end = video_encode_times[0], video_encode_times[1]
        directed_start, directed_end = directed_times[0], directed_times[1]
        parallel_done = time.time()

        # --- 结果解析 ---
        video_result = results[1]
        route_result = results[2]
        directed_result = results[3]

        send_visual = True
        if isinstance(route_result, Exception):
            logger.error(f"[router] 预路由异常: {type(route_result).__name__}: {route_result}")
        elif route_result is not None:
            send_visual = route_result

        # 对话对象判断：不是对小伴说话 → 丢弃消息
        is_directed = True
        if isinstance(directed_result, Exception):
            logger.error(f"[directed] 对话对象判断异常: {type(directed_result).__name__}: {directed_result}")
        elif directed_result is not None:
            is_directed = directed_result
        if not is_directed:
            logger.info(f"[directed] 用户不是在对小伴说话，丢弃消息 (耗时 {directed_end - directed_start:.3f}s)")
            result = await self._event_processor.process_pending()
            if result == "sleep":
                return True
            self._event_buffer.discard_actions()
            self._percept.resume()
            return False

        # 构建媒体
        media = None
        if isinstance(video_result, Exception):
            logger.error(f"[video] _encode_bg 任务异常: {type(video_result).__name__}: {video_result}")
        elif video_result is not None:
            b64_list, mime, media_type = video_result
            if media_type == "video":
                videos = [b64_list] if send_visual else []
                images = []
            else:
                videos = []
                images = list(b64_list) if send_visual else []
            media = (videos, images)

        # 合并系统事件携带的图片
        event_media = EventProcessor.build_media_from_events(non_action)
        if event_media:
            if media:
                media = (media[0], media[1] + event_media[1])
            else:
                media = event_media

        # 构建性能指标
        perf = PerfMetrics()
        perf.speech_begin_time = event.speech_begin_time if event.speech_begin_time > 0 else None
        perf.asr_sentence_time = event.asr_time
        perf.voiceprint_start = voiceprint_start
        perf.voiceprint_end = voiceprint_end
        perf.video_encode_start = video_encode_start
        perf.video_encode_end = video_encode_end
        perf.parallel_done_time = parallel_done

        # Agent 应答
        partial_text, was_interrupted, sleep_requested = await self._stream(
            event.audio_b64, user_text=event.sentence, media=media, perf=perf,
            send_visual=send_visual,
        )

        # 恢复感知环境
        self._audio_pm.drain_audio_queue()
        self._asr.drain_sentences()

        if sleep_requested:
            logger.info("[sleep] Agent 请求睡眠，回到等待唤醒状态")
            self._percept.stop()
            return True

        if was_interrupted:
            if partial_text:
                await self._chat_session.save_partial_response(partial_text, event.sentence)
            self._interrupt_audio_buffer.clear()
            self._interrupted.clear()

        result = await self._event_processor.process_pending()
        if result == "sleep":
            return True
        self._event_buffer.discard_actions()
        self._percept.resume()

        return False
