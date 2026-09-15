import pyaudio
import numpy as np
import threading
import subprocess
import re
from scipy.signal import resample_poly
from logging_config import get_logger

logger = get_logger(__name__)


def _parse_arecord_l():
    """解析 arecord -l 输出，返回 {card_number: (short_name, long_desc)} 字典。"""
    cards = {}
    try:
        result = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return cards

    for line in result.stdout.split("\n"):
        m = re.match(r"card\s+(\d+):\s*(\S+)\s*\[(.+?)\],\s*device", line)
        if m:
            card_num = int(m.group(1))
            short_name = m.group(2)
            long_desc = m.group(3)
            cards[card_num] = (short_name, long_desc)
    return cards


def _lookup_alsa_dsnoop_device(keyword):
    """用关键词在 arecord -l 中查找设备，返回 (card, device, sample_rate, dsnoop_str)。"""
    kw = keyword.lower()
    cards = _parse_arecord_l()
    for card_num, (short_name, long_desc) in cards.items():
        if kw in short_name.lower() or kw in long_desc.lower():
            rate = _probe_alsa_rate(card_num, 0)
            # 优先用 dsnoop（可共享），不行再用 plughw
            dsnoop_dev = f"dsnoop:{card_num},0"
            plughw_dev = f"plughw:{card_num},0"
            if _test_alsa_device(dsnoop_dev):
                return (card_num, 0, rate, dsnoop_dev)
            return (card_num, 0, rate, plughw_dev)
    return None


def _test_alsa_device(alsa_dev):
    """测试 ALSA 设备能否打开（--dump-hw-params），返回 True/False。"""
    try:
        result = subprocess.run(
            ["arecord", "--dump-hw-params", "-D", alsa_dev, "-f", "S16_LE", "-c", "1", "-r", "16000", "-d", "0", "/dev/null"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _probe_alsa_rate(card_num, device_num):
    """探测 ALSA 设备支持的采样率，优先返回 16000 或 44100 中可用者。"""
    alsa_dev = f"hw:{card_num},{device_num}"
    try:
        result = subprocess.run(
            ["arecord", "--dump-hw-params", "-D", alsa_dev, "-f", "S16_LE", "-c", "1", "-r", "16000", "-d", "0", "/dev/null"],
            capture_output=True, text=True, timeout=5
        )
        # 输出中包含 "RATE: 16000" 表示支持
        if "RATE: 16000" in result.stdout or "RATE: 16000" in result.stderr:
            return 16000
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return 44100  # 默认 44100，几乎总是支持


def _find_pyaudio_device_by_alsa_card(pa, card_number, is_input=True):
    """通过 ALSA 卡号在 PyAudio 设备列表中查找匹配的设备。"""
    patterns = [
        f"hw:{card_number}",
        f"plughw:{card_number}",
        f"hw:{card_number},",
        f"plughw:{card_number},",
        f"card {card_number}:",
        f"card={card_number}",
    ]
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info["name"].lower()
        if is_input and info["maxInputChannels"] < 1:
            continue
        if not is_input and info["maxOutputChannels"] < 1:
            continue
        for pat in patterns:
            if pat in name:
                return info["index"]
    return None


def find_device_by_name(pa, keyword, is_input=True):
    """按名称关键词查找设备索引，返回第一个匹配的设备。

    匹配策略（按优先级）：
    1. PyAudio 设备名匹配
    2. arecord -l 输出描述匹配 → 反向查找 PyAudio 索引
    """
    kw = keyword.lower()

    # 策略1: PyAudio 设备名匹配
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info["name"].lower()
        if kw in name:
            if is_input and info["maxInputChannels"] > 0:
                return info["index"]
            if not is_input and info["maxOutputChannels"] > 0:
                return info["index"]

    # 策略1 失败，打印所有 PortAudio 设备供调试
    kind = "输入" if is_input else "输出"
    logger.info(f"PortAudio {kind}设备列表 (关键词 \"{keyword}\" 未直接命中):")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        channels = info["maxInputChannels"] if is_input else info["maxOutputChannels"]
        if channels > 0:
            logger.info(f"  [{i}] {info['name']} (ch={channels}, rate={int(info['defaultSampleRate'])}Hz)")

    # 策略2: arecord -l 描述匹配
    arecord_cards = _parse_arecord_l()
    for card_num, (short_name, long_desc) in arecord_cards.items():
        if kw in short_name.lower() or kw in long_desc.lower():
            idx = _find_pyaudio_device_by_alsa_card(pa, card_num, is_input)
            if idx is not None:
                logger.info(
                    f"通过 arecord -l 匹配到设备: card {card_num} "
                    f"[{short_name}] ({long_desc[:60]}) → PyAudio index {idx}"
                )
                return idx
            else:
                logger.info(
                    f"arecord -l 找到 card {card_num} [{short_name}] ({long_desc[:60]}), "
                    f"PyAudio 中不可用，将通过 arecord 子进程采集"
                )

    return None


class AudioCapture:
    """使用系统麦克风采集音频，以设备原生采样率录制，统一输出 16kHz 浮点音频。"""

    TARGET_RATE = 16000

    def __init__(self, chunk_size=1600, device_index=None, device_name=None,
                 noise_gate_enabled=False, noise_gate_threshold=0.014,
                 noise_gate_onset_delta=0.0,
                 noise_gate_release_alpha=0.3, noise_gate_close_ratio=0.85):
        self.chunk_size = chunk_size
        self.audio_queue = None
        self.is_running = threading.Event()
        self.p = pyaudio.PyAudio()
        self._arecord_proc = None  # arecord 子进程（仅 ALSA 兜底模式）
        self._arecord_device = None  # ALSA hw 设备字符串

        # ── 远讲抑制（噪声门，P + I + 可选 D）──
        self._gate_enabled = noise_gate_enabled
        self._gate_threshold = noise_gate_threshold        # P：瞬时 rms 开门阈值
        self._gate_onset_delta = noise_gate_onset_delta    # D：能量陡升预开（0=关闭）
        self._gate_open = True                              # 当前门状态（初始开启）
        self._gate_gain = 1.0                               # 应用增益（软渐变防爆音）
        self._rms_env = 0.0                                 # I：泄漏 EMA 包络（慢关判据）
        self._rms_prev = 0.0                                # D：上一帧 rms
        self._gate_release_alpha = noise_gate_release_alpha  # I：慢关回落系数（越大关得越慢）
        self._gate_close_ratio = noise_gate_close_ratio      # 回滞：包络低于阈值×此值才关门
        self._muted = False  # 麦克风静音标志（execute_shell 期间置真）

        if device_name is not None:
            idx = find_device_by_name(self.p, device_name, is_input=True)
            if idx is not None:
                device_index = idx
            else:
                # PyAudio 找不到，尝试用 arecord + dsnoop 直接从 ALSA 采集
                alsa_info = _lookup_alsa_dsnoop_device(device_name)
                if alsa_info:
                    card_num, dev_num, rate, alsa_dev = alsa_info
                    self._arecord_device = alsa_dev
                    self.device_index = -1
                    self.device_name = f"ALSA {alsa_dev}"
                    self.device_rate = rate
                    self.device_channels = 1
                    ratio = self.device_rate / self.TARGET_RATE
                    self.chunk_size = int(chunk_size * ratio)
                    logger.info(
                        f"录音设备: {self.device_name} (原生 {self.device_rate} Hz → 输出 {self.TARGET_RATE} Hz, arecord 子进程)"
                    )
                    return
                else:
                    logger.warning(f"找不到麦克风设备 (关键词: {device_name})，使用默认设备")
                    device_name = None

        if device_index is not None:
            info = self.p.get_device_info_by_index(device_index)
            self.device_index = device_index
            self.device_name = info['name']
            self.device_rate = int(info['defaultSampleRate'])
            self.device_channels = info['maxInputChannels']
        else:
            default_info = self.p.get_default_input_device_info()
            self.device_index = default_info['index']
            self.device_name = default_info['name']
            self.device_rate = int(default_info['defaultSampleRate'])
            self.device_channels = default_info['maxInputChannels']

        self.capture_channels = min(1, self.device_channels)
        if self.capture_channels < 1:
            raise ValueError(f"设备 {self.device_name} 不支持输入 (channels={self.device_channels})")
        ratio = self.device_rate / self.TARGET_RATE
        self.chunk_size = int(chunk_size * ratio)
        logger.info(f"录音设备: {self.device_name} (索引 {self.device_index}, 原生 {self.device_rate} Hz → 输出 {self.TARGET_RATE} Hz)")

    def start(self, audio_queue):
        """启动录音线程。

        Args:
            audio_queue: 音频队列（CPM/ASR/打断检测/observe_environment 共用）
        """
        self.audio_queue = audio_queue
        self.is_running.set()
        if self._arecord_device is not None:
            self.thread = threading.Thread(target=self._record_loop_arecord, daemon=True)
        else:
            self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()
        logger.info(f"音频采集已启动 (chunk_size {self.chunk_size})")
        return True

    def stop(self):
        """停止录音。"""
        self.is_running.clear()
        if self._arecord_proc is not None:
            try:
                self._arecord_proc.terminate()
                self._arecord_proc.wait(timeout=2)
            except Exception:
                pass
        if hasattr(self, 'thread'):
            self.thread.join(timeout=2)
        self.p.terminate()

    @staticmethod
    def _log_rms(audio: np.ndarray) -> None:
        """打印单帧语音的 RMS 音量（线性 + dBFS），仅 debug 模式可见。"""
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        dbfs = 20.0 * np.log10(max(rms, 1e-8))
        logger.debug("[audio_capture] 采集帧 RMS=%.5f (%.1f dBFS)", rms, dbfs)

    def _apply_gate(self, audio: np.ndarray) -> np.ndarray:
        """远讲抑制门控（P + I + 可选 D）。

        - 开门：瞬时 rms >= 阈值（P，零延迟保字头）；可选 rms 陡升预开（D）。
        - 关门：泄漏 EMA 包络低于阈值×close_ratio 才关（I，慢关防单帧误关/远讲漏进）。
        - 输出在 chunk 内从上一帧增益线性渐变到目标增益，杜绝硬切爆音，不丢帧、时间轴连续。
        """
        if not self._gate_enabled:
            return audio

        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))

        # I：快升慢降的泄漏 EMA 包络（仅用于关门判据）
        if rms > self._rms_env:
            self._rms_env = rms
        else:
            self._rms_env = (self._gate_release_alpha * self._rms_env
                             + (1.0 - self._gate_release_alpha) * rms)

        if self._gate_open:
            if self._rms_env < self._gate_threshold * self._gate_close_ratio:
                self._gate_open = False
        else:
            onset = (self._gate_onset_delta > 0
                     and (rms - self._rms_prev) >= self._gate_onset_delta)
            if onset or rms >= self._gate_threshold:
                self._gate_open = True
        self._rms_prev = rms

        target = 1.0 if self._gate_open else 0.0
        n = audio.shape[0]
        ramp = np.linspace(self._gate_gain, target, n, dtype=np.float32) if n > 0 \
            else np.array([target], dtype=np.float32)
        self._gate_gain = target
        return audio.astype(np.float32) * ramp

    def set_muted(self, muted: bool):
        """静音/恢复麦克风采集。静音时采集线程写入静音帧（时间轴保持连续）。"""
        self._muted = bool(muted)
        logger.info(f"[audio_capture] 麦克风{'静音' if self._muted else '恢复'}")

    def _record_loop_arecord(self):
        """使用 arecord 子进程从 ALSA 硬件直接采集音频。"""
        need_resample = (self.device_rate != self.TARGET_RATE)

        self._arecord_proc = subprocess.Popen(
            [
                "arecord",
                "-D", self._arecord_device,
                "-f", "S16_LE",
                "-c", "1",
                "-r", str(self.device_rate),
                "--buffer-time=80000",  # 80ms buffer
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        bytes_per_chunk = self.chunk_size * 2  # S16_LE = 2 bytes per sample
        try:
            while self.is_running.is_set():
                data = self._arecord_proc.stdout.read(bytes_per_chunk)
                if not data:
                    break
                if self.audio_queue is not None and not self.audio_queue.full():
                    audio_np = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    if need_resample:
                        audio_np = resample_poly(audio_np, self.TARGET_RATE, self.device_rate)
                    if self._muted:
                        audio_np = np.zeros_like(audio_np)
                    self._log_rms(audio_np)
                    audio_np = self._apply_gate(audio_np)
                    self.audio_queue.put(audio_np)
        except Exception as e:
            if self.is_running.is_set():
                logger.error(f"arecord 录音异常: {e}")
        finally:
            if self._arecord_proc is not None:
                try:
                    self._arecord_proc.terminate()
                    self._arecord_proc.wait(timeout=2)
                except Exception:
                    pass

    def _record_loop(self):
        need_resample = (self.device_rate != self.TARGET_RATE)

        try:
            stream = self.p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.device_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.chunk_size,
            )
        except OSError as e:
            logger.warning(
                f"无法以 {self.device_rate} Hz 打开设备，回退到 {self.TARGET_RATE} Hz: {e}"
            )
            self.device_rate = self.TARGET_RATE
            self.chunk_size = 1600
            need_resample = False
            stream = self.p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.device_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.chunk_size,
            )

        try:
            while self.is_running.is_set():
                try:
                    data = stream.read(self.chunk_size, exception_on_overflow=False)
                    if self.audio_queue is not None and not self.audio_queue.full():
                        audio_np = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                        if need_resample:
                            audio_np = resample_poly(audio_np, self.TARGET_RATE, self.device_rate)
                        if self._muted:
                            audio_np = np.zeros_like(audio_np)
                        self._log_rms(audio_np)
                        audio_np = self._apply_gate(audio_np)
                        self.audio_queue.put(audio_np)
                    else:
                        logger.warning("音频队列满，丢弃帧")
                except Exception as e:
                    if self.is_running.is_set():
                        logger.error(f"录音读取异常: {e}")
                    break
        finally:
            stream.stop_stream()
            stream.close()