import asyncio
import base64
import json
import ssl
from enum import Enum, auto
from typing import Optional
from urllib.parse import urlparse

import cv2
import numpy as np
import websockets

from logging_config import get_logger

LOGGER = get_logger("minicpmo")


class State(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    STREAMING = auto()
    CLOSING = auto()
    CLOSED = auto()


class MiniCPMOClient:
    """Async WebSocket client for MiniCPM-o Realtime API video duplex mode."""

    def __init__(self, host: str, mode: str = "video", session_config: dict | None = None, insecure: bool = False):
        self._url = self._normalize_url(host, mode)
        self._session_config = session_config or {}
        self._insecure = insecure
        self._ws: Optional[websockets.ClientConnection] = None
        self._state = State.DISCONNECTED
        self._session_id: str = ""

        # Single tagged output queue: items are ("text", str), ("audio", bytes), or ("listen", True)
        self.output_queue: asyncio.Queue[tuple[str, str | bytes | bool]] = asyncio.Queue()

        self._receive_task: Optional[asyncio.Task] = None
        self._created_event = asyncio.Event()
        self._closed_event = asyncio.Event()

        # When True, send_loop skips audio + video (delegation active — audio routed to Qwen, camera freed)
        self.audio_paused: bool = False

        # Turn 状态标记：收到 text/audio 时置 True，收到 listen 时置 False
        # 用于去重服务端周期性发送的 listen 事件（只在真正的 turn 边界触发一次）
        self._turn_active: bool = False

    @staticmethod
    def _normalize_url(host: str, mode: str) -> str:
        """将 host（纯主机或完整 URL）规范化为 wss:// URL。"""
        # 如果已经是 ws/wss URL，直接返回
        if host.startswith("ws://") or host.startswith("wss://"):
            parsed = urlparse(host)
            if parsed.path and parsed.path != "/":
                return host
            return f"{host.rstrip('/')}/v1/realtime?mode={mode}"

        # 如果是 http/https URL，提取 host:port，转为 wss
        if host.startswith("http://") or host.startswith("https://"):
            parsed = urlparse(host)
            netloc = parsed.netloc
            scheme = "wss" if parsed.scheme == "https" else "ws"
            return f"{scheme}://{netloc}/v1/realtime?mode={mode}"

        # 纯 host（可能带端口）
        return f"wss://{host}/v1/realtime?mode={mode}"

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_streaming(self) -> bool:
        return self._state == State.STREAMING

    @property
    def closed_event(self) -> asyncio.Event:
        return self._closed_event

    async def connect(self) -> None:
        self._state = State.CONNECTING
        ssl_ctx = ssl.create_default_context()
        if self._insecure:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        self._ws = await websockets.connect(
            self._url,
            ssl=ssl_ctx,
            max_size=100 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        LOGGER.info("WebSocket connected to %s", self._url)

        self._receive_task = asyncio.create_task(self._receive_loop())

        try:
            await asyncio.wait_for(self._created_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError("Timed out waiting for session.created")

        self._state = State.STREAMING
        LOGGER.info("Session ready: %s", self._session_id)

    async def send_input_append(self, audio_b64: str, video_frames_b64: list[str]) -> None:
        if self._state != State.STREAMING:
            return
        message = json.dumps({
            "type": "input.append",
            "input": {
                "audio": audio_b64,
                "video_frames": video_frames_b64,
                "force_listen": False,
                "max_slice_nums": 1,
            },
        })
        await self._ws.send(message)

    async def reset(self) -> None:
        """Reset internal state so connect() can be called again."""
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
        self._state = State.DISCONNECTED
        self._session_id = ""
        self._ws = None
        self._receive_task = None
        self._created_event = asyncio.Event()
        self._closed_event = asyncio.Event()
        self.output_queue = asyncio.Queue()
        self.audio_paused = False
        self._turn_active = False

    async def close(self) -> None:
        if self._state in (State.CLOSED, State.CLOSING):
            return
        self._state = State.CLOSING
        try:
            if self._ws:
                await self._ws.send(json.dumps({"type": "session.close", "reason": "user_stop"}))
                await asyncio.wait_for(self._closed_event.wait(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass
        if self._ws:
            await self._ws.close()
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
        self._state = State.CLOSED

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(event)
        except websockets.ConnectionClosed as e:
            LOGGER.warning("WebSocket closed: %s", e)
        finally:
            if self._state not in (State.CLOSED, State.DISCONNECTED):
                self._state = State.CLOSED
            self._closed_event.set()

    async def _dispatch(self, event: dict) -> None:
        etype = event.get("type", "")
        LOGGER.info("<- %s", etype)

        if etype == "session.queued":
            pos = event.get("position", "?")
            LOGGER.info("Queued (position=%s)", pos)
            print(f"[server] session.queued (position={pos})")

        elif etype == "session.queue_update":
            pos = event.get("position", "?")
            LOGGER.info("Queue update (position=%s)", pos)
            print(f"[server] session.queue_update (position={pos})")

        elif etype in ("session.queue_done", "queue_done"):
            LOGGER.info("Queue done, sending session.init")
            print("[server] session.queue_done -> sending session.init")
            await self._send_session_init()

        elif etype == "session.created":
            self._session_id = event.get("session_id", "")
            LOGGER.info("Session created: %s", self._session_id)
            print(f"[server] session.created (session_id={self._session_id})")
            self._created_event.set()

        elif etype == "session.closed":
            LOGGER.info("Session closed: %s", event.get("reason", ""))
            print(f"[server] session.closed (reason={event.get('reason', '')})")
            self._closed_event.set()

        elif etype == "response.output.delta":
            await self._handle_output_delta(event)

        elif etype == "error":
            LOGGER.error("Server error: %s", event.get("error", {}))
            print(f"[server] error: {event.get('error', {})}")

    async def _send_session_init(self) -> None:
        payload = {}
        if "system_prompt" in self._session_config:
            payload["system_prompt"] = self._session_config["system_prompt"]
        if "config" in self._session_config:
            payload["config"] = self._session_config["config"]
        if "voice" in self._session_config:
            payload["voice"] = self._session_config["voice"]

        message = json.dumps({"type": "session.init", "payload": payload})
        await self._ws.send(message)

    async def _handle_output_delta(self, event: dict) -> None:
        kind = event.get("kind", "")
        LOGGER.info("output.delta kind=%s", kind)

        if kind == "listen":
            # 只在真正的 turn 边界触发（之前收到过 text/audio），过滤周期性 listen 刷屏
            if self._turn_active:
                self._turn_active = False
                print("[server] response.output.delta (kind=listen) → turn end")
                await self.output_queue.put(("listen", True))

        elif kind == "text":
            text = event.get("text", "")
            if text:
                self._turn_active = True
                LOGGER.info("text delta: %s", text[:80])
                print(f"[server] response.output.delta (kind=text): {text}")
                await self.output_queue.put(("text", text))

        elif kind == "audio":
            audio_b64 = event.get("audio", "")
            if audio_b64:
                self._turn_active = True
                audio_bytes = base64.b64decode(audio_b64)
                LOGGER.info("audio delta: %d bytes", len(audio_bytes))
                print(f"[server] response.output.delta (kind=audio, {len(audio_bytes)} bytes)")
                await self.output_queue.put(("audio", audio_bytes))

    @staticmethod
    def encode_audio_pcm(audio: np.ndarray) -> str:
        if audio is None or len(audio) == 0:
            return ""
        return base64.b64encode(audio.astype("<f4", copy=False).tobytes()).decode()

    @staticmethod
    def encode_video_frame(frame_bgr: np.ndarray, quality: int = 75) -> str:
        _, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return base64.b64encode(buf).decode()