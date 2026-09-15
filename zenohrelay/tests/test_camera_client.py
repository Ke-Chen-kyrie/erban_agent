import time

import numpy as np

import camera_client
from camera_client import LocalCameraCapture


class FakeVideoCapture:
    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.released = False
        self.settings = {}
        self.bgr = np.zeros((2, 3, 3), dtype=np.uint8)
        self.bgr[:, :] = (10, 20, 240)

    def isOpened(self):
        return True

    def set(self, key, value):
        self.settings[key] = value
        return True

    def read(self):
        time.sleep(0.001)
        return True, self.bgr.copy()

    def release(self):
        self.released = True


def test_local_camera_returns_latest_rgb(monkeypatch):
    fake = FakeVideoCapture(3)
    monkeypatch.setattr(camera_client.cv2, "VideoCapture", lambda _: fake)
    capture = LocalCameraCapture(3, width=640, height=480)
    try:
        success, rgb, sequence, timestamp_ns = capture.read_next(0, timeout=1)
        assert success
        assert sequence >= 1
        assert timestamp_ns > 0
        assert tuple(rgb[0, 0]) == (240, 20, 10)
        assert fake.settings[camera_client.cv2.CAP_PROP_FRAME_WIDTH] == 640
        assert fake.settings[camera_client.cv2.CAP_PROP_FRAME_HEIGHT] == 480
    finally:
        capture.release()
    assert fake.released
