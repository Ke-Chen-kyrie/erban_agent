"""
语音助手主入口 —— 基于 LangGraph + Omni 模型的多模态语音助手。

架构：
  AudioCapture → WakeupDetector → Omni 应答 → ASR → Agent (LangGraph) → Omni Audio
                                  ↑ 视频录制同步对齐  ↑

ref 参考模式：LangGraph 作为核心编排器，本地层负责 mic/speaker/camera I/O。
"""

import asyncio
import queue
import threading

from audio import AudioCapture
from perception import WakeupDetector
from asr import ByteDanceASR, SherpaOnnxASR
from common import ChatSession
from perception import PerceptEnvironment, SpeechEvent, GestureEvent, TimeoutEvent
from video import VideoRecorder
from identity import IdentificationClient
from agent.utils import set_ident_client, set_user_info, set_audio_queue
from audio import AudioPlaybackManager
from config import (
    MIC_DEVICE_INDEX, MIC_DEVICE_NAME,
    IDENTIFICATION_URL,
    ASR_PROVIDER,
    EVENT_SERVER_ENABLED, EVENT_PORT,
    ACTION_EVENT_ENABLED,
)
from events import EventServer, EventBuffer
from logging_config import get_logger
from app.speaker_id import SpeakerIdentifier
from app.event_processor import EventProcessor
from app.gesture_handler import GestureHandler
from app.speech_handler import SpeechHandler
from app.response_streamer import ResponseStreamer

logger = get_logger(__name__)


class VoiceAssistantApp:
    """多模态语音助手 —— 唤醒词 → ASR → 声纹识别 → Omni Agent → 语音输出"""

    def __init__(self):
        # --- 音频采集 ---
        self.audio_queue = queue.Queue(maxsize=80)
        set_audio_queue(self.audio_queue)
        self.audio_capture = AudioCapture(
            device_index=MIC_DEVICE_INDEX,
            device_name=MIC_DEVICE_NAME,
        )

        # --- 唤醒检测 ---
        self.wakeup_detector = WakeupDetector(audio_capture=self.audio_capture)
        self._wakeup_event = threading.Event()
        self._wakeup_lock = threading.Lock()  # 保护 on_wakeup ↔ _main_loop 的跨线程写

        # --- ASR (云端 / 本地) ---
        if ASR_PROVIDER == "sherpa_onnx":
            logger.info("[ASR] 使用 sherpa-onnx 本地 ASR")
            self.asr = SherpaOnnxASR()
        else:
            logger.info("[ASR] 使用 ByteDance 云端 ASR")
            self.asr = ByteDanceASR()

        # --- Agent ---
        self.chat_session = ChatSession()

        # --- 视频录制 ---
        self.video_recorder = VideoRecorder()

        # --- 声纹识别 ---
        self.ident_client = IdentificationClient(IDENTIFICATION_URL)
        set_ident_client(self.ident_client)
        self._speaker_id = SpeakerIdentifier(self.ident_client)

        # --- 感知环境封装 ---
        self._event_buffer = EventBuffer(wakeup_event=self._wakeup_event)
        self._percept = PerceptEnvironment(
            asr=self.asr,
            video_recorder=self.video_recorder,
            audio_queue=self.audio_queue,
            event_buffer=self._event_buffer if ACTION_EVENT_ENABLED else None,
        )

        # --- 音频播放 & 打断检测 ---
        self.audio_pm = AudioPlaybackManager(self.audio_queue)

        # --- 打断标志（引用 audio_pm 的内部 Event） ---
        self._interrupted = self.audio_pm.interrupted
        self._interrupt_audio_buffer = self.audio_pm.interrupt_audio_buffer

        self._partial_ai_text = ""
        self._wakeup_audio_bytes: bytes = b""

        # --- 流式响应器 ---
        self._streamer = ResponseStreamer(
            chat_session=self.chat_session,
            audio_pm=self.audio_pm,
            interrupted=self._interrupted,
        )

        # --- 事件处理器 ---
        self._event_processor = EventProcessor(
            chat_session=self.chat_session,
            event_buffer=self._event_buffer,
            video_recorder=self.video_recorder,
            percept=self._percept,
            audio_pm=self.audio_pm,
            asr=self.asr,
            interrupted=self._interrupted,
            interrupt_audio_buffer=self._interrupt_audio_buffer,
            stream_fn=self._streamer.stream,
        )

        # --- 手势事件处理器 ---
        self._gesture_handler = GestureHandler(
            chat_session=self.chat_session,
            percept=self._percept,
            event_buffer=self._event_buffer,
            event_processor=self._event_processor,
            audio_pm=self.audio_pm,
            asr=self.asr,
            interrupted=self._interrupted,
            interrupt_audio_buffer=self._interrupt_audio_buffer,
            stream_fn=self._streamer.stream,
        )

        # --- 语音事件处理器 ---
        self._speech_handler = SpeechHandler(
            chat_session=self.chat_session,
            percept=self._percept,
            event_buffer=self._event_buffer,
            event_processor=self._event_processor,
            speaker_id=self._speaker_id,
            audio_pm=self.audio_pm,
            asr=self.asr,
            interrupted=self._interrupted,
            interrupt_audio_buffer=self._interrupt_audio_buffer,
            stream_fn=self._streamer.stream,
        )

        # --- 统一事件服务器 ---
        self._event_server = EventServer(
            port=EVENT_PORT, event_buffer=self._event_buffer
        )

    # ========== 对外入口 ==========

    def run(self):
        """启动语音助手（阻塞主线程）。"""
        logger.info("模块化语音助手启动中...")
        self.wakeup_detector.load_model()

        # 本地 ASR 模型在启动时预加载，避免首次 ASR 会话阻塞
        if ASR_PROVIDER == "sherpa_onnx":
            logger.info("[ASR] 预加载 sherpa-onnx 模型...")
            self.asr.preload()

        self.audio_capture.start(self.audio_queue)

        # 预加载可能说话人列表
        asyncio.run(self._speaker_id.refresh_speakers())

        # 启动唤醒检测线程，唤醒时触发 _on_wakeup
        self.wakeup_detector.start(self.audio_queue, self)

        # 启动统一事件服务器
        if EVENT_SERVER_ENABLED:
            self._event_server.start()

        try:
            asyncio.run(self._main_loop())
        except KeyboardInterrupt:
            logger.info("\n程序退出中...")
        finally:
            self._shutdown()

    # ========== WakeupDetector 回调接口 ==========

    def is_asr_active(self) -> bool:
        """WakeupDetector 查询：是否应将音频让给 ASR。"""
        return self.asr.is_asr_active()

    def is_speaking(self) -> bool:
        """WakeupDetector 查询：AI 是否正在讲话（此时应让出音频给打断检测）。"""
        return self.audio_pm.is_playing()

    def on_wakeup(self, wakeup_audio_bytes: bytes = b""):
        """唤醒词检测回调（由 WakeupDetector 线程调用）。"""
        if self.audio_pm.is_playing():
            # 防御：AI 正在讲话时不应被唤醒打断，由 wakeup_detector 侧检查拦截
            logger.warning("[wakeup] 忽略：AI 正在讲话中")
            return

        # 预留音频给 ASR，防止 KWS 在 ASR 启动前抢走用户语音
        self.asr.reserve_audio_for_asr()

        with self._wakeup_lock:
            self.chat_session.thread_id = "admin"
            self.chat_session.user_id = "unknown"
            self.chat_session.user_name = ""
            self.chat_session.user_role = ""
            self.chat_session.user_description = ""
            self.chat_session.possible_speakers = self._speaker_id.possible_speakers
            self._wakeup_audio_bytes = wakeup_audio_bytes
        self._wakeup_event.set()

    # ========== 内部实现 ==========

    async def _main_loop(self):
        """主异步循环：等待唤醒 → 确定首条消息 → 持续 ASR → 句子即到即分发 Agent。"""
        while True:
            # 1. 等待唤醒（语音唤醒词 或 系统事件）
            await asyncio.get_event_loop().run_in_executor(None, self._wakeup_event.wait)
            self._wakeup_event.clear()

            # 2. 收集排队事件，合并为首条消息
            all_events = self._event_buffer.drain_all()

            # 构建 greeting 文本
            greeting_parts = []
            for e in all_events:
                greeting_parts.append(f"[系统事件] 类型: {e.event_name}，描述: {e.description}")
            system_event_text = "\n".join(greeting_parts) if greeting_parts else ""

            # 3. 确定唤醒类型，准备首条消息
            if system_event_text:
                # 事件唤醒：跳过声纹识别，使用"系统"作为发送者身份
                self.chat_session.user_name = "系统"
                self.chat_session.user_id = "system"
                self.chat_session.user_role = ""
                self.chat_session.user_description = ""
                set_user_info("系统", "", "")
                logger.info(f"[event] 唤醒，共 {len(all_events)} 个事件")
                greeting = system_event_text
            elif self._wakeup_audio_bytes:
                # 语音唤醒：声纹识别 + 问候（原子取出唤醒音频）
                with self._wakeup_lock:
                    wakeup_audio = self._wakeup_audio_bytes
                    self._wakeup_audio_bytes = b""
                user_id = await self._speaker_id.identify(wakeup_audio)
                self.chat_session.user_id = user_id
                logger.info(f"[ident] 唤醒声纹识别: user_id={user_id}")
                user_name, user_role, user_description = await self._speaker_id.get_user_info(user_id)
                self.chat_session.user_name = user_name
                self.chat_session.user_role = user_role
                self.chat_session.user_description = user_description
                set_user_info(user_name, user_role, user_description)
                greeting = "你好，小伴！"
            else:
                continue

            # 4. 唤醒应答（预启动视频录制，捕获画面给 Agent）
            await asyncio.get_event_loop().run_in_executor(None, self._percept.prepare_video)
            video_media = await self._event_processor.capture_visual() if system_event_text else None
            event_media = EventProcessor.build_media_from_events(all_events) if system_event_text else None
            media = None
            if video_media and event_media:
                media = (video_media[0], video_media[1] + event_media[1])
            elif video_media:
                media = video_media
            elif event_media:
                media = event_media
            partial_text, was_interrupted, _ = await self._streamer.stream(
                "", user_text=greeting, media=media, send_visual=bool(media),
            )

            if was_interrupted:
                if partial_text:
                    greeting_text = f"{self.chat_session.user_name or '用户'}：你好，我回来了"
                    await self.chat_session.save_partial_response(partial_text, greeting_text)
                self._interrupt_audio_buffer.clear()

            # 确保 _interrupted 已清除再启动 ASR
            self._interrupted.clear()

            # 启动 ASR 前清空队列
            self.audio_pm.drain_audio_queue()

            # 5. 启动感知环境前，先处理排队系统事件
            result = await self._event_processor.process_pending()
            if result == "sleep":
                continue

            # 丢弃 TTS 期间积压的动作事件，避免假唤醒
            self._event_buffer.discard_actions()

            # 启动感知环境（ASR + VideoRecorder）
            self._percept.start()

            # 6. 事件分发循环
            while True:
                event = await self._percept.wait_for_input()

                if isinstance(event, TimeoutEvent):
                    self._percept.stop()
                    # 感知阶段结束：丢弃动作，处理剩余非动作事件
                    await self._event_processor.process_pending()
                    break

                if isinstance(event, GestureEvent):
                    sleep_requested = await self._gesture_handler.handle(event)
                    self._interrupted.clear()
                    if sleep_requested:
                        break
                    continue

                if isinstance(event, SpeechEvent):
                    sleep_requested = await self._speech_handler.handle(event)
                    if sleep_requested:
                        break

                self._interrupted.clear()

    def _shutdown(self):
        """清理资源。"""
        self.audio_capture.stop()
        self.wakeup_detector.stop()
        self._event_server.stop()


if __name__ == "__main__":
    import sys
    from logging_config import setup
    from config import LOG_LEVEL, LOG_FILE, validate_config

    validate_config()

    debug = "--debug" in sys.argv
    setup(debug=debug or LOG_LEVEL.upper() == "DEBUG", log_file=LOG_FILE if LOG_FILE else None)

    app = VoiceAssistantApp()
    app.run()