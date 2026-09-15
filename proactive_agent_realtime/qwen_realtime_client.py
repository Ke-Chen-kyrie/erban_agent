#!/usr/bin/env python3
"""
Qwen-Omni-Realtime 客户端 — 官方 DashScope SDK 实现。

协议: WebSocket (DashScope OmniRealtimeConversation)
模型: qwen3.5-omni-plus-realtime / qwen3.5-omni-flash-realtime
功能: 实时音频+图片输入, 文本+音频输出, Function Calling, 内置 AEC+VAD+ASR+TTS
"""

import asyncio
import base64
import queue
import threading
import time
import traceback
import uuid
from enum import Enum, auto
from typing import Optional

from dashscope.audio.qwen_omni import (
    AudioFormat,
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)

from logging_config import get_logger

logger = get_logger("qwen_realtime")


class State(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    STREAMING = auto()
    CLOSING = auto()
    CLOSED = auto()


class QwenRealtimeClient:
    """Async client for Qwen-Omni-Realtime API (DashScope SDK)."""

    def __init__(
        self,
        api_key: str,
        workspace_id: str,
        model: str = "qwen3.5-omni-plus-realtime",
        voice: str = "Tina",
        instructions: str = "",
        tools: list[dict] | None = None,
        region: str = "cn-beijing",
        sample_rate: int = 16000,
        output_sample_rate: int = 24000,
    ):
        region_hosts = {
            "cn-beijing": f"{workspace_id}.cn-beijing.maas.aliyuncs.com",
            "ap-southeast-1": f"{workspace_id}.ap-southeast-1.maas.aliyuncs.com",
        }
        host = region_hosts.get(region, region_hosts["cn-beijing"])
        self._ws_url = f"wss://{host}/api-ws/v1/realtime"
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._instructions = instructions
        self._tools = tools or []
        self._sample_rate = sample_rate
        self._output_sample_rate = output_sample_rate

        self._conversation: Optional[OmniRealtimeConversation] = None
        self._state = State.DISCONNECTED
        self._session_id: str = ""

        # Callback-based audio player (set externally after connect)
        self._player: object = None

        # Output queues (put_nowait is thread-safe on asyncio.Queue)
        self.text_queue: asyncio.Queue[str] = asyncio.Queue()
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self.event_queue: asyncio.Queue[dict] = asyncio.Queue()

        self._session_ready = threading.Event()
        self._session_updated = threading.Event()  # session.update 被服务端确认
        self._closed = threading.Event()
        self._last_response_done_time: float = 0.0  # 上次 response.done 的时间戳

        # VAD state (set from WebSocket thread, read from asyncio — GIL-safe)
        self.speech_started: bool = False
        self.speech_stopped: bool = False

        # Response tracking (set from WebSocket thread, read from asyncio — GIL-safe)
        self.response_active: bool = False
        # 本轮是否收到过音频 delta：纯文本回复（如"仅回复 😊"）没有音频可收尾，
        # 需据此决定用 response.text.done 还是 response.done 来收敛 turn
        self._response_has_audio: bool = False

        # Transcription buffer
        self._transcription_buffer: str = ""
        self._last_transcript: str = ""

    # ── connection lifecycle ──────────────────────────────────────

    def _session_config(self, voice: str) -> dict:
        """完整会话配置。session.update 是全量覆盖，任何字段缺省都会被打回 SDK 默认值
        （如 semantic_vad → server_vad、instructions/tools 丢失），必须整体传递。"""
        return dict(
            output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
            voice=voice,
            input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
            output_audio_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            instructions=self._instructions,
            enable_input_audio_transcription=True,
            enable_turn_detection=True,
            turn_detection_type="semantic_vad",
            tools=self._tools,
        )

    async def connect(self) -> None:
        self._state = State.CONNECTING

        callback = _OmniCallback(self)
        self._conversation = OmniRealtimeConversation(
            api_key=self._api_key,
            url=self._ws_url,
            model=self._model,
            callback=callback,
        )

        # connect() blocks until WebSocket is established (up to 5s), then
        # spawns a daemon thread for the receive loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._conversation.connect)

        # Wait for session.created (arrives asynchronously via WebSocket thread)
        ready_ok = await loop.run_in_executor(
            None, lambda: self._session_ready.wait(timeout=10)
        )
        if not ready_ok:
            self._state = State.CLOSED
            raise TimeoutError("session.created not received within 10s")

        # Configure session — follows official DashScope example protocol
        self._conversation.update_session(**self._session_config(self._voice))
        logger.info(
            "session.update sent (voice=%s, tools=%d, vad=semantic_vad, "
            "audio_in=%dHz, audio_out=%dHz)",
            self._voice, len(self._tools), self._sample_rate, self._output_sample_rate,
        )

        # Wait for session.updated confirmation (up to 5s)
        updated_ok = await loop.run_in_executor(
            None, lambda: self._session_updated.wait(timeout=5)
        )
        if not updated_ok:
            # P5: 配置未被服务端确认（可能仍在用默认配置：无 tools、server_vad），
            # 宁可连接失败、等下次唤醒重试，也不带着错误配置跑
            self._state = State.CLOSED
            raise TimeoutError("session.updated not received within 5s")

        self._state = State.STREAMING
        logger.info("Qwen Realtime session ready: %s", self._session_id)

    async def close(self) -> None:
        if self._state in (State.CLOSED, State.CLOSING):
            return
        self._state = State.CLOSING

        if self._conversation:
            try:
                self._conversation.close()
            except Exception:
                pass
            self._conversation = None

        # Wait for on_close callback (with timeout)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._closed.wait(timeout=3))
        except Exception:
            pass

        # Clear queues
        for q in (self.text_queue, self.event_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        self._session_id = ""
        self.response_active = False
        self._state = State.CLOSED

    # ── input methods ─────────────────────────────────────────────

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send audio chunk (PCM int16, mono, sample_rate Hz)."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
        self._conversation.append_audio(audio_b64)

    async def send_image(self, image_b64: str) -> None:
        """Send image frame via input_image_buffer.append."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        self._conversation.append_video(image_b64)

    async def cancel_response(self) -> None:
        """Cancel current model response."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        self._conversation.cancel_response()

    async def clear_audio_buffer(self) -> None:
        """Clear the server-side audio buffer (exit VAD listening state)."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        self._conversation.clear_appended_audio()

    async def update_voice(self, voice: str) -> None:
        """Switch output voice mid-session."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        self._voice = voice
        # 必须传完整配置：session.update 是全量覆盖，只传 voice 会把
        # semantic_vad 打回 server_vad 默认值，并丢失 instructions/tools
        self._conversation.update_session(**self._session_config(voice))
        logger.info("session.update voice -> %s", voice)

    async def send_text(self, text: str) -> None:
        """Send text message (for non-audio conversation)."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        self._conversation.create_item({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        })
        # 必须显式传 instructions：实测服务端在「完整提示词 + tools」组合下，
        # response.create 不带 instructions 会触发 <50002> ModelServingError 并 1007 断连
        self._conversation.create_response(
            instructions=self._instructions,
            output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
        )

    async def send_function_call_result(self, call_id: str, output: str) -> None:
        """Send function call result back to the model (item only, no response trigger)."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        self._conversation.create_item({
            "id": "item_" + uuid.uuid4().hex,
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        })

    async def trigger_response(self) -> None:
        """Trigger model response after all function call results are sent."""
        if self._state != State.STREAMING or self._conversation is None:
            return
        # 同 send_text：显式传 instructions，规避服务端 ModelServingError
        self._conversation.create_response(
            instructions=self._instructions,
            output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
        )

    # ── helpers ───────────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        return self._state == State.STREAMING

    @property
    def is_closed(self) -> bool:
        """连接已关闭（含服务端主动断连），会话不可能再恢复。"""
        return self._state in (State.CLOSING, State.CLOSED)

    @property
    def last_response_done_time(self) -> float:
        return self._last_response_done_time

    # ── event dispatch (called from WebSocket thread) ──────────────

    def _dispatch(self, event: dict):
        etype = event.get("type", "")
        # 诊断用：DEBUG 下打印每一个收到的服务端事件类型，便于定位"纯 emoji 回复
        # 到底收到哪些事件、有没有 done"。正常 INFO 级别不输出。
        logger.debug("event: %s", etype)

        if etype == "session.created":
            self._session_id = event.get("session", {}).get("id", "")
            logger.info("session.created: %s", self._session_id)
            self._session_ready.set()

        elif etype == "session.updated":
            logger.info("session.updated: config accepted by server")
            self._session_updated.set()

        elif etype == "input_audio_buffer.speech_started":
            self.speech_started = True
            self.speech_stopped = False
            logger.info("VAD: speech started")

        elif etype == "input_audio_buffer.speech_stopped":
            self.speech_stopped = True
            self.speech_started = False
            logger.info("VAD: speech stopped")

        elif etype == "input_audio_buffer.committed":
            logger.debug("VAD: audio buffer committed")

        elif etype == "conversation.item.input_audio_transcription.delta":
            text = event.get("text", "") + event.get("stash", "")
            self._transcription_buffer += text

        elif etype == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            self._last_transcript = transcript
            self._transcription_buffer = ""
            logger.info("ASR completed: %s", transcript)
            try:
                self.event_queue.put_nowait({"type": "transcription", "text": transcript})
            except asyncio.QueueFull:
                pass

        elif etype == "conversation.item.input_audio_transcription.failed":
            logger.error("ASR failed: %s", event)

        elif etype == "response.audio_transcript.delta":
            text = event.get("delta", "")
            if text:
                try:
                    self.text_queue.put_nowait(text)
                except asyncio.QueueFull:
                    pass

        elif etype == "response.audio_transcript.done":
            pass

        elif etype == "response.created":
            self.response_active = True
            self._response_has_audio = False  # 新一轮响应，重置音频标志
            logger.debug("response.created")
            try:
                self.event_queue.put_nowait({"type": "response_created"})
            except asyncio.QueueFull:
                pass

        elif etype == "response.text.delta":
            text = event.get("delta", "")
            if text:
                try:
                    self.text_queue.put_nowait(text)
                except asyncio.QueueFull:
                    pass

        elif etype == "response.text.done":
            # 纯文本回复（服务端只发 response.text.delta + response.text.done，不发
            # response.done，例如系统提示要求的"仅回复 😊"）。本轮没收到任何音频时，
            # 用文本的结束标志收敛 turn，否则 response_active / _agent_speaking 永不复位
            # （keyword 模式下会因此停止喂音频，最终触发服务端 ModelServingError 崩断）。
            if not self._response_has_audio and self.response_active:
                self.response_active = False
                logger.info("response.text.done: text-only turn concluded (no audio)")
                try:
                    self.event_queue.put_nowait({"type": "response_done"})
                except asyncio.QueueFull:
                    pass

        elif etype == "response.audio.delta":
            audio_b64 = event.get("delta", "")
            if audio_b64:
                self._response_has_audio = True
                pcm_bytes = base64.b64decode(audio_b64)
                if self._player is not None:
                    self._player.enqueue(pcm_bytes)
                else:
                    try:
                        self.audio_queue.put_nowait(pcm_bytes)
                    except queue.Full:
                        pass

        elif etype == "response.function_call_arguments.done":
            call_id = event.get("call_id", "")
            name = event.get("name", "")
            arguments = event.get("arguments", "{}")
            try:
                self.event_queue.put_nowait({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                })
            except asyncio.QueueFull:
                pass

        elif etype == "response.done":
            # 若已由 response.text.done 收敛过（text-only turn），跳过，避免重复收尾
            was_active = self.response_active
            self.response_active = False
            logger.info("response.done")
            self._last_response_done_time = time.time()
            if was_active:
                try:
                    self.event_queue.put_nowait({"type": "response_done"})
                except asyncio.QueueFull:
                    pass

        elif etype == "error":
            error_info = event.get("error", {})
            logger.error("Server error: %s", error_info)
            try:
                self.event_queue.put_nowait({"type": "error", "error": error_info})
            except asyncio.QueueFull:
                pass

        else:
            pass


class _OmniCallback(OmniRealtimeCallback):
    """Internal callback bridging SDK sync events to QwenRealtimeClient."""

    def __init__(self, client: QwenRealtimeClient):
        super().__init__()
        self._client = client

    def on_open(self) -> None:
        logger.info("Omni WebSocket connected")

    def on_close(self, close_status_code: int, close_msg: str) -> None:
        logger.info("Omni WebSocket closed: code=%s, msg=%s", close_status_code, close_msg)
        if self._client._state not in (State.CLOSED, State.CLOSING):
            self._client._state = State.CLOSED
        self._client._closed.set()

    def on_event(self, event: dict) -> None:
        try:
            self._client._dispatch(event)
        except Exception:
            logger.error("Error dispatching event: %s", traceback.format_exc())