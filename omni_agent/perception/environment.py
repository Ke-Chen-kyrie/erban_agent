"""
感知环境封装 —— 统一管理 ASR + VideoRecorder + 动作事件 生命周期和 VAD 状态机。

对外暴露单一接口 await wait_for_input() → PerceptEvent:
  - SpeechEvent: 音频 VAD 触发（用户说话，优先级最高）
  - GestureEvent: 动作事件兜底（无声 + 外部动作检测推送）
  - TimeoutEvent: 总超时

录制与感知窗口一致：start() 开始录制，wait_for_input() 返回时停止。
"""

import asyncio
import base64
import queue
import time
from dataclasses import dataclass

from config import (
    SENTENCE_TIMEOUT,
    ACTION_EVENT_ENABLED,
)
from asr.base_asr import SentenceResult
from logging_config import get_logger

logger = get_logger(__name__)

# 动作事件类型直接透传英文名给 LLM


# ---------- 事件类型 ----------

class PerceptEvent:
    """感知事件基类。"""
    pass


@dataclass
class SpeechEvent(PerceptEvent):
    """音频 VAD 触发：用户说了一句话。"""
    sentence: str
    audio_b64: str
    video_frames: list
    speech_begin_time: float
    asr_time: float


@dataclass
class GestureEvent(PerceptEvent):
    """动作事件兜底：无声 + 外部动作检测命中。"""
    gesture_types: list[str]   # ["点头", "挥手", ...]  去重后的中文手势列表
    subjects: list[str]        # ["小明", "小红", ...]  与 gesture_types 一一对应
    audio_b64: str
    video_frames: list
    user_name: str
    user_id: str
    user_role: str
    user_description: str
    event_images: list = None  # [{image_base64, mime_type}, ...]  从事件中提取的图片
    event_videos: list = None  # [{video_base64, mime_type}, ...]  从事件中提取的视频


class TimeoutEvent(PerceptEvent):
    """总超时，无语音无动作。"""
    pass


# ---------- PerceptEnvironment ----------

class PerceptEnvironment:
    """管理 ASR + VideoRecorder + 动作事件 生命周期，封装语音优先的 VAD 状态机。"""

    def __init__(self, asr, video_recorder, audio_queue, event_buffer=None):
        self._asr = asr
        self._video_recorder = video_recorder
        self._audio_queue = audio_queue
        self._event_buffer = event_buffer

        self._last_speech_time = 0.0
        self._original_speech_begin_cb = asr.get_speech_begin_callback()
        self._callback_wrapped = False

    # ---- 生命周期 ----

    def prepare_video(self):
        """预启动视频录制（Agent 应答期间并行调用），避免 start() 时阻塞等首帧。"""
        self._video_recorder.start()
        logger.info("[percept] 视频录制已预启动")

    def start(self):
        """激活 ASR + 视频录制 + 动作事件。录制持续到 wait_for_input() 返回。"""
        self._asr.deactivate_listening()
        self._asr.drain_sentences()
        self._asr.snapshot_audio()
        self._asr.activate_listening()
        if self._video_recorder.is_recording():
            self._video_recorder.clear_frames()
        else:
            self._video_recorder.start()
        self._last_speech_time = 0.0

        # 清空上一轮积累的动作事件（Agent 应答期间到达的）
        if self._event_buffer:
            self._event_buffer.discard_actions()

        if not self._callback_wrapped:
            original_begin = self._original_speech_begin_cb

            def _on_speech_begin():
                self._last_speech_time = time.time()
                if self._event_buffer:
                    self._event_buffer.discard_actions()
                if original_begin:
                    original_begin()

            self._asr.set_speech_begin_callback(_on_speech_begin)
            self._callback_wrapped = True

        import threading
        threading.Thread(
            target=self._asr.start_listening,
            args=(self._audio_queue,),
            daemon=True,
        ).start()

        logger.info("[percept] 感知环境已启动")

    def stop(self):
        """停止 ASR + 视频录制。"""
        self._asr.deactivate_listening()
        self._video_recorder.stop()
        self._asr.drain_sentences()
        self._asr.snapshot_audio()
        self._asr.set_speech_begin_callback(self._original_speech_begin_cb)
        self._callback_wrapped = False
        logger.info("[percept] 感知环境已停止")

    def pause(self):
        """暂停感知（Agent 处理期间）：暂停 ASR。录制已在 wait_for_input 返回时停止。"""
        self._asr.pause()
        logger.info("[percept] 感知环境已暂停")

    def resume(self):
        """恢复感知（Agent 处理完毕后）：恢复 ASR + 启动录制。

        处理两种情况：
          - SpeechEvent: ASR 仅暂停（_active 仍为 True），调用 _asr.resume() 即可
          - GestureEvent: ASR 已被 deactivate_listening() 关闭，需重新激活并启动线程
        """
        self._asr.drain_sentences()
        self._asr.snapshot_audio()

        if not self._asr.is_asr_active():
            self._asr.activate_listening()
            import threading
            threading.Thread(
                target=self._asr.start_listening,
                args=(self._audio_queue,),
                daemon=True,
            ).start()
        else:
            self._asr.resume()

        if not self._video_recorder.is_recording():
            self._video_recorder.start()

        self._last_speech_time = 0.0
        if self._event_buffer:
            self._event_buffer.discard_actions()
        logger.info("[percept] 感知环境已恢复")

    def drain_audio(self):
        """清空音频队列中残留的音频。"""
        drained = 0
        while True:
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.task_done()
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.info(f"[percept] 丢弃残余音频 {drained} 帧")

    # ---- 核心接口 ----

    async def wait_for_input(self) -> PerceptEvent:
        """等待感知输入，返回 PerceptEvent 子类。

        语音优先的 VAD 状态机：
          1. 优先检查 ASR 句子队列（音频 VAD 最高优先级）
          2. 每 2s 轮询动作事件队列（无声兜底）
          3. 总超时 → TimeoutEvent

        录制在返回前停止，视频帧覆盖整个感知窗口。
        """
        try:
            if not ACTION_EVENT_ENABLED or self._event_buffer is None:
                return await self._wait_speech_only()
            return await self._wait_speech_and_action()
        finally:
            if self._video_recorder.is_recording():
                self._video_recorder.stop()

    async def _wait_speech_and_action(self) -> PerceptEvent:
        total_timeout = SENTENCE_TIMEOUT
        total_deadline = time.time() + total_timeout
        _last_speech_seen = 0.0
        next_action_check = time.time() + 2.0

        while True:
            if self._last_speech_time > _last_speech_seen:
                _last_speech_seen = self._last_speech_time
                total_deadline = time.time() + total_timeout

            # 1. 语音优先
            result = self._asr.get_sentence()
            if result:
                video_frames = self._video_recorder.stop()
                return self._build_speech_event(result, video_frames)

            if not self._asr.is_asr_active():
                # ASR 异常退出 → 动作兜底
                actions = self._drain_action_events()
                if actions:
                    gesture_types = [a.event_name for a in actions]
                    subjects = [a.description for a in actions]
                    user_name = "、".join(s for s in subjects if s)
                    event_images, event_videos = self._extract_event_media(actions)
                    logger.info(f"[percept] 动作事件命中: {gesture_types}, subjects: {subjects}")
                    video_frames = self._video_recorder.stop()
                    wav_bytes = self._asr.save_audio_to_wav()
                    audio_b64 = base64.b64encode(wav_bytes).decode("utf-8") if wav_bytes else ""
                    return GestureEvent(
                        gesture_types=gesture_types,
                        subjects=subjects,
                        audio_b64=audio_b64,
                        video_frames=video_frames or [],
                        user_name=user_name,
                        user_id="",
                        user_role="",
                        user_description="",
                        event_images=event_images,
                        event_videos=event_videos,
                    )
                self._video_recorder.stop()
                return TimeoutEvent()

            # 2. 每 2s 检查动作事件
            if time.time() >= next_action_check:
                next_action_check = time.time() + 2.0
                if self._last_speech_time > 0:
                    self._event_buffer.discard_actions()
                else:
                    actions = self._drain_action_events()
                    if actions:
                        gesture_types = [a.event_name for a in actions]
                        subjects = [a.description for a in actions]
                        user_name = "、".join(s for s in subjects if s)
                        event_images, event_videos = self._extract_event_media(actions)
                        logger.info(f"[percept] 动作事件命中: {gesture_types}, subjects: {subjects}")
                        video_frames = self._video_recorder.stop()
                        self._asr.deactivate_listening()
                        wav_bytes = self._asr.save_audio_to_wav()
                        audio_b64 = base64.b64encode(wav_bytes).decode("utf-8") if wav_bytes else ""
                        return GestureEvent(
                            gesture_types=gesture_types,
                            subjects=subjects,
                            audio_b64=audio_b64,
                            video_frames=video_frames or [],
                            user_name=user_name,
                            user_id="",
                            user_role="",
                            user_description="",
                            event_images=event_images,
                            event_videos=event_videos,
                        )

            # 3. 总超时
            if time.time() > total_deadline:
                logger.info(f"[percept] {total_timeout:.0f}s 总超时")
                self._asr.deactivate_listening()
                video_frames = self._video_recorder.stop()
                return self._flush_remaining_speech(video_frames)

            await asyncio.sleep(0.1)

    async def _wait_speech_only(self) -> PerceptEvent:
        """动作事件禁用时：仅等音频 VAD 或总超时。"""
        timeout = SENTENCE_TIMEOUT
        while True:
            result = self._asr.get_sentence()
            if result:
                video_frames = self._video_recorder.stop()
                return self._build_speech_event(result, video_frames)
            if not self._asr.is_asr_active():
                self._video_recorder.stop()
                return TimeoutEvent()
            if time.time() - self._asr.last_speech_time > timeout:
                break
            await asyncio.sleep(0.1)

        logger.info(f"[percept] {timeout:.0f}s 无语音，自动退出")
        self._asr.deactivate_listening()
        video_frames = self._video_recorder.stop()
        return self._flush_remaining_speech(video_frames)

    # ---- 内部辅助 ----

    def _drain_action_events(self) -> list:
        """从 EventBuffer 取出所有动作事件（去重已在入队时完成）。"""
        if self._event_buffer is None:
            return []
        return self._event_buffer.drain_by_type("action")

    @staticmethod
    def _extract_event_media(actions: list) -> tuple:
        """从动作事件列表中提取图片和视频。返回 (images, videos)。"""
        images = []
        videos = []
        for a in actions:
            if a.image_base64:
                mime = a.image_mime_type or "image/jpeg"
                images.append({"image_base64": a.image_base64, "mime_type": mime})
            if a.video_base64:
                mime = a.video_mime_type or "video/avi"
                videos.append({"video_base64": a.video_base64, "mime_type": mime})
        return (images, videos)

    def _build_speech_event(self, result: SentenceResult, video_frames: list) -> SpeechEvent:
        """从 SentenceResult + 视频帧构造 SpeechEvent。"""
        audio_b64 = base64.b64encode(result.wav_bytes).decode("utf-8") if result.wav_bytes else ""
        return SpeechEvent(
            sentence=result.sentence,
            audio_b64=audio_b64,
            video_frames=video_frames or [],
            speech_begin_time=result.speech_begin_time if result.speech_begin_time > 0 else 0.0,
            asr_time=result.timestamp,
        )

    def _flush_remaining_speech(self, video_frames: list) -> PerceptEvent:
        """超时后清空残留句子，返回 SpeechEvent 或 TimeoutEvent。"""
        deadline = time.time() + 3
        while time.time() < deadline:
            result = self._asr.get_sentence()
            if result:
                return self._build_speech_event(result, video_frames)
        return TimeoutEvent()