"""语音打断检测器 —— 简化能量 VAD + 滑动窗口，在 agent 说话期间检测用户是否插话。

策略（类似豆包语音通话）：
- 每帧直接判断 mic_rms 是否超过固定阈值（不做 EMA 平滑，不学基线）
- 滑动窗口投票：最近 N 帧中有 M 帧判为"有人声"即触发打断
- 响应快、无校准期、参数少
"""

import numpy as np
from collections import deque
from logging_config import get_logger

logger = get_logger(__name__)


class VoiceInterruptDetector:
    """简化能量 VAD + 滑动窗口投票。

    不做回声消除，利用近场效应：用户嘴离麦克风近，语音能量远大于 TTS 回声。
    每帧独立判断，滑动窗口汇总，避免 EMA 延迟和校准期。
    """

    def __init__(
        self,
        mic_sample_rate: int = 16000,
        energy_threshold: float = 0.02,
        window_size: int = 12,
        min_speech_frames: int = 2,
        frame_ms: float = 80.0,
    ):
        self.mic_rate = mic_sample_rate
        self.energy_threshold = energy_threshold
        self.window_size = window_size
        self.min_speech_frames = min_speech_frames
        self.frame_samples = int(mic_sample_rate * frame_ms / 1000)
        self._debug_frame = 0

        self._window: deque[bool] = deque(maxlen=window_size)

    # ---------- 公开接口 ----------

    def feed_reference(self, pcm_bytes: bytes):
        """保留接口兼容性，不再使用参考信号。"""
        pass

    def ref_ready(self) -> bool:
        """保留接口兼容性。"""
        return True

    def reset(self):
        self._window.clear()
        self._debug_frame = 0

    def process_mic(self, mic_chunk: np.ndarray) -> bool:
        """处理一帧麦克风音频，返回是否检测到打断。"""
        self._debug_frame += 1

        if len(mic_chunk) < self.frame_samples:
            return False

        mic_frame = np.asarray(mic_chunk[-self.frame_samples:], dtype=np.float32)
        mic_rms = float(np.sqrt(np.mean(mic_frame ** 2)))

        is_speech = mic_rms > self.energy_threshold
        self._window.append(is_speech)

        speech_count = sum(self._window)
        triggered = len(self._window) >= self.min_speech_frames and speech_count >= self.min_speech_frames

        import logging

        if logger.isEnabledFor(logging.DEBUG) and (self._debug_frame % 10 == 1 or is_speech):
            status = "!!TRIG!!" if triggered else ("↑" if is_speech else " ")
            logger.debug(
                f"[打断DEBUG] 帧{self._debug_frame}: "
                f"mic={mic_rms:.4f} speech={is_speech} "
                f"window={speech_count}/{len(self._window)} "
                f"need={self.min_speech_frames}/{self.window_size} {status}"
            )

        return triggered