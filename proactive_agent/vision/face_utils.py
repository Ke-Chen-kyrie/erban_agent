"""人脸检测 + 标注，通过 identity 服务 API。"""

import base64
import os
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vision.identity_client import IdentificationClient
from logging_config import get_logger

logger = get_logger(__name__)

# 框颜色: 绿框=已注册，红框=未知
_GREEN = (0, 255, 0)
_RED = (0, 0, 255)

_CHINESE_FONT_PATHS = [
    "/usr/share/fonts/opentype/source-han-cjk/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/opentype/source-han-cjk/SourceHanMono.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _get_font(size: int = 20) -> ImageFont.FreeTypeFont:
    for path in _CHINESE_FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except (OSError, IOError):
        return ImageFont.load_default()


def detect_and_draw_sync(
    client: IdentificationClient,
    frame_bgr: np.ndarray,
    max_faces: int = 10,
) -> Optional[str]:
    """同步版本：对一帧图像做人脸检测 + 标注，返回 base64 JPEG。"""
    if frame_bgr is None:
        return None

    def _encode(img: np.ndarray) -> str:
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return base64.b64encode(buf).decode()

    # 编码为 JPEG 字节
    _, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
    img_bytes = buf.tobytes()

    # 调用人脸检测 API
    try:
        result = client.face_detect_bytes(img_bytes, max_face_num=max_faces)
    except Exception as e:
        logger.warning("face detect API error: %s", e)
        return _encode(frame_bgr)

    faces = result.get("faces") or result.get("data") or []
    if not faces:
        return _encode(frame_bgr)

    logger.info("detect_and_draw: %d faces found", len(faces))

    annotated = frame_bgr.copy()
    for f in faces:
        loc = f.get("location")
        if not loc:
            continue
        x, y, w, h = loc["x"], loc["y"], loc["width"], loc["height"]
        matched = f.get("matched", bool(f.get("user_id")))
        color = _GREEN if matched else _RED
        cv2.rectangle(annotated, (x, y), (x + w, y + h), color, 2)

    # 转 PIL 写中文标签
    img_rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(img_rgb)
    draw = ImageDraw.Draw(pil_img)
    font = _get_font(36)

    for f in faces:
        loc = f.get("location")
        if not loc:
            continue
        x, y = loc["x"], loc["y"]
        matched = f.get("matched", bool(f.get("user_id")))
        color_rgb = (0, 255, 0) if matched else (255, 0, 0)
        label = (f.get("name") or "路人") if matched else "路人"
        if font is not None:
            draw.text((x, y - 48), label, font=font, fill=color_rgb)
        else:
            draw.text((x, y - 8), label, fill=color_rgb)

    img_bgr_annotated = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    return _encode(img_bgr_annotated)


async def detect_and_draw(
    client: IdentificationClient,
    frame_bgr: np.ndarray,
    max_faces: int = 10,
) -> Optional[str]:
    return detect_and_draw_sync(client, frame_bgr, max_faces)