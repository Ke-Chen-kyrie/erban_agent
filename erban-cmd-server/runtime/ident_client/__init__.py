"""人脸识别客户端库 (ident 用).

IDENTIFICATION_URL / CAMERA_WS_URL / CAMERA_TOPIC 读 /config/config.yaml 的 ident 段
(CMD_CONFIG 可覆盖根), 由本库自读, server 不解析.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import yaml


def _config_root() -> Path:
    return Path(os.environ.get("CMD_CONFIG", "/config"))


def _ident_section() -> dict:
    data = yaml.safe_load((_config_root() / "config.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    return data.get("ident", {}) or {}


def identification_url() -> str:
    return str(_ident_section().get("identification_url", "http://192.168.217.100:8001"))


def camera_ws_url() -> str:
    return str(_ident_section().get("camera_ws_url", ""))


def camera_topic() -> str:
    return str(_ident_section().get("camera_topic", ""))


def joint_url() -> str:
    return str(os.environ.get("JOINT_SERVICE_URL", "")) or str(
        _ident_section().get("joint_url", "http://192.168.217.100:8002")
    )


def focus_joints(body_parts: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """POST 关节聚焦: {"部位": 1} → {"status": int, "report": str}.

    返回 (退出码, 人类可读报告). 24 SKEL 关节 / 支持 "整体".
    """
    if not body_parts:
        return 1, "❌ 请指定至少一个身体部位"
    payload = {part: 1 for part in body_parts}
    try:
        resp = httpx.post(
            f"{joint_url()}/joint",
            json=payload,
            timeout=timeout,
        )
    except Exception as e:
        return (1, f"⚠️ 关节聚焦服务连接异常: {e}")
    if resp.status_code != 200:
        return 1, f"⚠️ 关节聚焦服务暂不可用 (Status: {resp.status_code})"
    data = resp.json()
    return 0, f"✅ 已聚焦身体部位: {'、'.join(body_parts)}。返回数据: {json.dumps(data, ensure_ascii=False)}"


def verify_face(user_id: str, image_path: str | Path) -> dict[str, Any]:
    """POST 人脸验证: multipart user_id + image, 返回 {"match": bool, "score": float}."""
    image_path = Path(image_path)
    with image_path.open("rb") as f:
        resp = httpx.post(
            f"{identification_url()}/api/face/verify",
            data={"user_id": user_id},
            files={"image": (image_path.name, f, "image/jpeg")},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
