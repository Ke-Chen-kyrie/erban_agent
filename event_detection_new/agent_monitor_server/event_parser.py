"""Strict dynamic-label model output parsing."""

from __future__ import annotations

import json
import re
from typing import Any

from .schemas import DetectedEvent


def parse_matching_event(text: Any, expected_event: str) -> DetectedEvent | None:
    if not isinstance(text, str):
        return None
    payload = _last_json_object(text)
    if payload is None:
        return None
    if payload.get("event") != expected_event:
        return None
    return DetectedEvent(
        event_name=expected_event,
        description=str(payload.get("desc", payload.get("description", "")) or "").strip(),
        name=str(payload.get("name", "") or "").strip(),
    )


def _last_json_object(text: str) -> dict[str, Any] | None:
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced[-1].strip()
    elif "</think>" in text:
        candidate = text.rsplit("</think>", 1)[-1].strip()
    else:
        candidate = text.strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None
