from types import SimpleNamespace
import time

import cv2
import numpy as np
import pytest

from protocol import encode_frame
from zenoh_client import ZenohImageCapture


def _jpeg_from_rgb(rgb: np.ndarray) -> bytes:
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode(".png", bgr)
    assert ok
    return encoded.tobytes()


def test_callback_and_read_return_rgb_copy():
    expected = np.zeros((3, 4, 3), dtype=np.uint8)
    expected[:, :] = (240, 20, 10)
    capture = ZenohImageCapture("camera/annotated")
    capture._running = True
    capture._on_sample(SimpleNamespace(payload=_jpeg_from_rgb(expected)))

    success, image = capture.read(timeout=0)
    assert success
    assert np.array_equal(image, expected)
    assert capture.frame_sequence == 1
    image[0, 0] = 0
    assert not np.array_equal(image, capture._latest_frame)


def test_metadata_is_optional_and_does_not_change_read_tuple():
    expected = np.full((2, 2, 3), 90, dtype=np.uint8)
    metadata = {"seq": 3, "faces": []}
    capture = ZenohImageCapture("camera/annotated")
    capture._running = True
    capture._on_sample(SimpleNamespace(
        payload=encode_frame(_jpeg_from_rgb(expected), metadata)
    ))

    success, image = capture.read(timeout=0)
    assert success
    assert np.array_equal(image, expected)
    assert capture.latest_meta == metadata


def test_client_rejects_negative_fps():
    with pytest.raises(ValueError):
        ZenohImageCapture("camera/annotated", fps=-1)


def test_client_fps_limits_read_frequency():
    expected = np.full((2, 2, 3), 50, dtype=np.uint8)
    capture = ZenohImageCapture("camera/annotated", fps=20)
    capture._running = True
    capture._on_sample(SimpleNamespace(payload=_jpeg_from_rgb(expected)))

    assert capture.read(timeout=0)[0]
    assert capture.read(timeout=0)[0] is False
    time.sleep(0.06)
    assert capture.read(timeout=0)[0]
