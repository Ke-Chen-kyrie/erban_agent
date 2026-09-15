"""调用身份识别服务并在 BGR 图像上绘制人脸身份标注。"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ident_client import IdentificationClient


_FONT_PATHS = (
    "/usr/share/fonts/opentype/source-han-cjk/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)
# 标签字号：环境变量可覆盖；默认随人脸框高度自适应（框高/3）
FONT_SIZE_ENV = "ANNOTATION_FONT_SIZE"
_MIN_FONT_SIZE = 16
_MAX_FONT_SIZE = 90


def _load_font(size: int):
    custom = os.getenv("ANNOTATION_FONT", "")
    for candidate in ((custom,) if custom else ()) + _FONT_PATHS:
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _font_for(size: int):
    # 同字号复用字体对象，避免每帧重复加载
    cache = getattr(_font_for, "_cache", None)
    if cache is None:
        cache = _font_for._cache = {}
    font = cache.get(size)
    if font is None:
        font = cache[size] = _load_font(size)
    return font


class FaceAnnotator:
    """人脸识别与标注器；一个实例复用同一个 HTTP 连接。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        threshold: float = 0.7,
        max_face_num: int = 10,
    ) -> None:
        self._client = IdentificationClient(base_url, timeout)
        self._threshold = threshold
        self._max_face_num = max_face_num
        self._font_size = int(os.getenv(FONT_SIZE_ENV, "0")) or 0

    def detect(self, bgr_frame: np.ndarray) -> list[dict]:
        ok, encoded = cv2.imencode(
            ".jpg", bgr_frame, [cv2.IMWRITE_JPEG_QUALITY, 92]
        )
        if not ok:
            raise ValueError("识别输入帧 JPEG 编码失败")
        result = self._client.face_detect_bytes(
            encoded.tobytes(), max_face_num=self._max_face_num
        )
        faces = result.get("faces", [])
        normalized: list[dict] = []
        for original in faces:
            face = dict(original)
            score = float(face.get("score") or 0.0)
            face["matched"] = bool(face.get("matched")) and score >= self._threshold
            if not face["matched"]:
                face["name"] = ""
            normalized.append(face)
        return normalized

    def draw(self, bgr_frame: np.ndarray, faces: list[dict]) -> np.ndarray:
        if not faces:
            return bgr_frame

        annotated = bgr_frame.copy()
        height, width = annotated.shape[:2]
        labels: list[tuple[int, int, str, tuple[int, int, int], int]] = []

        for face in faces:
            location = face.get("location") or {}
            try:
                x = max(0, min(width - 1, int(location["x"])))
                y = max(0, min(height - 1, int(location["y"])))
                w = max(1, int(location["width"]))
                h = max(1, int(location["height"]))
            except (KeyError, TypeError, ValueError):
                continue

            x2, y2 = min(width - 1, x + w), min(height - 1, y + h)
            matched = bool(face.get("matched"))
            bgr_color = (0, 220, 0) if matched else (0, 0, 255)
            rgb_color = (0, 220, 0) if matched else (255, 0, 0)
            cv2.rectangle(annotated, (x, y), (x2, y2), bgr_color, max(2, h // 40))

            name = str(face.get("name") or "").strip()
            label = name if matched and name else "路人"
            font_size = self._font_size or min(
                _MAX_FONT_SIZE, max(_MIN_FONT_SIZE, h // 3)
            )
            labels.append((x, max(0, y - font_size - 8), label, rgb_color, font_size))

        if not labels:
            return annotated

        rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        drawer = ImageDraw.Draw(image)
        for x, y, label, color, font_size in labels:
            font = _font_for(font_size)
            box = drawer.textbbox((x, y), label, font=font, stroke_width=2)
            # 半透明底色，避免浅色背景下看不清文字
            pad = font_size // 8
            bg_box = (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
            drawer.rectangle(bg_box, fill=(30, 30, 30))
            drawer.text((x, y), label, font=font, fill=color,
                        stroke_width=2, stroke_fill=(0, 0, 0))
        return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)

    def annotate(self, bgr_frame: np.ndarray) -> tuple[np.ndarray, list[dict]]:
        faces = self.detect(bgr_frame)
        return self.draw(bgr_frame, faces), faces

    def close(self) -> None:
        self._client.close()
