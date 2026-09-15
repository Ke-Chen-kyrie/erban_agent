"""``user_identification`` 人脸检测接口的轻量客户端。"""

from __future__ import annotations

from typing import Any

import httpx


class IdentificationClient:
    """复用 HTTP 连接调用 ``POST /api/face/detect``。"""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, trust_env=False)

    def face_detect_bytes(
        self,
        image_bytes: bytes,
        *,
        filename: str = "frame.jpg",
        max_face_num: int = 10,
    ) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/api/face/detect",
            data={"max_face_num": str(max_face_num)},
            files={"image": (filename, image_bytes, "image/jpeg")},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("faces", []), list):
            raise ValueError("人脸识别服务返回格式不正确")
        return result

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
