"""Validated HTTP contract types."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .config import MonitorConfig


_EVENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RequestValidationError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class MonitorRequest:
    event_name: str
    prompt: str
    timeout_seconds: float

    @classmethod
    def parse(cls, payload: Any, config: MonitorConfig) -> "MonitorRequest":
        if not isinstance(payload, dict):
            raise RequestValidationError("body", "request body must be a JSON object")

        event_name = payload.get("event_name")
        if not isinstance(event_name, str) or not _EVENT_NAME_RE.fullmatch(event_name):
            raise RequestValidationError(
                "event_name",
                "event_name must contain only letters, digits, underscores, or hyphens",
            )

        if "prompt" in payload:
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise RequestValidationError("prompt", "prompt must be a non-empty string")
            if len(prompt) > config.max_prompt_length:
                raise RequestValidationError(
                    "prompt", f"prompt must not exceed {config.max_prompt_length} characters"
                )
        else:
            instruction = payload.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                raise RequestValidationError(
                    "instruction", "instruction must be a non-empty string"
                )
            if len(instruction) > config.max_prompt_length:
                raise RequestValidationError(
                    "instruction",
                    "instruction must not exceed "
                    f"{config.max_prompt_length} characters",
                )
            prompt = instruction

        raw_timeout = payload.get("timeout_seconds")
        if isinstance(raw_timeout, bool):
            raise RequestValidationError("timeout_seconds", "timeout_seconds must be a number")
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            raise RequestValidationError(
                "timeout_seconds", "timeout_seconds must be a number"
            ) from None
        if not math.isfinite(timeout_seconds) or not (
            config.min_timeout_seconds <= timeout_seconds <= config.max_timeout_seconds
        ):
            raise RequestValidationError(
                "timeout_seconds",
                "timeout_seconds must be between "
                f"{config.min_timeout_seconds:g} and {config.max_timeout_seconds:g}",
            )

        return cls(event_name=event_name, prompt=prompt, timeout_seconds=timeout_seconds)


@dataclass(frozen=True)
class DetectedEvent:
    event_name: str
    description: str
    name: str
