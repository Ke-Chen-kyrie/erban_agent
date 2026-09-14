#!/usr/bin/env python3
"""
火山引擎 SeedASR 2.0 — 豆包流式语音识别模型2.0
双向流式优化版 + 二遍识别，自动 VAD 分句，兼顾速度与准确率

继承 BaseASR，可直接替换其他 ASR 实现。
"""

import json
import struct
import gzip
import uuid as uuid_lib
import threading
import time
import queue

import numpy as np
import websocket

from asr.base_asr import BaseASR, SentenceResult
from config import (
    BYTEDANCE_API_KEY,
    BYTEDANCE_APP_KEY,
    BYTEDANCE_ACCESS_KEY,
    BYTEDANCE_RESOURCE_ID,
    BYTEDANCE_ASR_URL,
    ASR_END_WINDOW_SIZE,
    ASR_KEEPALIVE_INTERVAL,
)
from logging_config import get_logger

logger = get_logger(__name__)


# ============================================================
# 协议常量
# ============================================================

class _Proto:
    VERSION = 0b0001
    HEADER_SIZE = 0b0001  # x4 = 4 bytes

    MSG_FULL_CLIENT = 0b0001
    MSG_AUDIO_ONLY = 0b0010
    MSG_FULL_SERVER = 0b1001
    MSG_ERROR = 0b1111

    FLAG_NO_SEQ = 0b0000
    FLAG_POS_SEQ = 0b0001
    FLAG_NEG_NO_SEQ = 0b0010
    FLAG_NEG_WITH_SEQ = 0b0011

    SER_JSON = 0b0001
    SER_NONE = 0b0000

    COMP_GZIP = 0b0001
    COMP_NONE = 0b0000


# ============================================================
# ByteDanceASR
# ============================================================

class ByteDanceASR(BaseASR):
    """火山引擎 SeedASR 2.0 — WebSocket 直连实现，继承 BaseASR"""

    def __init__(self, sample_rate=16000):
        super().__init__(sample_rate=sample_rate)
        self._api_key = BYTEDANCE_API_KEY
        self._app_key = BYTEDANCE_APP_KEY
        self._access_key = BYTEDANCE_ACCESS_KEY
        self._resource_id = BYTEDANCE_RESOURCE_ID
        self._url = BYTEDANCE_ASR_URL

    # ---------- 鉴权 ----------

    def _build_headers(self) -> dict:
        headers = {
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": str(uuid_lib.uuid4()),
            "X-Api-Sequence": "-1",
        }
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        else:
            headers["X-Api-App-Key"] = self._app_key
            headers["X-Api-Access-Key"] = self._access_key
        return headers

    # ---------- 协议构建 ----------

    @staticmethod
    def _build_header(msg_type: int, flags: int, ser: int, comp: int) -> bytes:
        b0 = (_Proto.VERSION << 4) | _Proto.HEADER_SIZE
        b1 = (msg_type << 4) | flags
        b2 = (ser << 4) | comp
        b3 = 0x00
        return bytes([b0, b1, b2, b3])

    def _build_full_request(self) -> bytes:
        header = self._build_header(
            _Proto.MSG_FULL_CLIENT, _Proto.FLAG_POS_SEQ, _Proto.SER_JSON, _Proto.COMP_GZIP
        )

        payload = {
            "user": {"uid": "xiaoban_robot"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": "zh-CN",
            },
            "request": {
                "model_name": "bigmodel",
                "enable_nonstream": True,
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "end_window_size": ASR_END_WINDOW_SIZE,
                "result_type": "single",
            },
        }

        payload_bytes = json.dumps(payload).encode("utf-8")
        compressed = gzip.compress(payload_bytes)

        seq = self._seq
        self._seq += 1

        return header + struct.pack(">i", seq) + struct.pack(">I", len(compressed)) + compressed

    def _build_audio_request(self, pcm_bytes: bytes, is_last: bool = False) -> bytes:
        if is_last:
            flags = _Proto.FLAG_NEG_WITH_SEQ
            seq = -self._seq
        else:
            flags = _Proto.FLAG_POS_SEQ
            seq = self._seq

        header = self._build_header(_Proto.MSG_AUDIO_ONLY, flags, _Proto.SER_NONE, _Proto.COMP_GZIP)
        compressed = gzip.compress(pcm_bytes)

        if not is_last:
            self._seq += 1

        return header + struct.pack(">i", seq) + struct.pack(">I", len(compressed)) + compressed

    # ---------- 响应解析 ----------

    @staticmethod
    def _parse_response(data: bytes) -> dict | None:
        if len(data) < 4:
            return None

        msg_type = (data[1] >> 4) & 0x0F
        flags = data[1] & 0x0F
        ser = (data[2] >> 4) & 0x0F
        comp = data[2] & 0x0F

        header_size = (data[0] & 0x0F) * 4
        offset = header_size

        seq = None
        if flags & 0x01:
            if offset + 4 <= len(data):
                seq = struct.unpack(">i", data[offset:offset + 4])[0]
                offset += 4

        is_last = bool(flags & 0x02)

        if msg_type == _Proto.MSG_ERROR:
            if offset + 4 <= len(data):
                code = struct.unpack(">i", data[offset:offset + 4])[0]
                offset += 4
                if offset + 4 <= len(data):
                    size = struct.unpack(">I", data[offset:offset + 4])[0]
                    offset += 4
                    err_msg = data[offset:offset + size].decode("utf-8", errors="replace")
                    return {"type": "error", "code": code, "message": err_msg}
            return {"type": "error", "code": -1, "message": "parse error"}

        if msg_type == _Proto.MSG_FULL_SERVER:
            if offset + 4 <= len(data):
                size = struct.unpack(">I", data[offset:offset + 4])[0]
                offset += 4
                payload = data[offset:offset + size]

                if comp == _Proto.COMP_GZIP:
                    try:
                        payload = gzip.decompress(payload)
                    except Exception:
                        pass

                if ser == _Proto.SER_JSON:
                    try:
                        result = json.loads(payload.decode("utf-8"))
                    except json.JSONDecodeError:
                        return None
                    result["_seq"] = seq
                    result["_is_last"] = is_last
                    return result

        return None

    # ---------- 核心方法 ----------

    def start_listening(self, audio_queue):
        if not self._active.is_set():
            return

        my_session = self._session_id
        ws = None
        recv = None
        stop_recv = threading.Event()
        speech_started = False
        speech_begin_time = 0.0
        self._seq = 1

        # 已通过 sentence_queue 送出的句子数，用于在 _is_last 时判断是否需要补发
        sentences_sent = 0

        def recv_thread():
            nonlocal speech_started, speech_begin_time
            shown_text = ""

            ws.settimeout(0.5)
            while not stop_recv.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    break
                except Exception as e:
                    logger.error(f"[ByteDance recv] 异常: {e}")
                    break

                resp = self._parse_response(raw)
                if resp is None:
                    continue

                if resp.get("type") == "error":
                    logger.error(f"[ByteDance] 错误: code={resp['code']} msg={resp['message']}")
                    continue

                result = resp.get("result", {})
                text = result.get("text", "")

                # 实时显示部分结果
                if text and text != shown_text:
                    shown_text = text
                    _now = time.time()
                    print(f"\r[识别中 {_now:.3f}]: {text}", end="", flush=True)

                    if not speech_started:
                        speech_started = True
                        speech_begin_time = _now
                        self.last_speech_time = _now
                        logger.info(f"\n[开始说话 {_now:.3f}]")
                        if self.speech_begin_callback:
                            self.speech_begin_callback()

                    self.last_speech_time = _now

                # 二遍识别返回的 definite 分句
                utterances = result.get("utterances", [])
                for utt in utterances:
                    if utt.get("definite") and utt.get("text", "").strip():
                        final_text = utt["text"].strip()
                        now = time.time()
                        video_frames = None
                        wav_bytes = self.save_audio_to_wav()
                        logger.info(f"\n[识别完成 {now:.3f}]：{final_text}")
                        self.sentence_queue.put(SentenceResult(
                            sentence=final_text, timestamp=now,
                            video_frames=video_frames, speech_begin_time=speech_begin_time,
                            wav_bytes=wav_bytes,
                        ))
                        nonlocal sentences_sent
                        sentences_sent += 1
                        shown_text = ""
                        speech_begin_time = 0.0
                        speech_started = False

                # 服务端处理完所有音频
                if resp.get("_is_last"):
                    if shown_text and shown_text.strip():
                        final_text = shown_text.strip()
                        now = time.time()
                        video_frames = None
                        wav_bytes = self.save_audio_to_wav()
                        logger.info(f"\n[识别完成 {now:.3f}]：{final_text}")
                        self.sentence_queue.put(SentenceResult(
                            sentence=final_text, timestamp=now,
                            video_frames=video_frames, speech_begin_time=speech_begin_time,
                            wav_bytes=wav_bytes,
                        ))
                        sentences_sent += 1
                    break

            # 线程退出前 flush 未完成的累积文本（含 definite 已送出后又累积的部分结果）
            if shown_text and shown_text.strip():
                final_text = shown_text.strip()
                now = time.time()
                video_frames = None
                wav_bytes = self.save_audio_to_wav()
                logger.info(f"\n[识别完成 {now:.3f}]：{final_text}")
                self.sentence_queue.put(SentenceResult(
                            sentence=final_text, timestamp=now,
                            video_frames=video_frames, speech_begin_time=speech_begin_time,
                            wav_bytes=wav_bytes,
                        ))

        try:
            headers = self._build_headers()
            logger.info(f"[ByteDance] 连接: {self._url} (resource_id={self._resource_id})")
            ws = websocket.create_connection(self._url, header=headers, timeout=15)

            # 发送 full client request
            full_req = self._build_full_request()
            ws.send_binary(full_req)
            logger.info("[ByteDance] Full client request 已发送")

            # 接收握手响应
            raw = ws.recv()
            resp = self._parse_response(raw)
            if resp and resp.get("type") == "error":
                logger.error(f"[ByteDance] 握手错误: code={resp['code']} msg={resp['message']}")
                return
            logger.info("[ByteDance] 握手成功，开始识别...")

            # 启动接收线程
            recv = threading.Thread(target=recv_thread, daemon=True)
            recv.start()

            # 发送音频循环
            logger.info("已进入云端说话模式...")
            self.last_speech_time = time.time()

            while self._active.is_set() and self._session_id == my_session:
                if self._paused.is_set():
                    # AI 播放期间仅发静音保活，不消费队列，把音频留给打断检测
                    time.sleep(0.2)
                    # 发静音保活防超时（服务端 8s 超时）
                    now = time.time()
                    if not hasattr(self, '_last_keepalive') or now - self._last_keepalive > ASR_KEEPALIVE_INTERVAL:
                        self._last_keepalive = now
                        silence = np.zeros(int(0.2 * self.sample_rate), dtype=np.float32)
                        try:
                            ws.send_binary(self._build_audio_request(
                                (silence * 32767).astype(np.int16).tobytes()
                            ))
                        except Exception:
                            pass
                    continue

                try:
                    audio_chunk = audio_queue.get(timeout=0.2)
                    audio_int16 = (np.clip(audio_chunk, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                    ws.send_binary(self._build_audio_request(audio_int16))
                    with self._audio_lock:
                        self._audio_buffer.append(audio_chunk.copy())
                    audio_queue.task_done()
                    self._last_keepalive = time.time()
                except queue.Empty:
                    # 队列为空时发静音保活，防止服务端 8s 超时断连
                    now = time.time()
                    if not hasattr(self, '_last_keepalive') or now - self._last_keepalive > ASR_KEEPALIVE_INTERVAL:
                        self._last_keepalive = now
                        silence = np.zeros(int(0.2 * self.sample_rate), dtype=np.float32)
                        try:
                            ws.send_binary(self._build_audio_request(
                                (silence * 32767).astype(np.int16).tobytes()
                            ))
                        except Exception:
                            pass
                    continue
                except Exception as e:
                    logger.error(f"[ByteDance] 音频发送异常: {e}")
                    self._active.clear()
                    break

        except Exception as e:
            logger.error(f"ByteDance ASR 错误: {e}")
        finally:
            # 发送结束标识（负包）
            if ws is not None:
                try:
                    ws.send_binary(self._build_audio_request(b"", is_last=True))
                    logger.info(f"[ByteDance] 已发送结束标识 (seq=-{self._seq})")
                except Exception as e:
                    logger.error(f"[ByteDance] 发送结束标识异常: {e}")

                # 等待 recv 线程收到最终响应
                stop_recv.set()
                if recv is not None:
                    recv.join(timeout=2.0)
                try:
                    ws.close()
                except Exception:
                    pass

            if self._session_id == my_session:
                self._active.clear()
                self._audio_reserved.clear()
                logger.info("[ByteDance] 云端识别结束")