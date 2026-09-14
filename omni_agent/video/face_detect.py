"""人脸检测绘制 —— 在图像上绘制检测框和身份信息。"""

import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import FACE_RECOGNITION_THRESHOLD
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


def _load_chinese_font(size: int = 20) -> ImageFont.FreeTypeFont | None:
    for path in _CHINESE_FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


def detect_faces(frame: np.ndarray) -> list[dict]:
    """对单帧运行人脸检测，返回人脸列表（已应用阈值过滤）。
    每个 face 包含: matched, name, user_id, score, location, mouth。
    """
    try:
        from agent.utils import get_ident_client
        client = get_ident_client()
        if client is None:
            return []

        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        result = client.face_detect_bytes(buf.tobytes(), filename="frame.jpg")
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


def _draw_face_detect(frame: np.ndarray) -> np.ndarray:
    """对帧运行人脸检测，在图像上绘制检测框和身份信息，返回标注后的帧。"""
    try:
        faces = detect_faces(frame)
        if not faces:
            return frame

        annotated = frame.copy()
        font = _load_chinese_font(20)
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
            logger.info(f"[face-detect] face: matched={matched}, user_id={f.get('user_id')}, name={f.get('name')}, score={f.get('score', 'N/A')}, label={label}")
            if font is not None:
                draw.text((x, y - 22), label, font=font, fill=color_rgb)
            else:
                draw.text((x, y - 8), label, fill=color_rgb)

        annotated = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        logger.info(f"[face-detect] 检测到 {len(faces)} 张人脸，已标注到最后一帧")
        return annotated
    except Exception as e:
        logger.warning(f"[face-detect] 人脸检测失败: {e}")
        return frame