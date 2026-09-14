"""
大屏渲染 WebSocket 客户端。

与渲染服务器建立单连接，发送 user_speech、agent_token、system_event 等消息。
所有公开方法均为 fire-and-forget，不阻塞主流程，异常时只打日志。
"""

import asyncio
import json
import time
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from logging_config import get_logger

logger = get_logger(__name__)

_RECONNECT_MAX = 3
_RECONNECT_DELAY = 2.0
_PING_INTERVAL = 30
_QUEUE_MAXSIZE = 256


class RenderSocket:
    """大屏渲染 WebSocket 客户端，一次 wakeup cycle 一个连接。"""

    def __init__(self, url: str):
        self._url = url
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._send_queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._alive = False
        self._reconnect_count = 0

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def connect(self) -> None:
        """建立连接，5s 超时。失败不抛异常。"""
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self._url, ping_interval=None, close_timeout=2),
                timeout=5,
            )
            self._alive = True
            self._reconnect_count = 0
            self._send_task = asyncio.create_task(self._send_loop())
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info(f"[render] 已连接: {self._url}")
        except Exception as e:
            logger.warning(f"[render] 连接失败: {e}，本次会话不启用大屏渲染")
            self._alive = False

    async def close(self) -> None:
        """关闭连接，释放资源。"""
        self._alive = False
        for task in (self._send_task, self._recv_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info("[render] 连接已释放")

    async def send_user_speech_start(self) -> None:
        await self._enqueue("user_speech_start")

    async def send_user_speech(self, text: str) -> None:
        await self._enqueue("user_speech", {"text": text})

    async def send_agent_response_start(self) -> None:
        await self._enqueue("agent_response_start")

    async def send_agent_token(self, index: int, text: str) -> None:
        await self._enqueue("agent_token", {"index": index, "text": text})

    async def send_agent_response_end(self) -> None:
        await self._enqueue("agent_response_end")

    async def send_agent_response_interrupted(self, partial_text: str = "") -> None:
        await self._enqueue("agent_response_interrupted", {"partial_text": partial_text})

    async def send_system_event(self, event_name: str, description: str) -> None:
        await self._enqueue("system_event", {"event_name": event_name, "description": description})

    async def _enqueue(self, msg_type: str, extra: dict | None = None) -> None:
        """入队即走，队列满则丢弃。"""
        if not self._alive:
            return
        payload = {"type": msg_type, "timestamp": time.time()}
        if extra:
            payload.update(extra)
        try:
            self._send_queue.put_nowait(json.dumps(payload, ensure_ascii=False))
        except asyncio.QueueFull:
            logger.debug(f"[render] 发送队列满，丢弃: {msg_type}")

    async def _send_loop(self) -> None:
        """后台发送协程：取队列消息 → 发送。"""
        last_ping = time.time()
        while self._alive:
            try:
                msg = await asyncio.wait_for(self._send_queue.get(), timeout=_PING_INTERVAL / 2)
                if self._ws:
                    await self._ws.send(msg)
            except asyncio.TimeoutError:
                # 发心跳
                if time.time() - last_ping >= _PING_INTERVAL and self._ws:
                    try:
                        await self._ws.send(json.dumps({"type": "ping"}))
                        last_ping = time.time()
                    except Exception:
                        self._alive = False
            except (ConnectionClosed, Exception):
                self._alive = False

    async def _recv_loop(self) -> None:
        """后台接收协程：处理 pong 等。"""
        while self._alive:
            try:
                if self._ws:
                    await asyncio.wait_for(self._ws.recv(), timeout=_PING_INTERVAL + 10)
            except asyncio.TimeoutError:
                pass
            except (ConnectionClosed, Exception):
                self._alive = False
                await self._try_reconnect()

    async def _try_reconnect(self) -> None:
        """后台尝试重连，最多 3 次。"""
        while self._reconnect_count < _RECONNECT_MAX and not self._alive:
            self._reconnect_count += 1
            logger.warning(f"[render] 断连，尝试重连 ({self._reconnect_count}/{_RECONNECT_MAX})")
            await asyncio.sleep(_RECONNECT_DELAY)
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(self._url, ping_interval=None, close_timeout=2),
                    timeout=5,
                )
                self._alive = True
                self._reconnect_count = 0
                logger.info("[render] 重连成功")
                return
            except Exception:
                pass
        if not self._alive:
            logger.warning("[render] 重连失败，放弃")