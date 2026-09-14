#!/usr/bin/env python3
"""工具模块内部使用的辅助函数 & 跨模块共享状态"""

import re
import threading

import sounddevice as sd
from audio.audio_output import find_sd_device_by_name
from config import (
    SPEAKER_DEVICE_NAME, SPEAKER_DEVICE_INDEX,
)
from logging_config import get_logger

logger = get_logger(__name__)

_tts_fallback = threading.Event()


def get_tts_fallback() -> bool:
    return _tts_fallback.is_set()


def _looks_like_error(output: str) -> bool:
    """检测输出是否包含 Python Traceback 或明显的错误标记。"""
    if not output:
        return False
    if "Traceback (most recent call last):" in output:
        return True
    error_markers = [
        "Error:",
        "Exception:",
        "exit code: 1",
        "FATAL:",
        "panic:",
        "SIGSEGV",
        "SIGABRT",
    ]
    for marker in error_markers:
        if marker in output:
            return True
    return False


# ---------- 音量控制（声卡查找）----------

def _get_tts_sound_card() -> tuple[int, str]:
    """用与 TTS 相同的逻辑查找输出设备对应的 (ALSA 声卡号, 混音器控制名)"""
    device_index = SPEAKER_DEVICE_INDEX
    device_name = SPEAKER_DEVICE_NAME
    is_yunji = False

    try:
        if device_name is not None:
            idx = find_sd_device_by_name(device_name, kind='output')
            if idx is not None:
                device_index = idx
                is_yunji = "yunji" in device_name.lower()
            else:
                logger.warning(f"音量控制: 找不到扬声器设备 (关键词: {device_name})，使用默认设备")

        if device_index is not None:
            info = sd.query_devices(device_index)
        else:
            info = sd.query_devices(kind='output')

        name = info['name']
        match = re.search(r'hw:(\d+)', name)
        if match:
            card = int(match.group(1))
            control = "PCM" if is_yunji else "Master"
            return card, control
    except Exception as e:
        logger.warning(f"音量控制: 解析 TTS 设备失败, {e}")

    return 1, "Master"


_card_control = None


def reset_audio_card_cache():
    """重置声卡缓存，供设备热插拔或 player 重建时调用。"""
    global _card_control
    _card_control = None


def _get_card_control() -> tuple[int, str]:
    """获取 TTS 对应的 (声卡号, 混音器控制名)（缓存）"""
    global _card_control
    if _card_control is None:
        _card_control = _get_tts_sound_card()
        card, control = _card_control
        logger.info(f"音量控制: 声卡 {card}, 混音器 {control}")
    return _card_control


# ---------- 音频采集队列 ----------

_audio_queue = None


def set_audio_queue(q):
    global _audio_queue
    _audio_queue = q


# ---------- 麦克风静音（execute_shell 期间关闭采集） ----------

_audio_capture = None


def set_audio_capture(capture):
    global _audio_capture
    _audio_capture = capture


def set_mic_muted(muted: bool):
    """静音/恢复麦克风采集（execute_shell 执行期间屏蔽机器人自身噪声）。"""
    if _audio_capture is not None:
        _audio_capture.set_muted(muted)


# ---------- 睡眠模式信号（go_sleep 工具设置，main.py 消费）----------

_sleep_requested = threading.Event()


def request_sleep_mode():
    _sleep_requested.set()


def clear_sleep_request():
    _sleep_requested.clear()