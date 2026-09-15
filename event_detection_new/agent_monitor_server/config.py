"""Environment-backed configuration for the monitor server."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MonitorConfig:
    server_host: str = "127.0.0.1"
    server_port: int = 8781
    auth_token: str = ""
    min_timeout_seconds: float = 1.0
    max_timeout_seconds: float = 3600.0
    max_prompt_length: int = 16000
    push_timeout_seconds: float = 3.0
    expand_timeout_seconds: float = 30.0
    expand_max_tokens: int = 1024
    frame_interval: float = 0.5
    max_tokens: int = 256
    temperature: float = 0.2
    disable_thinking: bool = False
    max_context_turns: int = 20
    main_api_base: str = "http://127.0.0.1:7060/v1"
    main_model: str = "streamingharness-8b"
    api_key: str = "EMPTY"
    agent_webhook_url: str = ""
    system_event_url: str = ""
    video_source: str = "foxglove"

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        return cls(
            server_host=_env("MONITOR_SERVER_HOST", "127.0.0.1"),
            server_port=_env_int("MONITOR_SERVER_PORT", 8781),
            auth_token=_env("MONITOR_AUTH_TOKEN", ""),
            min_timeout_seconds=_env_float("MONITOR_MIN_TIMEOUT_SECONDS", 1),
            max_timeout_seconds=_env_float("MONITOR_MAX_TIMEOUT_SECONDS", 3600),
            max_prompt_length=_env_int("MONITOR_MAX_PROMPT_LENGTH", 16000),
            push_timeout_seconds=_env_float("MONITOR_PUSH_TIMEOUT_SECONDS", 3),
            expand_timeout_seconds=_env_float("MONITOR_EXPAND_TIMEOUT_SECONDS", 30),
            expand_max_tokens=_env_int("MONITOR_EXPAND_MAX_TOKENS", 1024),
            frame_interval=_env_float("MONITOR_FRAME_INTERVAL", 0.5),
            max_tokens=_env_int("MONITOR_MAX_TOKENS", 256),
            temperature=_env_float("MONITOR_TEMPERATURE", 0.2),
            disable_thinking=_env_bool(
                "MONITOR_DISABLE_THINKING",
                _env_bool("MAIN_DISABLE_THINKING", False),
            ),
            max_context_turns=_env_int("MONITOR_MAX_CONTEXT_TURNS", 20),
            main_api_base=_env("MONITOR_MAIN_API_BASE") or _env(
                "MAIN_API_BASE", "http://127.0.0.1:7060/v1"
            ),
            main_model=_env("MONITOR_MAIN_MODEL") or _env(
                "MAIN_MODEL", "streamingharness-8b"
            ),
            api_key=_env("MODEL_API_KEY", "EMPTY"),
            agent_webhook_url=_env("AGENT_WEBHOOK_URL", ""),
            system_event_url=_env("SYSTEM_EVENT_URL", ""),
            video_source=_env("VIDEO_SOURCE", "foxglove").strip().lower(),
        )
