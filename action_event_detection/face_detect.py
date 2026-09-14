"""人脸检测绘制 —— 在图像上绘制检测框和身份信息。"""

import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import IDENT_API_BASE, FACE_RECOGNITION_THRESHOLD
from logging_config import get_logger

logger = get_logger(__name__)

_CHINESE_FONT_PATHS = [
    "/usr/share/fonts/opentype/source-han-cjk/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/opentype/source-han-cjk/SourceHanMono.ttc",
    "/usr/share/fonts/fonts-gb/GB_ST_GB18030.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

_ident_client = None


def _get_ident_client():
    global _ident_client
    if _ident_client is None:
        try:
            from ident_client import IdentificationClient
            _ident_client = IdentificationClient(base_url=IDENT_API_BASE)
        except ImportError:
            logger.warning("[face-detect] ident_client not available, face detection disabled")
            return None
    return _ident_client


def _load_chinese_font(size: int = 20) -> ImageFont.FreeTypeFont | None:
    for path in _CHINESE_FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


async def detect_faces(frame: np.ndarray) -> list[dict]:
    """对单帧运行人脸检测，返回人脸列表（已应用阈值过滤）。
    每个 face 包含: matched, name, user_id, score, location, mouth。
    """
    try:
        client = _get_ident_client()
        if client is None:
            return []

        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        result = await client.face_detect_bytes_async(buf.tobytes(), filename="frame.jpg")
        faces = result.get("faces", [])

        threshold = FACE_RECOGNITION_THRESHOLD
        for f in faces:
            if f.get("score", 0) < threshold:
                f["matched"] = False
                f["name"] = None
                f["user_id"] = None

        return faces
    except Exception as e:
        logger.warning(f"[face-detect] 人脸检测失败: {e}")
        return []


def draw_face_detect(frame: np.ndarray, faces: list[dict] | None = None) -> np.ndarray:
    """对帧运行人脸检测，在图像上绘制检测框和身份信息，返回标注后的 BGR 帧。"""
    try:
        if faces is None:
            faces = detect_faces(frame)
        if not faces:
            return frame

        annotated = frame.copy()
        font = _load_chinese_font(36)
        for f in faces:
            loc = f.get("location")
            if not loc:
                continue
            x, y, w, h = loc["x"], loc["y"], loc["width"], loc["height"]
            matched = f.get("matched", False)
            color = (0, 255, 0) if matched else (0, 0, 255)
            cv2.rectangle(annotated, (x, y), (x + w, y + h), color, 2)

            mouth = f.get("mouth", [])
            for p in mouth:
                px, py = int(p["x"]), int(p["y"])
                cv2.circle(annotated, (px, py), 2, (255, 0, 0), -1)

        annotated_rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(annotated_rgb)
        draw = ImageDraw.Draw(pil_img)
        for f in faces:
            loc = f.get("location")
            if not loc:
                continue
            x, y = loc["x"], loc["y"]
            matched = f.get("matched", False)
            color_rgb = (0, 255, 0) if matched else (255, 0, 0)
            label = (f.get("name") or "路人") if matched else "路人"
            logger.debug(
                "[face-detect] face: matched=%s, user_id=%s, name=%s, score=%s, label=%s",
                matched, f.get("user_id"), f.get("name"), f.get("score", "N/A"), label,
            )
            if font is not None:
                draw.text((x, y - 48), label, font=font, fill=color_rgb)
            else:
                draw.text((x, y - 8), label, fill=color_rgb)

        annotated = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        logger.debug("[face-detect] 检测到 %d 张人脸，已标注到帧", len(faces))
        return annotated
    except Exception as e:
        logger.warning(f"[face-detect] 人脸检测失败: {e}")
        return frame