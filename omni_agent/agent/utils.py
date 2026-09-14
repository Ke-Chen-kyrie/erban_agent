#!/usr/bin/env python3
"""工具模块内部使用的辅助函数 & 跨模块共享状态"""

import os
import re
import threading
from typing import Any, Optional

import sounddevice as sd
from audio.audio_output import find_sd_device_by_name
from config import (
    SPEAKER_DEVICE_NAME, SPEAKER_DEVICE_INDEX,
)
from logging_config import get_logger

logger = get_logger(__name__)


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


# ---------- TTS 回退状态（TTS 初始化失败时由 main.py 设置，omni_node 读取）----------

_tts_fallback = threading.Event()


def set_tts_fallback(enabled: bool):
    if enabled:
        _tts_fallback.set()
    else:
        _tts_fallback.clear()


def get_tts_fallback() -> bool:
    return _tts_fallback.is_set()


# 由 main.py 注入
_ident_client: Optional[Any] = None


def set_ident_client(client):
    global _ident_client
    _ident_client = client


def get_ident_client():
    return _ident_client


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


# ---------- 跨模块共享状态（声纹结果，供 Agent 线程读取）----------

_latest_user_name = ""
_latest_user_role = ""
_latest_user_description = ""

_user_info_ready = threading.Event()


def set_user_info(name: str, role: str, description: str = ""):
    global _latest_user_name, _latest_user_role, _latest_user_description
    _latest_user_name = name
    _latest_user_role = role
    _latest_user_description = description
    _user_info_ready.set()


def get_user_info(timeout: float = 1.0) -> tuple[str, str, str]:
    if _user_info_ready.wait(timeout):
        return _latest_user_name, _latest_user_role, _latest_user_description
    return _latest_user_name, _latest_user_role, _latest_user_description


# ---------- 音频采集队列（供 observe_environment 等工具录制用）----------

_audio_queue = None


def set_audio_queue(q):
    global _audio_queue
    _audio_queue = q


def get_audio_queue():
    return _audio_queue


def reset_shared_state():
    """每轮对话开始前重置共享状态"""
    global _latest_user_name, _latest_user_role, _latest_user_description
    _latest_user_name = ""
    _latest_user_role = ""
    _latest_user_description = ""
    _tts_fallback.clear()
    _user_info_ready.clear()


# ---------- 睡眠模式信号（go_sleep 工具设置，main.py 消费）----------

_sleep_requested = threading.Event()


def request_sleep_mode():
    _sleep_requested.set()


def is_sleep_requested() -> bool:
    return _sleep_requested.is_set()


def clear_sleep_request():
    _sleep_requested.clear()


# ---------- 路由意图识别耗时（agent/agent.py 设置，perf_metrics.py 的 PerfMetrics 读取）----------

_router_start_time: Optional[float] = None
_router_end_time: Optional[float] = None


def set_router_start_time(t: float):
    global _router_start_time
    _router_start_time = t


def set_router_end_time(t: float):
    global _router_end_time
    _router_end_time = t


def get_router_timing() -> tuple[Optional[float], Optional[float]]:
    return _router_start_time, _router_end_time


# ---------- LLM API 调用起点（agent/agent.py 设置，perf_metrics.py 的 PerfMetrics 读取）----------

_agent_start_time: Optional[float] = None


def set_agent_start_time(t: float):
    global _agent_start_time
    _agent_start_time = t


def get_agent_start_time() -> Optional[float]:
    return _agent_start_time


# ---------- API 调用前准备耗时起点（agent/agent.py 设置，main.py 读取用于分解 LLM 首 token 延时）----------

_agent_prep_start_time: Optional[float] = None


def set_agent_prep_start_time(t: float):
    global _agent_prep_start_time
    _agent_prep_start_time = t


def get_agent_prep_start_time() -> Optional[float]:
    return _agent_prep_start_time
