"""Append-only, per-run audit records for the dashboard.

The audit deliberately stores text metadata only. Live frames and embedded
image payloads stay in memory and are never written to disk.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


_DROP = object()
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MEDIA_KEYS = {"image", "images", "frame", "frames", "video", "videos"}
_MEDIA_SUFFIXES = {
    ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".m4v", ".mkv",
    ".mov", ".mp4", ".png", ".webm", ".webp",
}


def _looks_like_embedded_base64(value: str) -> bool:
    compact = "".join(value.split())
    return len(compact) >= 256 and len(compact) % 4 == 0 and bool(_BASE64_RE.fullmatch(compact))


def _sanitize_media(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered.startswith("data:"):
            return _DROP
        is_reference = (
            lowered.startswith(("http://", "https://", "file://"))
            or stripped.startswith(("/", "./", "../"))
            or "/" in stripped
            or "\\" in stripped
            or Path(stripped).suffix.lower() in _MEDIA_SUFFIXES
        )
        return value if is_reference else _DROP
    if isinstance(value, Mapping):
        return _sanitize(value)
    if isinstance(value, (list, tuple, set)):
        cleaned_items = []
        for item in value:
            safe = _sanitize_media(item)
            if safe is not _DROP:
                cleaned_items.append(safe)
        return cleaned_items
    return _DROP


def _sanitize(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _DROP
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower().startswith("data:image/") or _looks_like_embedded_base64(stripped):
            return _DROP
        return value
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            safe = _sanitize_media(item) if str(key).lower() in _MEDIA_KEYS else _sanitize(item)
            if safe is not _DROP:
                cleaned[str(key)] = safe
        return cleaned
    if isinstance(value, (list, tuple, set)):
        cleaned_items = []
        for item in value:
            safe = _sanitize(item)
            if safe is not _DROP:
                cleaned_items.append(safe)
        return cleaned_items
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def sanitize_payload(value: Any) -> Any:
    """Return a JSON-safe value with embedded binary/image data removed."""
    cleaned = _sanitize(value)
    return None if cleaned is _DROP else cleaned


class SessionAuditLog:
    """A fresh append-only JSONL audit file for one dashboard process run."""

    def __init__(self, data_dir: str | Path) -> None:
        directory = Path(data_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        while True:
            path = directory / f"dashboard_{timestamp}_{uuid.uuid4().hex[:8]}.jsonl"
            try:
                path.touch(exist_ok=False)
            except FileExistsError:
                continue
            self.path = path
            break

    def append(self, record_type: str, payload: Mapping[str, Any] | None = None) -> None:
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "type": str(record_type),
            "payload": sanitize_payload(dict(payload or {})),
        }
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            output.write("\n")
            output.flush()
