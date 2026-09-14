"""音频播放管理器 —— 封装 AudioPlayer 生命周期、语音打断检测、音频队列清空。"""

import os
import queue
import threading
import time

from audio.audio_output import AudioPlayer
from audio.interrupt_detector import VoiceInterruptDetector
from config import (
    SPEAKER_DEVICE_INDEX, SPEAKER_DEVICE_NAME,
    OMNI_SAMPLE_RATE,
    BYTEDANCE_TTS_SAMPLE_RATE,
    AUDIO_SOURCES,
    INTERRUPT_MIC_SAMPLE_RATE, INTERRUPT_ENERGY_THRESHOLD,
    INTERRUPT_WINDOW_SIZE, INTERRUPT_MIN_SPEECH_FRAMES,
    INTERRUPT_FRAME_MS,
)
from agent.utils import get_tts_fallback, reset_audio_card_cache
from logging_config import get_logger

logger = get_logger(__name__)


class AudioPlaybackManager:
    """管理音频播放器（Omni/TTS）和语音打断检测的生命周期。"""

    def __init__(self, audio_queue: queue.Queue):
        self._audio_queue = audio_queue
        self._player: AudioPlayer | None = None
        self._lock = threading.Lock()

        # 打断检测
        self._voice_interrupt = VoiceInterruptDetector(
            mic_sample_rate=INTERRUPT_MIC_SAMPLE_RATE,
            energy_threshold=INTERRUPT_ENERGY_THRESHOLD,
            window_size=INTERRUPT_WINDOW_SIZE,
            min_speech_frames=INTERRUPT_MIN_SPEECH_FRAMES,
            frame_ms=INTERRUPT_FRAME_MS,
        )
        self._interrupted = threading.Event()
        self._interrupt_stop = threading.Event()
        self._interrupt_thread: threading.Thread | None = None
        self._interrupt_audio_buffer: list = []

        # 调试音频
        self._debug_audio = bytearray()

    # ---- 播放器生命周期 ----

    def ensure_player(self):
        """确保 AudioPlayer 已创建并打开（同步，线程安全）。"""
        with self._lock:
            if self._player is not None:
                return
            use_tts = AUDIO_SOURCES == "tts" and not get_tts_fallback()
            sr = BYTEDANCE_TTS_SAMPLE_RATE if use_tts else OMNI_SAMPLE_RATE
            reset_audio_card_cache()  # 重建 player 时刷新声卡缓存
            player = AudioPlayer(
                sample_rate=sr,
                device_index=SPEAKER_DEVICE_INDEX,
                device_name=SPEAKER_DEVICE_NAME,
                on_write=self._voice_interrupt.feed_reference,
            )
            try:
                player.open()
                self._player = player
            except Exception:
                logger.warning("[audio] 播放设备打开失败，下次音频块重试")

    def write(self, pcm: bytes):
        """线程安全地写入 PCM 数据到播放器。"""
        with self._lock:
            if self._player is not None:
                self._player.write(pcm)

    def drain(self):
        """线程安全地 drain 播放器。"""
        with self._lock:
            if self._player is not None:
                self._player.drain()

    def close(self):
        """线程安全地关闭播放器。"""
        with self._lock:
            if self._player is not None:
                self._player.close()
                self._player = None

    def is_playing(self) -> bool:
        with self._lock:
            return self._player is not None

    # ---- 打断检测 ----

    @property
    def interrupted(self) -> threading.Event:
        return self._interrupted

    @property
    def interrupt_audio_buffer(self) -> list:
        return self._interrupt_audio_buffer

    @property
    def interrupt_detector(self) -> VoiceInterruptDetector:
        return self._voice_interrupt

    def start_interrupt_detection(self):
        """启动语音打断检测线程（仅在音频播放期间运行）。"""
        if self._interrupt_thread is not None:
            if self._interrupt_thread.is_alive():
                logger.warning("[打断] 旧打断线程仍在运行，强制停止")
                self._interrupt_stop.set()
                self._interrupt_thread.join(timeout=1)
            self._interrupt_thread = None

        self._voice_interrupt.reset()
        self._interrupt_audio_buffer.clear()
        self._interrupt_stop.clear()
        self._interrupt_thread = threading.Thread(
            target=self._interrupt_loop, daemon=True
        )
        self._interrupt_thread.start()
        logger.info("[打断] 开始监听用户插话...")

    def stop_interrupt_detection(self):
        """停止语音打断检测线程。"""
        if self._interrupt_thread is None:
            return
        self._interrupt_stop.set()
        self._interrupt_thread.join(timeout=2)
        if self._interrupt_thread.is_alive():
            logger.warning("[打断] 打断线程未能在 2s 内退出，可能存在线程泄漏")
        else:
            self._interrupt_thread = None
        logger.info("[打断] 停止监听用户插话")

    def _interrupt_loop(self):
        """后台线程：轮询 mic 音频，检测用户是否插话打断。"""
        poll_count = 0
        self._interrupt_audio_buffer.clear()
        while not self._interrupt_stop.is_set():
            try:
                audio_chunk = self._audio_queue.get(timeout=0.3)
                poll_count += 1
                if poll_count % 25 == 1:
                    logger.debug(
                        f"[打断轮询] 已轮询 {poll_count} 次, "
                        f"queue≈{self._audio_queue.qsize()}"
                    )
                self._interrupt_audio_buffer.append(audio_chunk)
                fs = self._voice_interrupt.frame_samples
                for offset in range(0, len(audio_chunk), fs):
                    if offset + fs > len(audio_chunk):
                        sub_frame = audio_chunk[-fs:]
                    else:
                        sub_frame = audio_chunk[offset:offset + fs]
                    if self._voice_interrupt.process_mic(sub_frame):
                        logger.info(
                            f"\n[打断] 检测到用户说话 (轮询第 {poll_count} 次, offset={offset})"
                        )
                        self._interrupted.set()
                        break
                self._audio_queue.task_done()
                if self._interrupted.is_set():
                    break
            except queue.Empty:
                poll_count += 1
                if poll_count % 50 == 1:
                    logger.debug(f"[打断轮询] queue 持续为空 (已轮询 {poll_count} 次)")
                continue
            except Exception as e:
                logger.error(f"\n[打断检测] 异常: {e}")
                break

    # ---- 音频队列 ----

    def drain_audio_queue(self):
        """清空音频队列中 AI 播报期间的残余音频，避免回声被 ASR 误识别。"""
        drained = 0
        while True:
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.task_done()
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.info(f"[audio] 丢弃播报期间残余音频 {drained} 帧")

    # ---- 调试 ----

    def start_debug_capture(self):
        self._debug_audio = bytearray()

    def append_debug_audio(self, pcm: bytes):
        self._debug_audio.extend(pcm)

    def save_debug_audio(self):
        if not self._debug_audio:
            return
        os.makedirs("debug_files", exist_ok=True)
        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        path = f"debug_files/omni_debug_{ts}.wav"
        data = bytes(self._debug_audio)
        if data[:4] == b'RIFF':
            with open(path, "wb") as f:
                f.write(data)
        else:
            import wave as _wave
            with _wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(OMNI_SAMPLE_RATE)
                wf.writeframes(data)
        dur = len(data) / (OMNI_SAMPLE_RATE * 2)
        logger.info(f"[audio debug] Omni 音频已保存: {path} ({len(data)} bytes, {dur:.2f}s, 格式={'WAV' if data[:4]==b'RIFF' else 'PCM'})")
        self._debug_audio = bytearray()

    def reset(self):
        """重置所有状态（新会话开始前调用）。"""
        self._interrupted.clear()
        self._interrupt_audio_buffer.clear()
        self._debug_audio = bytearray()