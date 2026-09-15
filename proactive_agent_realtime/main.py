#!/usr/bin/env python3
"""
小伴 — 具身智能康养管家（唤醒词模式）。

唤醒词"小伴小伴"唤醒 → Qwen Omni 长连接多轮对话（音频+视觉） → 休眠。

Usage:
  python main.py
  python main.py --debug
"""

import argparse
import asyncio
import os
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from logging_config import get_logger

from agent.interrupt_state import is_ai_speaking, interrupt_event, reset as reset_interrupt_state

logger = get_logger("main")


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ═══════════════════════════════════════════════════════════════
#  唤醒词模式 —— 系统休眠，喊"小伴小伴"唤醒后进入多轮对话
# ═══════════════════════════════════════════════════════════════

class WakeupApp:
    """多模态语音助手 —— 唤醒词 → Omni Agent 长连接多轮对话（音频+视觉）"""

    def __init__(self):
        import queue as _queue

        from audio import AudioCapture
        from wakeup_detector import WakeupDetector
        from vision import IdentificationClient
        from agent.utils import set_audio_queue, set_audio_capture
        from audio.playback_manager import AudioPlaybackManager
        from config import (
            MIC_DEVICE_INDEX, MIC_DEVICE_NAME,
            NOISE_GATE_ENABLED, NOISE_GATE_THRESHOLD, NOISE_GATE_ONSET_DELTA,
            NOISE_GATE_RELEASE_ALPHA, NOISE_GATE_CLOSE_RATIO,
            IDENTIFICATION_URL,
            EVENT_SERVER_ENABLED, EVENT_PORT,
            ACTION_EVENT_ENABLED,
            CAMERA_SOURCE, LOCAL_CAMERA_INDEX, LOCAL_CAMERA_WIDTH, LOCAL_CAMERA_HEIGHT,
            CAMERA_FPS, FOXGLOVE_BRIDGE_URL, CAMERA_TOPIC_HEAD, ZENOH_TOPIC, ZENOH_URL,
        )
        from events import EventServer, EventBuffer

        # --- 音频采集 ---
        self.audio_queue = _queue.Queue(maxsize=80)
        set_audio_queue(self.audio_queue)
        self.audio_capture = AudioCapture(
            device_index=MIC_DEVICE_INDEX,
            device_name=MIC_DEVICE_NAME,
            noise_gate_enabled=NOISE_GATE_ENABLED,
            noise_gate_threshold=NOISE_GATE_THRESHOLD,
            noise_gate_onset_delta=NOISE_GATE_ONSET_DELTA,
            noise_gate_release_alpha=NOISE_GATE_RELEASE_ALPHA,
            noise_gate_close_ratio=NOISE_GATE_CLOSE_RATIO,
        )
        set_audio_capture(self.audio_capture)

        # --- 唤醒检测 ---
        self.wakeup_detector = WakeupDetector(
            audio_capture=self.audio_capture,
            interrupted_event=interrupt_event(),
        )
        self._wakeup_event = threading.Event()

        # --- 人脸检测服务 ---
        self.face_client = IdentificationClient(IDENTIFICATION_URL)

        # --- 摄像头采集（持续运行，与 proactive_agent 一致） ---
        from camera_capture import CameraCapture
        self._camera_capture = CameraCapture(
            source=CAMERA_SOURCE,
            camera_index=LOCAL_CAMERA_INDEX,
            camera_width=LOCAL_CAMERA_WIDTH,
            camera_height=LOCAL_CAMERA_HEIGHT,
            camera_fps=CAMERA_FPS,
            foxglove_url=FOXGLOVE_BRIDGE_URL,
            camera_topic=CAMERA_TOPIC_HEAD,
            zenoh_url=ZENOH_URL,
            zenoh_topic=ZENOH_TOPIC,
            face_client=self.face_client,
        )

        # --- 事件系统 ---
        self._event_buffer = EventBuffer(wakeup_event=self._wakeup_event)

        # --- 音频播放 ---
        self.audio_pm = AudioPlaybackManager(self.audio_queue)

        # --- 统一事件服务器 ---
        self._event_server = EventServer(
            port=EVENT_PORT, event_buffer=self._event_buffer
        )
        self._event_server_enabled = EVENT_SERVER_ENABLED

    async def _fetch_all_users(self) -> list[dict]:
        """从识别服务拉取所有注册用户列表，失败时返回空列表。"""
        try:
            resp = await self.face_client.list_users_async()
            if isinstance(resp, list):
                return resp
            if isinstance(resp, dict):
                return resp.get("users", resp.get("data", []))
            return []
        except Exception as e:
            logger.warning(f"拉取注册用户列表失败: {e}")
            return []

    def run(self):
        """启动语音助手（阻塞主线程）。"""
        logger.info("唤醒词模式启动中...")
        self.wakeup_detector.load_model()

        self.audio_capture.start(self.audio_queue)
        self.wakeup_detector.start(self.audio_queue, self)

        if self._event_server_enabled:
            self._event_server.start()

        # 启动摄像头采集（持续运行，事件循环无关）
        self._camera_capture.start()

        try:
            asyncio.run(self._main_loop())
        except KeyboardInterrupt:
            logger.info("\n程序退出中...")
        finally:
            self._shutdown()

    def is_asr_active(self) -> bool:
        return self._wakeup_event.is_set()

    def is_speaking(self) -> bool:
        return is_ai_speaking()

    def on_wakeup(self):
        """唤醒词检测回调（由 WakeupDetector 线程调用）。"""
        if self.audio_pm.is_playing():
            logger.warning("[wakeup] 忽略：AI 正在讲话中")
            return
        self._wakeup_event.set()

    def _render_send(self, msg_type: str, **kwargs):
        rs = self._render_socket
        if rs is None or not rs.is_alive:
            return
        method = getattr(rs, f"send_{msg_type}", None)
        if method:
            asyncio.create_task(method(**kwargs))

    def _play_startup_sound(self):
        """播放启动音效，扬声器选择与 agent 说话一致（SPEAKER_DEVICE_NAME/INDEX）。"""
        from config import SPEAKER_DEVICE_INDEX, SPEAKER_DEVICE_NAME
        from audio.audio_output import AudioPlayer
        import soundfile as sf

        _startup_wav = Path(__file__).resolve().parent / "启动音效.wav"
        if not _startup_wav.exists():
            return
        try:
            sr = sf.info(str(_startup_wav)).samplerate
            player = AudioPlayer(
                sample_rate=sr,
                device_index=SPEAKER_DEVICE_INDEX,
                device_name=SPEAKER_DEVICE_NAME,
                pcm_dtype="float32",  # 与 agent 播放器保持一致
            )
            player.open()
            player.write(_startup_wav.read_bytes())  # RIFF 头自动识别，按设备采样率重采样
            player.drain()
            player.close()
            logger.info("[main] 启动音效播放完成")
        except Exception:
            logger.warning("[main] 启动音效播放失败", exc_info=True)

    async def _emote(self, name: str):
        """向机器人发 emote 表情命令（后台执行，不阻塞主流程）。"""
        from agent.tools import execute_shell
        try:
            result = await execute_shell(f"emote {name}")
            logger.info("[main] emote %s: %s", name, result)
        except Exception as e:
            logger.warning("[main] emote %s 失败: %s", name, e)

    async def _main_loop(self):
        """主异步循环：等待唤醒 → 长连接多轮对话 → 休眠。"""
        from agent.utils import clear_sleep_request
        from prompts import build_system_prompt
        from realtime_session import RealtimeSession
        from config import (
            RENDER_SOCKET_ENABLED, RENDER_SOCKET_URL, SENTENCE_TIMEOUT,
            ACTION_EVENT_ENABLED, EVENT_SERVER_ENABLED,
        )

        self._render_socket = None
        if RENDER_SOCKET_ENABLED:
            from render import RenderSocket
            self._render_socket = RenderSocket(RENDER_SOCKET_URL)
            await self._render_socket.connect()

        # 播放启动音效，提示系统就绪
        self._play_startup_sound()

        # 往大屏推送一条系统通知横幅
        self._render_send("system_event", event_name="system_startup", description="系统：小伴智能体大脑启动成功！")

        # 启动即处于睡眠态：先把机器人表情设为 default
        asyncio.create_task(self._emote("default"))

        try:
            while True:
                # 1. 等待唤醒
                await asyncio.get_running_loop().run_in_executor(None, self._wakeup_event.wait)
                # 注意: _wakeup_event 不能在这里 clear，它同时控制 is_asr_active()
                # 必须保持 set 状态以阻止 WakeupDetector 抢音频，会话结束后再 clear

                # 唤醒（语音唤醒词 / 系统事件，所有情况）→ 表情切 shy
                asyncio.create_task(self._emote("shy"))

                # 2. 收集排队事件
                all_events = self._event_buffer.drain_all()
                greeting_parts = []
                for e in all_events:
                    greeting_parts.append(f"[系统事件] 类型: {e.event_name}，描述: {e.description}")
                system_event_text = "\n".join(greeting_parts) if greeting_parts else ""

                greeting = system_event_text or "你好，小伴！"

                self._render_send("user_speech", text=greeting)

                # 3. 构建系统提示词
                all_users = await self._fetch_all_users()
                system_prompt = build_system_prompt(all_users=all_users)
                logger.info(f"===== 系统提示词 =====\n{system_prompt}\n===== END =====")

                # 4. 长连接多轮对话
                print("🤖 AI: ", end="", flush=True)

                def on_text(token: str):
                    print(token, end="", flush=True)

                session = RealtimeSession(
                    system_prompt=system_prompt,
                    audio_pm=self.audio_pm,
                    face_client=self.face_client,
                    camera_capture=self._camera_capture,
                    render_send=self._render_send,
                )

                try:
                    sleep_requested = await session.run(
                        audio_queue=self.audio_queue,
                        event_buffer=self._event_buffer,
                        on_text=on_text,
                        greeting_text=greeting,
                        timeout=SENTENCE_TIMEOUT,
                    )
                except Exception:
                    # 会话异常不能杀死整个进程，记录后回到休眠等待下次唤醒
                    logger.error("[wakeup] 会话异常退出", exc_info=True)
                    sleep_requested = False

                print()
                self.audio_pm.drain()
                if self.audio_pm.is_playing():
                    self.audio_pm.close()
                clear_sleep_request()
                # 兜底：会话异常退出时 run() 里的 reset 可能没执行到，这里再清一次，
                # 否则休眠期 is_ai_speaking() 残留 True 会吞掉唤醒词
                reset_interrupt_state()
                # 进入睡眠前（超时 / go_sleep 工具）→ 表情切回 default
                asyncio.create_task(self._emote("default"))
                self._wakeup_event.clear()
                # P3: 会话结束到 clear() 之间到达的事件会重新 set 唤醒信号，
                # clear 后需检查 buffer，避免该事件滞留到下次唤醒才被处理
                if self._event_buffer.pending():
                    logger.info("[wakeup] buffer 中仍有事件，立即重新唤醒")
                    self._wakeup_event.set()

                if sleep_requested:
                    logger.info("[wakeup] go_sleep，回到休眠")

        finally:
            if self._render_socket is not None:
                try:
                    await self._render_socket.close()
                except Exception:
                    pass
                self._render_socket = None
                logger.info("[Wakeup] 大屏渲染连接已释放")

    def _shutdown(self):
        self.audio_capture.stop()
        self.wakeup_detector.stop()
        self._event_server.stop()
        self._camera_capture.stop()


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════

def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def main():
    parser = argparse.ArgumentParser(description="小伴 — 具身智能康养管家")
    parser.add_argument("--debug", action="store_true", default=_env_bool("DEBUG"))
    args = parser.parse_args()

    from config import LOG_LEVEL, LOG_FILE, validate_config
    from logging_config import setup
    validate_config()
    setup(debug=args.debug or LOG_LEVEL.upper() == "DEBUG", log_file=LOG_FILE if LOG_FILE else None)

    print(f"[{ts()}] Mode: wakeup (唤醒词模式)")
    app = WakeupApp()
    app.run()


if __name__ == "__main__":
    main()