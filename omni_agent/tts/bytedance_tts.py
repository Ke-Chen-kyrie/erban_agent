"""
ByteDance Seed-TTS 2.0 双向流式 WebSocket 客户端。

基于 wss://openspeech.bytedance.com/api/v3/tts/bidirection 二进制协议，
支持文本流式输入、PCM 音频流式输出。

协议使用二进制帧（与 ByteDance ASR 相同的 header+payload 格式），
不是 JSON text messages。
"""

import asyncio
import json
import uuid
from typing import Optional

import websockets

from tts import protocols as proto
from logging_config import get_logger

logger = get_logger(__name__)


class ByteDanceTTS:
    """ByteDance TTS 双向流式 WebSocket 客户端。

    用法::

        tts = ByteDanceTTS(api_key="...", speaker="...")
        await tts.connect()
        await tts.start_session()
        await tts.send_text("你好")
        await tts.send_text("世界")
        await tts.finish()
        # 同时从 tts.audio_queue 读取 PCM 音频

        pcm = await tts.audio_queue.get()
        if pcm is None:
            # 合成结束
            pass

        await tts.close()
    """

    def __init__(
        self,
        api_key: str,
        resource_id: str = "seed-tts-2.0",
        speaker: str = "zh_female_gaolengyujie_uranus_bigtts",
        sample_rate: int = 24000,
        speech_rate: int = 0,
        url: str = "wss://openspeech.bytedance.com/api/v3/tts/bidirection",
    ):
        self._api_key = api_key
        self._resource_id = resource_id
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._speech_rate = speech_rate
        self._url = url

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._session_id: Optional[str] = None
        self._connect_id: str = str(uuid.uuid4())

        self._text_queue: asyncio.Queue = asyncio.Queue()
        self.audio_queue: asyncio.Queue = asyncio.Queue()

        self._send_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._session_finished = False
        self._sentinel_sent = False

    # ── 连接 ──

    async def connect(self):
        """建立 WebSocket 连接并完成握手 (StartConnection → ConnectionStarted)。"""
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Connect-Id": self._connect_id,
        }
        logger.debug(f"[TTS] 连接 {self._url}")
        self._ws = await websockets.connect(
            self._url, additional_headers=headers, max_size=10 * 1024 * 1024,
            ping_interval=None,  # 禁用 WebSocket 层 ping，依赖应用层 keepalive（每 10s 空文本）
        )
        logid = ""
        if self._ws.response and self._ws.response.headers:
            logid = self._ws.response.headers.get("x-tt-logid", "")
        logger.debug(f"[TTS] WebSocket 已连接 (logid={logid})")

        # StartConnection — 二进制帧
        await proto.start_connection(self._ws)
        await proto.wait_for_event(
            self._ws, proto.MsgType.FullServerResponse, proto.EventType.ConnectionStarted
        )
        logger.debug("[TTS] 连接握手完成")

    # ── 会话 ──

    async def start_session(self):
        """创建合成会话 (StartSession → SessionStarted)，并启动后台收发任务。"""
        self._session_id = str(uuid.uuid4())
        session_payload = json.dumps({
            "req_params": {
                "speaker": self._speaker,
                "audio_params": {
                    "format": "pcm",
                    "sample_rate": self._sample_rate,
                    "speech_rate": self._speech_rate,
                },
            },
        }).encode("utf-8")

        await proto.start_session(self._ws, session_payload, self._session_id)
        await proto.wait_for_event(
            self._ws, proto.MsgType.FullServerResponse, proto.EventType.SessionStarted
        )
        logger.debug(f"[TTS] 会话已创建 (session_id={self._session_id})")

        # 启动后台任务
        self._session_finished = False
        self._sentinel_sent = False
        self._send_task = asyncio.create_task(self._send_loop())
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def send_text(self, text: str):
        """将文本片段送入 TTS 合成（非阻塞，放入内部队列）。"""
        if self._session_finished:
            return
        await self._text_queue.put(text)

    async def finish(self):
        """结束合成会话 (FinishSession → SessionFinished)。

        即使 _session_finished 已置位（如 send_loop 异常），也要确保 audio_queue
        有 None 哨兵唤醒 consumer，防止 main.py 侧 30s 超时。
        """
        if self._session_finished:
            # 会话已异常结束，不再发 FinishSession，但要立即唤醒 consumer
            self._put_sentinel()
            # 关闭 WebSocket 以唤醒 receive_loop（否则它卡在 120s 超时上）
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            return

        # 发送排空哨兵，通知 _send_loop 退出
        await self._text_queue.put(None)

        # 等待 _send_loop 处理完
        if self._send_task and not self._send_task.done():
            try:
                await asyncio.wait_for(self._send_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # 发送 FinishSession
        try:
            await proto.finish_session(self._ws, self._session_id)
        except Exception as e:
            logger.warning(f"[TTS] 发送 FinishSession 失败: {e}")

    async def close(self):
        """关闭连接，确保所有后台任务已停止、audio_queue 已放入结束哨兵。"""
        # 取消发送任务（防止阻塞在 _text_queue.get()）
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass

        # 先关闭 WebSocket 唤醒 _receive_loop（否则 recv() 卡在 120s 超时上）
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        # 等待接收任务完成（WebSocket 已关闭，recv() 会立即抛出 ConnectionClosed）
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        # 确保 audio_queue 有结束哨兵
        self._put_sentinel()

        logger.debug("[TTS] 连接已关闭")

    # ── 内部 ──

    async def _send_loop(self):
        """后台任务：流式发送 ChatTTSText，空闲时发 keepalive 防止会话超时。"""
        _keepalive_interval = 10  # 秒

        try:
            while True:
                try:
                    text = await asyncio.wait_for(
                        self._text_queue.get(), timeout=_keepalive_interval
                    )
                except asyncio.TimeoutError:
                    await self._send_keepalive()
                    continue

                if text is None:
                    self._text_queue.task_done()
                    break

                # speaker/audio_params 已在 start_session 中设置，流式追加只需发增量 text
                payload = json.dumps({
                    "req_params": {
                        "text": text,
                    },
                }).encode("utf-8")
                try:
                    await proto.chat_tts_text(self._ws, payload, self._session_id)
                except Exception as e:
                    logger.warning(f"[TTS] 发送文本失败: {e}")
                    self._text_queue.task_done()
                    self._session_finished = True
                    # 发送失败说明 WebSocket 大概率已断开，主动 close 唤醒 receive_loop
                    if self._ws:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
                    break
                self._text_queue.task_done()
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            pass
        except Exception:
            self._session_finished = True

    async def _send_keepalive(self):
        """发送空文本 ChatTTSText 保持会话活跃。"""
        try:
            payload = json.dumps({
                "req_params": {
                    "text": "",
                },
            }).encode("utf-8")
            await proto.chat_tts_text(self._ws, payload, self._session_id)
            logger.debug("[TTS] keepalive 已发送")
        except Exception as e:
            logger.warning(f"[TTS] keepalive 发送失败: {e}")
            self._session_finished = True
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass

    async def _receive_loop(self):
        """后台任务：接收服务端二进制消息，分发事件和音频数据。"""
        _audio_chunks = 0
        _audio_bytes = 0
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        proto.receive_message(self._ws), timeout=120
                    )
                except asyncio.TimeoutError:
                    logger.warning("[TTS] 接收超时 (120s)，退出")
                    break
                except websockets.ConnectionClosed:
                    logger.debug("[TTS] WebSocket 连接已关闭")
                    break
                except ValueError as e:
                    # receive_message raises ValueError on unexpected text messages
                    logger.warning(f"[TTS] 收到异常消息: {e}")
                    continue

                if msg.type == proto.MsgType.AudioOnlyServer:
                    # raw PCM 音频数据
                    if msg.payload:
                        _audio_chunks += 1
                        _audio_bytes += len(msg.payload)
                        if _audio_chunks <= 3 or _audio_chunks % 20 == 0:
                            logger.debug(
                                f"[TTS] 收到音频 #{_audio_chunks}: "
                                f"{len(msg.payload)} 字节 (累计 {_audio_bytes} 字节)"
                            )
                        await self.audio_queue.put(msg.payload)

                elif msg.type == proto.MsgType.FullServerResponse:
                    self._handle_event(msg.event, msg)
                    if msg.event in (
                        proto.EventType.SessionFinished,
                        proto.EventType.SessionFailed,
                    ):
                        self._session_finished = True
                        break

                elif msg.type == proto.MsgType.Error:
                    error_text = msg.payload.decode("utf-8", "ignore") if msg.payload else ""
                    logger.error(f"[TTS] 服务端错误 (code={msg.error_code}): {error_text}")
                    self._session_finished = True
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[TTS] 接收循环异常退出: {e}")
        finally:
            logger.debug(
                f"[TTS] 接收结束: 共 {_audio_chunks} 个音频块, {_audio_bytes} 字节 "
                f"({(_audio_bytes / (self._sample_rate * 2) * 1000):.0f}ms @{self._sample_rate}Hz)"
            )
            self._put_sentinel()

    def _put_sentinel(self):
        """放入结束哨兵（幂等，确保只放一次）。"""
        if not self._sentinel_sent:
            self._sentinel_sent = True
            try:
                self.audio_queue.put_nowait(None)
            except Exception:
                pass

    def _handle_event(self, event, msg):
        """处理服务端控制事件。"""
        if event == proto.EventType.TTSSentenceStart:
            logger.debug("[TTS] 句子开始合成")
        elif event == proto.EventType.TTSSentenceEnd:
            logger.debug("[TTS] 句子合成结束")
        elif event == proto.EventType.TTSResponse:
            pass
        elif event == proto.EventType.TTSSubtitle:
            pass
        elif event == proto.EventType.SessionFinished:
            logger.debug("[TTS] 会话已结束")
        elif event == proto.EventType.SessionFailed:
            error_text = msg.payload.decode("utf-8", "ignore") if msg.payload else "未知错误"
            logger.error(f"[TTS] 会话失败: {error_text}")
        elif event == proto.EventType.SessionCanceled:
            logger.debug("[TTS] 会话已取消")
        elif event == proto.EventType.UsageResponse:
            logger.debug(f"[TTS] 用量响应: {msg.payload.decode('utf-8', 'ignore')}")
        else:
            logger.debug(f"[TTS] 事件: {event}")
