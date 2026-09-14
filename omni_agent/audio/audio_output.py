import io
import time
import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import resample_poly
from typing import Callable
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
    """使用 OutputStream 阻塞 write 流式播放音频。"""

    def __init__(
        self,
        sample_rate=24000,
        device_index=None,
        device_name=None,
        on_write: Callable[[bytes], None] | None = None,
        on_first_audible: Callable[[float], None] | None = None,
    ):
        self.source_rate = sample_rate
        self._stream = None
        self._output_samples = 0
        self._write_count = 0
        self.first_write_time = None
        self.first_audible_time = None
        self._last_write_time = None
        self.on_write = on_write
        self.on_first_audible = on_first_audible

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

    def open(self):
        """创建并启动输出流（幂等）。"""
        if self._stream is not None:
            return
        out_rate = self._device_rate if self._device_rate else self.source_rate
        self._stream = sd.OutputStream(
            samplerate=out_rate,
            channels=1,
            dtype="int16",
            device=self._device,
        )
        self._stream.start()
        logger.info(f"AudioPlayer: OutputStream 已打开 (采样率 {out_rate} Hz)")

    def write(self, pcm_data: bytes):
        """写入音频数据（支持 WAV 格式和 raw PCM）。"""
        if self._stream is None:
            logger.warning("AudioPlayer: write 调用但 stream 为 None，丢弃音频")
            return
        if self.on_write:
            self.on_write(pcm_data)
        if pcm_data[:4] == b'RIFF':
            buf = io.BytesIO(pcm_data)
            audio_np, sr = sf.read(buf, dtype='int16', always_2d=False)
        else:
            audio_np = np.frombuffer(pcm_data, dtype=np.int16)
        out_rate = self._stream.samplerate
        if self.source_rate != out_rate:
            audio_np = audio_np.astype(np.float32)
            audio_np = resample_poly(audio_np, out_rate, self.source_rate).astype(np.int16)

        first_write = self.first_write_time is None
        write_started_at = time.time()
        self._stream.write(audio_np)
        self._output_samples += len(audio_np)
        self._write_count += 1

        if first_write:
            self.first_write_time = time.time()
            try:
                raw_latency = self._stream.latency
                if isinstance(raw_latency, (tuple, list)):
                    raw_latency = raw_latency[-1]
                output_latency = max(0.0, float(raw_latency or 0.0))
            except (TypeError, ValueError, AttributeError):
                output_latency = 0.0
            # PortAudio 报告的输出延迟用于估算第一帧到达 DAC 的时间。
            self.first_audible_time = write_started_at
            if self.on_first_audible:
                try:
                    self.on_first_audible(self.first_audible_time)
                except Exception as exc:
                    logger.warning(f"AudioPlayer: 首音频时间回调失败: {exc}")
            logger.info(
                f"AudioPlayer: 首次写入 {len(pcm_data)} 字节 "
                f"(源 {self.source_rate}Hz → 设备 {out_rate}Hz, "
                f"输出延迟估算 {output_latency * 1000:.1f}ms)"
            )
        self._last_write_time = time.time()

    def close(self):
        """停止并关闭输出流。"""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def drain(self):
        """等待所有已缓冲的音频播放完毕。"""
        if self._stream is None:
            return
        # 写入一段静音作为 flush：OutputStream.write() 阻塞直到 ring buffer
        # 有空间接纳新数据，这意味着之前所有音频都已播放完毕，不依赖时间估算
        out_rate = self._stream.samplerate
        silence_samples = int(out_rate * AUDIO_DRAIN_SILENCE_MS / 1000.0)
        silence = np.zeros(silence_samples, dtype=np.int16)
        self._stream.write(silence)
        logger.debug(
            f"AudioPlayer.drain: flush 完成 writes={self._write_count}"
        )

    def reset_stats(self):
        """重置采样计数和首写时间戳。"""
        self._output_samples = 0
        self._write_count = 0
        self.first_write_time = None
        self.first_audible_time = None
        self._last_write_time = None