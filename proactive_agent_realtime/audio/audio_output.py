import io
import queue
import threading
import time

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import resample_poly

from logging_config import get_logger
from config import AUDIO_DRAIN_SILENCE_MS

logger = get_logger(__name__)


def find_sd_device_by_name(keyword, kind='output'):
    """按名称关键词在 sounddevice 中查找设备。"""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if keyword.lower() in dev['name'].lower():
            if kind == 'output' and dev['max_output_channels'] > 0:
                return i
            if kind == 'input' and dev['max_input_channels'] > 0:
                return i
    return None


class AudioPlayer:
    """使用 OutputStream 播放音频，支持阻塞模式和队列模式。

    阻塞模式（默认）::

        player = AudioPlayer(sample_rate=24000)
        player.open()
        player.write(pcm_bytes)   # 阻塞直到写入完成
        player.drain()
        player.close()

    队列模式（main.py CPM 路径）::

        player = AudioPlayer(sample_rate=24000, pcm_dtype="float32")
        player.start()
        player.enqueue(pcm_bytes)  # 非阻塞
        ...
        player.stop()
    """

    def __init__(
        self,
        sample_rate=24000,
        device_index=None,
        device_name=None,
        pcm_dtype: str = "int16",
    ):
        self.source_rate = sample_rate
        self._pcm_dtype = pcm_dtype
        self._stream = None
        self._stream_lock = threading.Lock()   # 串行化 write/open/close，避免并发操作同一 PortAudio 流
        self._output_samples = 0
        self._write_count = 0
        self.first_write_time = None
        self.first_audible_time = None
        self._last_write_time = None

        # ── 队列模式 ──
        self._queue: queue.Queue[bytes | None] | None = None
        self._queue_thread: threading.Thread | None = None
        self._queue_stop = threading.Event()

        try:
            if device_name is not None:
                idx = find_sd_device_by_name(device_name, kind='output')
                if idx is not None:
                    device_index = idx
                else:
                    logger.warning(f"找不到扬声器设备 (关键词: {device_name})，使用默认设备")
                    device_name = None

            if device_index is not None:
                default_info = sd.query_devices(device_index)
                self._device = device_index
            else:
                default_info = sd.query_devices(kind='output')
                self._device = default_info['index']
            self._device_rate = int(default_info['default_samplerate'])
            logger.info(f"播放设备: {default_info['name']} (索引 {self._device}, 采样率 {self._device_rate} Hz)")
        except Exception:
            self._device_rate = None
            self._device = None

    # ──────────────────── 阻塞模式 API ────────────────────

    def open(self):
        """创建并启动输出流（幂等）。"""
        with self._stream_lock:
            if self._stream is not None:
                return
            out_rate = self._device_rate if self._device_rate else self.source_rate
            try:
                self._stream = sd.OutputStream(
                    samplerate=out_rate,
                    channels=1,
                    dtype=self._pcm_dtype,
                    device=self._device,
                )
                self._stream.start()
            except Exception:
                # PortAudio/ALSA 可能处于不一致状态，重置后重试一次
                logger.warning("AudioPlayer: 打开设备失败，尝试重置 PortAudio 后重试")
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
                self._stream = sd.OutputStream(
                    samplerate=out_rate,
                    channels=1,
                    dtype=self._pcm_dtype,
                    device=self._device,
                )
                self._stream.start()
            logger.info(f"AudioPlayer: OutputStream 已打开 (采样率 {out_rate} Hz, dtype={self._pcm_dtype})")

    def write(self, pcm_data: bytes) -> bool:
        """写入音频数据（支持 WAV 格式和 raw PCM）。返回是否成功。"""
        # 与 open()/close() 串行化：并发操作同一 PortAudio 流会损坏其内部缓冲（ALSA -9999 / 堆损坏崩溃）
        with self._stream_lock:
            stream = self._stream
            if stream is None:
                logger.warning("AudioPlayer: write 调用但 stream 为 None，丢弃音频")
                return False
            if pcm_data[:4] == b'RIFF':
                buf = io.BytesIO(pcm_data)
                audio_np, sr = sf.read(buf, dtype=self._pcm_dtype, always_2d=False)
            else:
                audio_np = np.frombuffer(pcm_data, dtype=self._pcm_dtype)
            out_rate = stream.samplerate
            if self.source_rate != out_rate:
                # 重采样始终在 float32 下进行以保留精度
                audio_f32 = audio_np.astype(np.float32) if self._pcm_dtype != "float32" else audio_np.copy()
                audio_f32 = resample_poly(audio_f32, out_rate, self.source_rate)
                audio_np = audio_f32.astype(self._pcm_dtype) if self._pcm_dtype != "float32" else audio_f32

            first_write = self.first_write_time is None
            write_started_at = time.time()
            try:
                stream.write(audio_np)
            except Exception as e:
                logger.warning("AudioPlayer: 写入失败 (可能是设备断开): %s", e)
                return False
            self._output_samples += len(audio_np)
            self._write_count += 1

            if first_write:
                self.first_write_time = time.time()
                try:
                    raw_latency = stream.latency
                    if isinstance(raw_latency, (tuple, list)):
                        raw_latency = raw_latency[-1]
                    output_latency = max(0.0, float(raw_latency or 0.0))
                except (TypeError, ValueError, AttributeError):
                    output_latency = 0.0
                self.first_audible_time = write_started_at
                logger.info(
                    f"AudioPlayer: 首次写入 {len(pcm_data)} 字节 "
                    f"(源 {self.source_rate}Hz → 设备 {out_rate}Hz, "
                    f"dtype={self._pcm_dtype}, "
                    f"输出延迟估算 {output_latency * 1000:.1f}ms)"
                )
            self._last_write_time = time.time()
            return True

    def close(self):
        """停止并关闭输出流。"""
        with self._stream_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
        # PortAudio/ALSA 不会同步释放硬件，给 ALSA 时间清理
        time.sleep(0.15)

    def drain(self):
        """等待所有已缓冲的音频播放完毕。"""
        if self._stream is None:
            return
        out_rate = self._stream.samplerate
        try:
            device_latency = self._stream.latency
            if isinstance(device_latency, (tuple, list)):
                device_latency = device_latency[-1]
            device_latency = max(0.0, float(device_latency or 0.05))
        except (TypeError, ValueError, AttributeError):
            device_latency = 0.05

        # 基于实际写入的音频总时长计算播放完成时间，确保长 TTS 响应也能完整播放
        if self.first_write_time is not None and self._output_samples > 0:
            total_duration = self._output_samples / out_rate
            expected_end = self.first_write_time + total_duration + device_latency
            remaining = expected_end - time.time()
            if remaining > 0:
                logger.debug(
                    f"AudioPlayer.drain: 等待 {remaining:.2f}s 播放完成 "
                    f"(total_duration={total_duration:.2f}s, device_latency={device_latency*1000:.0f}ms)"
                )
                time.sleep(remaining + 0.1)
        else:
            # 未曾写入音频时的兜底
            silence_samples = int(out_rate * AUDIO_DRAIN_SILENCE_MS / 1000.0)
            silence = np.zeros(silence_samples, dtype=self._pcm_dtype)
            self._stream.write(silence)
            wait_s = device_latency + AUDIO_DRAIN_SILENCE_MS / 1000.0 + 0.1
            time.sleep(wait_s)
            logger.debug(
                f"AudioPlayer.drain: flush 完成 (device_latency={device_latency*1000:.0f}ms, "
                f"silence={AUDIO_DRAIN_SILENCE_MS}ms, wait={wait_s*1000:.0f}ms) writes={self._write_count}"
            )

    def reset_stats(self):
        """重置采样计数和首写时间戳。"""
        self._output_samples = 0
        self._write_count = 0
        self.first_write_time = None
        self.first_audible_time = None
        self._last_write_time = None

    def is_playing(self) -> bool:
        """是否有音频正在播放（队列非空 或 最近写入尚未播完）。"""
        if self._queue is not None:
            try:
                if self._queue.qsize() > 0:
                    return True
            except Exception:
                pass
        if self._last_write_time is not None:
            out_rate = self._device_rate if self._device_rate else self.source_rate
            bytes_per_sec = out_rate * 2  # int16 mono
            if self._output_samples > 0 and self.first_write_time is not None:
                expected_end = self.first_write_time + self._output_samples / bytes_per_sec
                if time.time() < expected_end + 0.1:
                    return True
        return False

    def clear(self):
        """清空缓冲中的音频（用于 VAD 打断）。"""
        if self._queue is not None:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    # ──────────────────── 队列模式 API ────────────────────

    def start(self):
        """启动队列模式：打开输出流 + 后台播放线程。"""
        if self._queue_thread is not None and self._queue_thread.is_alive():
            return
        self.open()
        self.reset_stats()
        self._queue = queue.Queue(maxsize=900)
        self._queue_stop.clear()
        self._queue_thread = threading.Thread(
            target=self._queue_worker, daemon=True, name="audio-player-queue"
        )
        self._queue_thread.start()
        logger.info("AudioPlayer: 队列模式已启动")

    def enqueue(self, data: bytes):
        """非阻塞入队。队列满时丢弃最旧的块。"""
        if self._queue is None or self._queue_stop.is_set():
            return
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            try:
                self._queue.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(data)
            except queue.Full:
                pass

    def stop(self):
        """停止队列模式：等待已入队的音频播放完毕 + 关闭线程 + 关闭输出流。"""
        self._queue_stop.set()
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)  # sentinel，放在最后让 worker 播完再退出
            except queue.Full:
                pass
        if self._queue_thread is not None:
            # 根据队列中残留的音频量动态计算等待时间
            qsize = 0
            if self._queue is not None:
                try:
                    qsize = self._queue.qsize()
                except Exception:
                    pass
            # 每个 chunk 约 80-100ms 音频，加 2s 安全余量
            drain_timeout = max(5.0, qsize * 0.1 + 2.0)
            self._queue_thread.join(timeout=drain_timeout)
            if self._queue_thread.is_alive():
                # Worker 未能在超时内退出，强制关闭流以释放 ALSA 设备
                logger.warning("AudioPlayer: worker 未能在 %.1fs 内退出，强制关闭流", drain_timeout)
                self.close()
                # 再给 worker 0.5s 来响应流关闭并退出
                self._queue_thread.join(timeout=0.5)
            self._queue_thread = None
        # 清空队列残留（worker 已退出，剩余的不会播放了）
        if self._queue is not None:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queue = None
        self.drain()
        self.close()
        logger.info("AudioPlayer: 队列模式已停止")

    def _queue_worker(self):
        """后台线程：从队列取数据，调用 write() 播放。"""
        while not self._queue_stop.is_set():
            try:
                chunk = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                break
            try:
                self.write(chunk)
            except Exception as e:
                logger.warning("AudioPlayer: queue worker write error: %s", e)
