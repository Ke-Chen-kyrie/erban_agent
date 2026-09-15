from .audio_capture import AudioCapture
from .audio_output import AudioPlayer, find_sd_device_by_name

__all__ = [
    "AudioCapture",
    "AudioPlayer",
    "find_sd_device_by_name",
    "AudioPlaybackManager",
]


def __getattr__(name: str):
    """Lazy import for AudioPlaybackManager to avoid circular import with agent.utils."""
    if name == "AudioPlaybackManager":
        from .playback_manager import AudioPlaybackManager
        return AudioPlaybackManager
    raise AttributeError(f"module 'audio' has no attribute {name!r}")
