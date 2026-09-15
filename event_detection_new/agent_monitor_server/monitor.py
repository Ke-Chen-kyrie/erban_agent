"""Small frame helpers used by the monitoring loop."""

from __future__ import annotations

import io

import cv2
from PIL import Image

from .video_source import CapturedFrame


def encode_frame_jpeg(frame: CapturedFrame) -> bytes:
    image = frame.image
    if frame.bgr_input:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    output = io.BytesIO()
    Image.fromarray(image).save(output, format="JPEG", quality=85)
    return output.getvalue()

