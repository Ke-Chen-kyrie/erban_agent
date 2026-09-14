"""音频播放管理器 —— 封装 AudioPlayer 生命周期、语音打断检测、音频队列清空。"""

import queue
import threading
import time

import numpy as np

from audio.audio_output import AudioPlayer
from config import (
    SPEAKER_DEVICE_INDEX, SPEAKER_DEVICE_NAME,
    OMNI_SAMPLE_RATE,
    BYTEDANCE_TTS_SAMPLE_RATE,
    AUDIO_SOURCES,
)
from agent.utils import get_tts_fallback, reset_audio_card_cache
from logging_config import get_logger

logger = get_logger(__name__)


class AudioPlaybackManager:
    """管理音频播放器（Omni/TTS）和语音打断检测的生命周期。"""

    def __init__(self, audio_queue: queue.Queue):
        self._audio_queue = audio_queue
        self._player: AudioPlayer | None = None
        self._external_player: AudioPlayer | None = None  # 外部传入的播放器（如 CPM 的 persistent player）
        self._lock = threading.Lock()
        self._write_in_progress = 0
        self._write_done = threading.Condition()

        self._interrupted = threading.Event()

        # 播放器连续失败计数（达到上限后停止重试，避免日志刷屏）
        self._player_fail_count = 0
        self._max_player_fails = 5

        # 写入失败计数（连续写入失败时尝试重开播放器）
        self._write_fail_count = 0
        self._max_write_fails = 3  # 连续 3 次写入失败后尝试重开

    # ---- 播放器生命周期 ----

    def ensure_player(self):
        """确保 AudioPlayer 已创建并打开（同步，线程安全）。"""
        with self._lock:
            if self._player is not None:
                return
            # 外部播放器已设置但变为 None（被 close 后），重新使用外部引用
            if self._external_player is not None:
                self._player = self._external_player
                self._player_fail_count = 0
                return
            if self._player_fail_count >= self._max_player_fails:
                return  # 连续失败超限，不再重试，等 close() 重置
            use_tts = AUDIO_SOURCES == "tts" and not get_tts_fallback()
            sr = BYTEDANCE_TTS_SAMPLE_RATE if use_tts else OMNI_SAMPLE_RATE
            reset_audio_card_cache()  # 重建 player 时刷新声卡缓存
            player = AudioPlayer(
                sample_rate=sr,
                device_index=SPEAKER_DEVICE_INDEX,
                device_name=SPEAKER_DEVICE_NAME,
                pcm_dtype="float32",  # USB 声卡通常只支持 float32，与 CPM 播放器保持一致
            )
            # 重试逻辑：CPM 播放器 stop() 后设备可能需要短暂时间释放
            last_err = None
            retry_delays = [0.2, 0.5, 1.0]  # 递增等待，给 ALSA 更多时间释放设备
            for attempt, delay in enumerate(retry_delays):
                try:
                    player.open()
                    self._player = player
                    self._player_fail_count = 0  # 成功则重置
                    return
                except Exception as e:
                    last_err = e
                    if attempt < len(retry_delays) - 1:
                        logger.info(
                            "[audio] 播放设备繁忙，%0.1fs 后重试 (%d/%d)...",
                            delay, attempt + 1, len(retry_delays),
                        )
                        time.sleep(delay)
            # 3 次重试均失败，计入连续失败计数
            self._player_fail_count += 1
            if self._player_fail_count >= self._max_player_fails:
                logger.error(
                    "[audio] 播放设备连续 %d 次打开失败，停止重试: %s",
                    self._player_fail_count, last_err,
                )
            else:
                logger.warning(
                    "[audio] 播放设备打开失败 (%d/%d): %s",
                    self._player_fail_count, self._max_player_fails, last_err,
                )

    def write(self, pcm: bytes):
        """线程安全地写入 PCM 数据到播放器。

        TTS 发送 int16 raw PCM，Omni 发送 WAV（带 RIFF 头）。
        播放器使用 float32，需要在写入前做格式转换。

        注意：player.write() 是阻塞调用（PortAudio），必须在锁外执行，
        否则打断时 close() 无法获取锁来关闭播放器，形成死锁。
        """
        if self._interrupted.is_set():
            return True
        with self._lock:
            player = self._player
            if player is None:
                return

            # WAV 格式（Omni 音频）：soundfile 自动处理格式转换，直接透传
            if pcm[:4] == b'RIFF':
                data = pcm
            else:
                # raw PCM：TTS int16 → float32 转换
                audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                data = audio.tobytes()

            self._write_in_progress += 1

        # 在锁外执行阻塞写入，避免与 close() 死锁
        try:
            success = player.write(data)
        finally:
            with self._write_done:
                self._write_in_progress -= 1
                self._write_done.notify_all()

        with self._lock:
            if success:
                self._write_fail_count = 0  # 成功则重置
            else:
                self._write_fail_count += 1
                if self._write_fail_count >= self._max_write_fails:
                    logger.warning(
                        "[audio] 连续 %d 次写入失败，尝试重开播放器",
                        self._write_fail_count,
                    )
                    # 关闭当前播放器，让下次 ensure_player() 尝试重开
                    if self._player is not None:
                        self._player.close()
                        self._player = None
                    self._player_fail_count = 0  # 重置打开失败计数，允许重试
                    self._write_fail_count = 0

    def drain(self):
        """线程安全地 drain 播放器。"""
        with self._lock:
            if self._player is not None:
                self._player.drain()

    def close(self):
        """线程安全地关闭播放器（外部播放器仅清除引用，不关闭）。"""
        player_to_close = None
        with self._lock:
            if self._player is not None:
                if self._player is self._external_player:
                    self._player = None
                else:
                    player_to_close = self._player
                    self._player = None
            self._player_fail_count = 0

        if player_to_close is not None:
            # 等待正在进行的 write() 完成，避免 PortAudio 并发操作导致 SIGSEGV
            with self._write_done:
                while self._write_in_progress > 0:
                    self._write_done.wait(timeout=0.5)
            player_to_close.close()
            time.sleep(0.15)

    def is_playing(self) -> bool:
        with self._lock:
            return self._player is not None

    # ---- 打断协调 ----

    @property
    def interrupted(self) -> threading.Event:
        return self._interrupted