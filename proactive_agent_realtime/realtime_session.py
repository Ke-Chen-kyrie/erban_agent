#!/usr/bin/env python3
"""RealtimeSession —— 长连接 Qwen Omni Realtime 会话，支持多轮对话 + 事件注入。"""

import asyncio
import json
import queue
import time
from typing import Callable, Optional

import numpy as np

from qwen_realtime_client import QwenRealtimeClient
from agent.tools import build_tools_for_realtime, execute_shell, execute_tool, is_local_cli_shell
from agent.voice_state import get_current_voice
from agent.interrupt_state import (
    consume_interrupt,
    is_ai_speaking,
    is_tool_audio_active,
    set_ai_speaking,
    reset as reset_interrupt_state,
)
from audio.audio_output import AudioPlayer
from config import (
    DASHSCOPE_API_KEY,
    DASHSCOPE_WORKSPACE_ID,
    QWEN_OMNI_MODEL,
    QWEN_OMNI_REGION,
    OMNI_SAMPLE_RATE,
    SENTENCE_TIMEOUT,
    SPEAKER_DEVICE_NAME,
    INTERRUPT_MODE,
)
from logging_config import get_logger
from langfuse import get_client

logger = get_logger(__name__)

# "不说话"专用表情：模型按提示词约定只回这个 emoji 时不会合成任何音频，会卡死服务端、
# ~10s 后 ModelServingError 崩断。检测到即主动打断。
_EMOJI_NO_SPEAK = "😊"


class RealtimeSession:
    """长连接 Qwen Omni Realtime 会话。

    唤醒后创建一次，持续喂音频，VAD 自动切分多轮对话，模型在同一次会话内记住上下文。
    支持中途注入手势/系统事件，go_sleep 或超时后结束。
    """

    def __init__(
        self,
        system_prompt: str,
        audio_pm,          # AudioPlaybackManager
        face_client=None,  # IdentificationClient
        camera_capture=None,  # CameraCapture（由 WakeupApp 管理生命周期）
        voice: str = None,
        sample_rate: int = 16000,
        output_sample_rate: int = 24000,
        render_send=None,  # Callable[[str, **kwargs], None]
    ):
        self._system_prompt = system_prompt
        self._audio_pm = audio_pm
        self._face_client = face_client
        self._camera_capture = camera_capture
        self._voice = voice or get_current_voice()
        self._sample_rate = sample_rate
        self._output_sample_rate = output_sample_rate
        self._tools = build_tools_for_realtime()
        self._render_send = render_send

        self._client: Optional[QwenRealtimeClient] = None
        self._stop_event = asyncio.Event()
        self._sleep_requested = False
        self._partial_text = ""
        self._token_index = 0
        self._last_activity = 0.0
        self._user_speaking = False
        self._agent_speaking = False
        self._tool_executing = False
        self._response_epoch = 0       # response.done 时递增，标记"新 turn 需要先发音频"
        self._audio_sent_epoch = -1    # 上次发音频时的 epoch，== _response_epoch 才允许发图片
        self._turn_interrupted = False
        self._injection_pending = False  # 上一次注入的 response.created 尚未到达，防止重复注入撞「已激活响应」

        # Langfuse tracing
        self._langfuse = get_client()
        self._trace_root = None
        self._trace_id = None
        self._current_generation = None
        self._session_start_time: float = 0.0
        self._speech_started_time: float = 0.0
        self._user_speech_duration_ms: float = 0.0
        self._e2e_latency_ms: float = 0.0
        self._turn_start_time: float = 0.0
        self._first_token_time: float = 0.0
        self._agent_speaking_start: float = 0.0
        self._turn_index: int = 0
        self._generation_index: int = 0
        self._tool_call_count: int = 0
        self._last_transcription: str = ""

    # ── public API ──────────────────────────────────────────────────

    async def run(
        self,
        audio_queue: queue.Queue,
        event_buffer=None,
        on_text: Callable[[str], None] = None,
        greeting_text: str = None,
        timeout: float = None,
    ) -> bool:
        """运行长连接会话，阻塞直到 go_sleep / timeout / interrupted。

        Returns:
            True 如果 go_sleep 触发，False 如果是超时或中断。
        """
        if timeout is None:
            timeout = SENTENCE_TIMEOUT

        # 重置打断状态，避免上一会话遗留的 ai_speaking/打断信号影响本次
        reset_interrupt_state()

        # 1. 创建并连接
        self._client = QwenRealtimeClient(
            api_key=DASHSCOPE_API_KEY,
            workspace_id=DASHSCOPE_WORKSPACE_ID,
            model=QWEN_OMNI_MODEL,
            voice=self._voice,
            instructions=self._system_prompt,
            tools=self._tools,
            region=QWEN_OMNI_REGION,
            sample_rate=self._sample_rate,
            output_sample_rate=self._output_sample_rate,
        )
        try:
            await self._client.connect()
        except Exception:
            logger.error("[RealtimeSession] connect failed", exc_info=True)
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            return False
        logger.info("[RealtimeSession] session started, voice=%s", self._voice)
        self._session_start_time = time.time()

        # 创建 Langfuse Trace
        try:
            self._trace_id = self._langfuse.create_trace_id()
            self._trace_root = self._langfuse.start_observation(
                trace_context={"trace_id": self._trace_id},
                name="realtime_session",
                metadata={
                    "model": QWEN_OMNI_MODEL,
                    "voice": self._voice,
                    "region": QWEN_OMNI_REGION,
                },
            )
        except Exception:
            logger.warning("[langfuse] trace 创建失败", exc_info=True)
            self._trace_root = None
            self._trace_id = None

        # 2. 启动音频播放器
        player = AudioPlayer(sample_rate=24000, device_name=SPEAKER_DEVICE_NAME or None)
        player.start()
        self._client._player = player

        # 3. 启动音频+图片发送协程、消费协程
        tasks = [
            asyncio.create_task(self._feed_audio_and_video(audio_queue)),
            asyncio.create_task(self._consume(player, on_text)),
        ]

        # 4. 发送问候
        if greeting_text:
            await self._client.send_text(greeting_text)

        # 5. 问候发出后再启动事件监听
        if event_buffer:
            tasks.append(asyncio.create_task(self._watch_events(event_buffer, player)))

        # 6. 等待停止信号
        self._last_activity = time.time()
        try:
            while not self._stop_event.is_set():
                # 服务端断连（如 ModelServingError 1007）→ 立即结束，避免死连接空转到 idle timeout
                if self._client is None or self._client.is_closed:
                    logger.warning("[RealtimeSession] connection lost, ending session")
                    break
                if player.is_playing() or self._user_speaking or self._agent_speaking or self._tool_executing:
                    self._last_activity = time.time()
                else:
                    idle = time.time() - self._last_activity
                    if idle >= timeout:
                        logger.info("[RealtimeSession] idle timeout (%.0fs), closing", idle)
                        break
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

        # 7. 清理
        self._stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        player.stop()
        # 关键：会话结束必须清掉 ai_speaking 信号，否则下一轮休眠期 is_ai_speaking() 恒为 True，
        # KWS 会把唤醒词当成"AI 讲话中"而忽略，导致无法再次唤醒
        reset_interrupt_state()

        if self._client:
            self._client._player = None
            await self._client.close()
            self._client = None

        # 结束未关闭的 Generation
        if self._current_generation is not None:
            try:
                self._current_generation.update(
                    output=self._partial_text,
                    status_message="session_ended",
                    usage_details={"completion_tokens": self._token_index},
                )
                self._current_generation.end()
            except Exception:
                pass
            self._current_generation = None

        # 更新 Trace 元数据并 flush
        if self._trace_root:
            try:
                self._trace_root.update(
                    metadata={
                        "turn_count": self._turn_index,
                        "generation_count": self._generation_index,
                        "tool_call_count": self._tool_call_count,
                        "session_duration_ms": round((time.time() - self._session_start_time) * 1000, 1),
                        "sleep_requested": self._sleep_requested,
                    }
                )
                self._trace_root.end()
                self._langfuse.flush()
            except Exception:
                pass

        logger.info("[RealtimeSession] session ended, sleep_requested=%s", self._sleep_requested)
        return self._sleep_requested

    @property
    def partial_text(self) -> str:
        return self._partial_text

    # ── background tasks ────────────────────────────────────────────

    async def _feed_audio_and_video(self, audio_queue: queue.Queue):
        """对齐官方 demo：发音频 → 取最新帧 → 发图片。"""
        loop = asyncio.get_running_loop()

        while not self._stop_event.is_set():
            # 本地命令执行中（唱歌等）或 keyword 模式 AI 讲话时：关流，让 KWS 独占队列听打断词
            if is_tool_audio_active() or (INTERRUPT_MODE == "keyword" and is_ai_speaking()):
                await asyncio.sleep(0.02)
                continue
            try:
                pcm = await loop.run_in_executor(
                    None, lambda: audio_queue.get(timeout=0.1)
                )
                if pcm is not None and self._client and self._client.is_streaming:
                    if hasattr(pcm, "dtype"):
                        pcm_bytes = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                    else:
                        pcm_bytes = pcm if isinstance(pcm, bytes) else bytes(pcm)
                    await self._client.send_audio(pcm_bytes)

                    self._audio_sent_epoch = self._response_epoch

                    if self._camera_capture and self._audio_sent_epoch == self._response_epoch:
                        frame = self._camera_capture.get_frame(timeout=0)
                        if frame:
                            await self._client.send_image(frame)
            except queue.Empty:
                await asyncio.sleep(0.01)
            except Exception:
                logger.error("[RealtimeSession] audio feed error", exc_info=True)
                await asyncio.sleep(0.1)

    async def _stop_robot(self):
        """用户打断时立刻停止机器人当前动作并复位（fire-and-forget，不阻塞取消响应）。"""
        try:
            result = await execute_shell("stop && reset")
            logger.info("[RealtimeSession] 打断触发 stop && reset 完成: %s", result)
        except Exception as e:
            logger.warning("[RealtimeSession] stop && reset 失败: %s", e)

    async def _interrupt_playback(self, player: AudioPlayer, reason: str):
        """中断当前播放并取消响应（服务端 VAD 抢话 / 关键词打断共用）。"""
        interrupted_text = self._partial_text
        _now = time.time()
        agent_speaking_duration_ms = ((_now - self._agent_speaking_start) * 1000
                                      if self._agent_speaking_start else 0)

        # 用户打断（服务端 VAD 抢话 / 本地打断词）且 AI 正在活动（说话或执行物理动作）时，
        # 立刻停止机器人当前动作并复位。emoji 收尾(reason=emoji_no_audio) 不是用户打断，不触发。
        if reason in ("server_vad", "keyword") and (self._agent_speaking or self._tool_executing):
            asyncio.create_task(self._stop_robot())

        if self._agent_speaking and self._render_send:
            self._render_send("agent_response_interrupted", partial_text=self._partial_text)
        self._agent_speaking = False
        self._agent_speaking_start = 0.0
        self._partial_text = ""
        self._turn_interrupted = True
        self._last_activity = _now
        player.clear()

        # 通知服务端取消当前响应，避免新旧 turn 冲突
        # 仅在有活跃响应时发送：无响应时 cancel 会让服务端回 error 事件
        if self._client.response_active:
            await self._client.cancel_response()

        # 结束被中断的 Generation
        if self._current_generation is not None:
            try:
                self._current_generation.update(
                    output=interrupted_text,
                    status_message="interrupted",
                    usage_details={"completion_tokens": self._token_index},
                    metadata={
                        "turn_index": self._turn_index,
                        "generation_index": self._generation_index,
                        "interrupted": True,
                        "agent_speaking_duration_ms": round(agent_speaking_duration_ms, 1),
                    },
                )
                self._current_generation.end()
            except Exception:
                pass
            self._current_generation = None

        self._last_transcription = ""
        logger.info("[RealtimeSession] interrupted playback (reason=%s)", reason)

    async def _consume(self, player: AudioPlayer, on_text):
        """消费模型输出：文本增量、工具调用、VAD 事件。"""
        while not self._stop_event.is_set():
            if self._client.speech_started:
                _now = time.time()
                self._user_speaking = True
                self._speech_started_time = _now
                self._last_activity = _now
                self._client.speech_started = False
                # keyword 模式：讲话期间音频被关流，speech_started 通常是新 turn 开始（AI 未讲话），
                # 仅在 AI 确实在讲话（边界音频泄漏）时才当打断；vad 模式维持原语义。
                if INTERRUPT_MODE != "keyword" or self._agent_speaking:
                    logger.info("[RealtimeSession] server VAD: speech_started, interrupting playback")
                    await self._interrupt_playback(player, "server_vad")

            if self._client.speech_stopped:
                _now = time.time()
                self._user_speech_duration_ms = ((_now - self._speech_started_time) * 1000
                                                 if self._speech_started_time else 0)
                logger.info("[RealtimeSession] server VAD: speech_stopped (user_speech=%.0fms)",
                            self._user_speech_duration_ms)
                self._user_speaking = False
                self._last_activity = _now
                self._client.speech_stopped = False
                self._turn_start_time = _now
                self._first_token_time = 0.0
                self._agent_speaking_start = 0.0
                self._turn_index += 1

            # keyword 模式：打断词命中 → 强制打断（清播放、取消响应、重开音频流）
            if INTERRUPT_MODE == "keyword" and consume_interrupt():
                logger.info("[RealtimeSession] 打断词触发，强制打断")
                await self._interrupt_playback(player, "keyword")

            # 文本
            try:
                text = self._client.text_queue.get_nowait()
                if not self._agent_speaking:
                    self._token_index = 0
                    self._partial_text = ""
                    self._turn_interrupted = False
                    if self._render_send:
                        self._render_send("agent_response_start")

                    # 首个 token → 创建 Generation
                    _now = time.time()
                    self._first_token_time = _now
                    self._agent_speaking_start = _now
                    self._generation_index += 1
                    self._e2e_latency_ms = ((_now - self._speech_started_time) * 1000
                                            if self._speech_started_time else 0)
                    try:
                        if self._trace_root:
                            parent_id = self._trace_root.id
                            self._current_generation = self._langfuse.start_observation(
                                trace_context={"trace_id": self._trace_id, "parent_span_id": parent_id},
                                name=f"turn_{self._turn_index}_gen_{self._generation_index}",
                                as_type="generation",
                                model=QWEN_OMNI_MODEL,
                                input=self._last_transcription or "(system greeting)",
                                usage_details={"completion_tokens": 0},
                                metadata={
                                    "turn_index": self._turn_index,
                                    "generation_index": self._generation_index,
                                },
                            )
                    except Exception:
                        logger.warning("[langfuse] generation 创建失败", exc_info=True)
                        self._current_generation = None
                self._agent_speaking = True
                self._last_activity = time.time()
                self._partial_text += text
                if on_text:
                    on_text(text)
                if self._render_send:
                    self._render_send("agent_token", index=self._token_index, text=text)
                    self._token_index += 1

                # "不说话"专用表情：模型只回 😊 时不会合成任何音频，服务端会卡死在音频合成、
                # ~10s 后 50002 崩断。一旦出现立即打断并取消，不等服务端 done。
                if _EMOJI_NO_SPEAK in self._partial_text:
                    logger.info("[RealtimeSession] 检测到 %s（不说话），立即 cancel", _EMOJI_NO_SPEAK)
                    await self._interrupt_playback(player, "emoji_no_audio")
            except asyncio.QueueEmpty:
                pass

            # 事件
            events = []
            while True:
                try:
                    events.append(self._client.event_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            has_function_calls = False
            for event in events:
                if event["type"] == "function_call":
                    result_sent = await self._handle_tool_call(event, player)
                    if result_sent:
                        has_function_calls = True
                    if self._stop_event.is_set():
                        break
                elif event["type"] == "response_created":
                    # 新响应已建立：上一 turn 的"中断丢弃"语义结束。
                    # 否则事件注入触发的响应若以工具调用开场（无文本 token），
                    # _turn_interrupted 仍为 True，工具结果会被误丢弃（P1）。
                    self._turn_interrupted = False
                    self._injection_pending = False
                elif event["type"] == "error":
                    err = event.get("error", {})
                    msg = str(err.get("message", ""))
                    logger.error("[RealtimeSession] server error: %s", err)
                    # 注入碰撞：send_text 的 create_response 被「已有活跃响应」拒绝。
                    # 取消当前响应 → 等待 → 重新触发一次
                    if "already has an active response" in msg:
                        logger.warning("[RealtimeSession] injection collision, attempting recovery: cancel+retry")
                        await self._client.cancel_response()
                        waited = 0.0
                        while self._client.response_active and waited < 3.0 and not self._stop_event.is_set():
                            await asyncio.sleep(0.05)
                            waited += 0.05
                        if not self._client.response_active and not self._stop_event.is_set():
                            await self._client.trigger_response()
                            self._injection_pending = True
                            logger.info("[RealtimeSession] recovery: re-triggered response after collision")
                elif event["type"] == "response_done":
                    self._last_activity = time.time()
                    self._response_epoch += 1
                    if self._agent_speaking and self._render_send:
                        self._render_send("agent_response_end")
                    self._agent_speaking = False

                    # 结束当前 Generation
                    if self._current_generation is not None:
                        try:
                            _now = time.time()
                            ttft_ms = ((self._first_token_time - self._turn_start_time) * 1000
                                       if self._first_token_time else 0)
                            _agent_speaking_duration_ms = ((_now - self._agent_speaking_start) * 1000
                                                           if self._agent_speaking_start else 0)
                            self._current_generation.update(
                                output=self._partial_text,
                                usage_details={"completion_tokens": self._token_index},
                                metadata={
                                    "turn_index": self._turn_index,
                                    "generation_index": self._generation_index,
                                    "ttft_ms": round(ttft_ms, 1),
                                    "total_latency_ms": round((_now - self._turn_start_time) * 1000, 1),
                                    "user_speech_duration_ms": round(self._user_speech_duration_ms, 1),
                                    "e2e_latency_ms": round(self._e2e_latency_ms, 1),
                                    "agent_speaking_duration_ms": round(_agent_speaking_duration_ms, 1),
                                    "interrupted": False,
                                },
                            )
                            self._current_generation.end()
                        except Exception:
                            pass
                        self._current_generation = None
                    self._agent_speaking_start = 0.0
                    self._speech_started_time = 0.0
                elif event["type"] == "transcription":
                    if self._render_send:
                        self._render_send("user_speech", text=event['text'])
                    self._last_transcription = event['text']

            if has_function_calls and not self._stop_event.is_set() and not self._turn_interrupted and not self._client.speech_started:
                await self._client.trigger_response()

            set_ai_speaking(self._agent_speaking or player.is_playing())

            _c = getattr(self, '_consume_ticks', 0) + 1
            self._consume_ticks = _c
            if _c % 150 == 0:
                logger.debug("[RealtimeSession] consume alive: %d ticks", _c)

            await asyncio.sleep(0.02)

    async def _watch_events(self, event_buffer, player: AudioPlayer):
        """监听手势/系统事件，中途注入到会话中。"""
        logger.info("[RealtimeSession] _watch_events started")
        while not self._stop_event.is_set():
            await asyncio.sleep(0.5)

            # P2: 用户正在说话时不注入——clear_audio_buffer 会清掉用户语音、
            # cancel 会与 VAD 自动 turn 冲突。事件留在 buffer，下一轮再试。
            if self._user_speaking or (self._client and self._client.speech_started):
                continue

            # 上一轮注入还在等待 response.created，不允许连续注入——
            # 否则服务端响应卡死（无 response.created）时 _response_active 标志位
            # 与服务端状态背离，下一轮 create_response 会撞 "Conversation already has an active response"
            if self._injection_pending:
                continue

            events = event_buffer.drain_all()
            if not events:
                continue

            parts = []
            for e in events:
                parts.append(f"[系统事件] 类型: {e.event_name}，描述: {e.description}")
            event_text = "\n".join(parts)
            logger.info("[RealtimeSession] injecting %d event(s): %s", len(events), event_text)

            # 仅在有活跃响应时才取消，避免无响应时发送 cancel 触发服务端错误
            if self._client.response_active:
                await self._client.cancel_response()
                # 等待 cancel 确认（response.done），最多 3s
                waited = 0.0
                while self._client.response_active and waited < 3.0 and not self._stop_event.is_set():
                    await asyncio.sleep(0.05)
                    waited += 0.05
                if self._client.response_active:
                    logger.warning("[RealtimeSession] cancel_response took >3s, proceeding anyway")

            player.clear()
            self._agent_speaking = False
            self._turn_interrupted = True

            # 清空服务端音频缓冲，退出 VAD 听音状态，确保 response.create 被接受
            await self._client.clear_audio_buffer()
            await self._client.send_text(event_text)
            self._injection_pending = True
            self._last_activity = time.time()

    # ── tool call handling ──────────────────────────────────────────

    async def _handle_tool_call(self, event: dict, player: AudioPlayer) -> bool:
        """Handle a single tool call. Returns True if a function_call_result was sent."""
        name = event["name"]
        call_id = event["call_id"]
        try:
            args = json.loads(event.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}

        logger.info("[RealtimeSession] tool call: %s(%s)", name, args)
        self._tool_call_count += 1

        # 创建工具 Span
        tool_span = None
        _tool_start = time.time()
        try:
            if self._trace_root:
                parent_id = self._current_generation.id if self._current_generation else self._trace_root.id
                tool_span = self._langfuse.start_observation(
                    trace_context={"trace_id": self._trace_id, "parent_span_id": parent_id},
                    name=f"tool.{name}",
                    as_type="span",
                    input=args,
                )
        except Exception:
            pass

        if name == "go_sleep":
            self._sleep_requested = True
            while player.is_playing():
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.5)
            self._stop_event.set()
            if tool_span:
                try:
                    tool_span.update(
                        output="sleep_requested",
                        metadata={"duration_ms": round((time.time() - _tool_start) * 1000, 1), "success": True},
                    )
                    tool_span.end()
                except Exception:
                    pass
            return False

        self._tool_executing = True
        self._last_activity = time.time()

        # 本地 CLI 命令（如 sing）需要在本机独占扬声器：先等小伴把当前这句播完，再释放输出流
        released = is_local_cli_shell(name, args)
        if released:
            _deadline = time.time() + 10          # 兜底，避免 is_playing 卡住时无限等待
            while player.is_playing() and time.time() < _deadline:
                await asyncio.sleep(0.05)
            player.close()
        try:
            result = await execute_tool(name, args)
        finally:
            self._tool_executing = False
            self._last_activity = time.time()
            if released:
                # 唱歌进程（尤其被 kill 后）释放 ALSA 设备有延迟：稍等 + 重试，失败也不抛（避免拖垮 _consume）
                for _ in range(4):
                    await asyncio.sleep(0.2)
                    try:
                        player.open()
                        break
                    except Exception:
                        logger.warning("[RealtimeSession] 唱歌后重新打开扬声器失败，重试中", exc_info=True)

        # 结束工具 Span
        if tool_span:
            try:
                is_success = not result.startswith("❌") and not result.startswith("工具执行失败")
                tool_span.update(
                    output=result,
                    metadata={
                        "duration_ms": round((time.time() - _tool_start) * 1000, 1),
                        "success": is_success,
                    },
                )
                tool_span.end()
            except Exception:
                pass

        # 如果工具执行期间用户已开始说话，服务端已开启新 turn，不再发送旧 turn 的结果
        if self._client.speech_started or self._turn_interrupted:
            logger.info("[RealtimeSession] tool %s: turn interrupted, dropping result", name)
            return False

        if name == "set_omni_voice" and "已切换" in result:
            voice = args.get("voice", "")
            if voice:
                await self._client.update_voice(voice)

        await self._client.send_function_call_result(call_id, result)
        return True