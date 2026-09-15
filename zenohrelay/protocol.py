"""Zenoh 标注帧的线协议。

默认 payload 是标准 JPEG 字节。开启 metadata 时才使用：
``MAGIC + 4字节JSON长度 + JSON + JPEG``。客户端会自动识别两种格式。
"""

from __future__ import annotations

import json
import struct
from typing import Any


MAGIC = b"ZRM1"
_HEADER = struct.Struct(">4sI")


def encode_frame(jpeg: bytes, metadata: dict[str, Any] | None = None) -> bytes:
    if not metadata:
        return jpeg
    meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _HEADER.pack(MAGIC, len(meta)) + meta + jpeg


def decode_frame(payload: bytes) -> tuple[bytes, dict[str, Any] | None]:
    if len(payload) < _HEADER.size or not payload.startswith(MAGIC):
        return payload, None
    magic, length = _HEADER.unpack_from(payload)
    end = _HEADER.size + length
    if magic != MAGIC or end > len(payload):
        raise ValueError("无效的 ZenohRelay metadata payload")
    metadata = json.loads(payload[_HEADER.size:end].decode("utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("ZenohRelay metadata 必须是 JSON object")
    return payload[end:], metadata
