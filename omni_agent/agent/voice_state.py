"""音色状态管理 —— 当前音色及其 getter/setter。"""

from config import OMNI_VOICE, _VALID_OMNI_VOICES

_VALID_VOICES = list(_VALID_OMNI_VOICES)

_current_voice = OMNI_VOICE


def get_current_voice() -> str:
    return _current_voice


def _set_current_voice(voice: str):
    global _current_voice
    _current_voice = voice