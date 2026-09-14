"""打断状态 —— 跨线程共享的打断检测信号。

纯关键词打断（INTERRUPT_MODE == "keyword"）下，需要 KWS 线程和
RealtimeSession 协程共享两个状态：

- ``_ai_speaking``：AI 是否正在讲话，决定麦克风音频队列归属
  （True → KWS 独占听打断词；False → feeder 喂给 Qwen）。
- ``_interrupt_requested``：打断词已命中（一次性触发信号）。
"""
import threading

_ai_speaking = threading.Event()
_interrupt_requested = threading.Event()


def set_ai_speaking(v: bool) -> None:
    """由 RealtimeSession._consume() 每 tick 刷新，标记 AI 是否正在讲话。"""
    if v:
        _ai_speaking.set()
    else:
        _ai_speaking.clear()


def is_ai_speaking() -> bool:
    """KWS 线程 / feeder 门控读取：AI 是否正在讲话。"""
    return _ai_speaking.is_set()


def reset() -> None:
    """会话开始时调用，清掉上一会话遗留的信号。"""
    _ai_speaking.clear()
    _interrupt_requested.clear()


def interrupt_event() -> threading.Event:
    """返回打断触发 Event，供 WakeupDetector 直接 set()。"""
    return _interrupt_requested


def consume_interrupt() -> bool:
    """RealtimeSession._consume() 每 tick 消费：返回是否被打断并清位。"""
    was = _interrupt_requested.is_set()
    _interrupt_requested.clear()
    return was