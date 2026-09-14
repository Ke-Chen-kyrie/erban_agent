import io
import os
import time
import wave
import queue
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
from logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class SentenceResult:
    """ASR 识别结果，替代裸 tuple 入队。"""
    sentence: str
    timestamp: float
    video_frames: Optional[list] = None
    speech_begin_time: float = 0.0
    wav_bytes: bytes = b""


class BaseASR:
    """ASR 基类：封装音频缓冲、WAV 导出、会话生命周期、结果队列、视频回调等共享逻辑。
    子类只需实现 start_listening(audio_queue)。"""

    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self._active = threading.Event()
        self._audio_reserved = threading.Event()
        self._paused = threading.Event()
        self._session_id = 0
        self.silence_timeout = 15.0
        self.last_speech_time = 0.0

        self.sentence_queue = queue.Queue()
        self._audio_buffer: list = []
        self._audio_lock = threading.Lock()

        self.speech_begin_callback = None

    def set_speech_begin_callback(self, cb):
        self.speech_begin_callback = cb

    def get_speech_begin_callback(self):
        return self.speech_begin_callback

    # ---- WakeupDetector 协调 ----

    def is_asr_active(self) -> bool:
        return self._active.is_set() or self._audio_reserved.is_set()

    def reserve_audio_for_asr(self):
        self._audio_reserved.set()

    # ---- 会话生命周期 ----

    def activate_listening(self):
        self._session_id += 1
        self._active.set()
        self._paused.clear()
        self._audio_reserved.clear()
        self.last_speech_time = time.time()
        with self._audio_lock:
            self._audio_buffer.clear()

    def deactivate_listening(self):
        self._active.clear()
        self._audio_reserved.clear()

    def pause(self):
        """暂停音频发送，WebSocket 保持连接。"""
        self._paused.set()

    def resume(self):
        """恢复音频发送，重置静音计时器。"""
        self._paused.clear()
        self.last_speech_time = time.time()

    # ---- 子类必须实现 ----

    def start_listening(self, audio_queue):
        raise NotImplementedError

    # ---- 结果队列 ----

    def get_sentence(self):
        try:
            return self.sentence_queue.get_nowait()
        except queue.Empty:
            return None

    def drain_sentences(self):
        while not self.sentence_queue.empty():
            try:
                self.sentence_queue.get_nowait()
            except queue.Empty:
                break

    def snapshot_audio(self) -> list:
        """快照并清空当前句子的音频缓冲。"""
        with self._audio_lock:
            buffer = self._audio_buffer
            self._audio_buffer = []
        return buffer

    def save_audio_to_wav(self, debug_dir: str = "") -> bytes:
        """快照音频缓冲并转为 WAV bytes。debug_dir 非空时额外写盘。"""
        buffer = self.snapshot_audio()
        if not buffer:
            logger.warning("[ASR] 音频缓冲区为空，无法保存 WAV")
            return b""
        audio_data = np.concatenate(buffer)
        duration = len(audio_data) / self.sample_rate
        logger.info(f"[ASR] 缓冲音频: {len(audio_data)} 样本, {duration:.2f}s")
        int_data = (audio_data * 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(int_data.tobytes())
        wav_bytes = buf.getvalue()

        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(debug_dir, f"asr_{ts}.wav")
            with open(path, "wb") as f:
                f.write(wav_bytes)
            logger.info(f"[ASR] WAV 已保存: {path} ({len(wav_bytes)} bytes, {duration:.2f}s)")

        return wav_bytes