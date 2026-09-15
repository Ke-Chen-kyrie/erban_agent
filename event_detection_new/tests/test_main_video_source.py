import os
import unittest
from unittest.mock import Mock, patch

import main


class VideoSourceFactoryTest(unittest.TestCase):
    def test_zenoh_source_uses_deployed_connection_defaults(self) -> None:
        with patch.dict(os.environ, {"VIDEO_SOURCE": "zenoh"}, clear=True):
            capture, bgr_input, description = main._create_video_source()

        try:
            self.assertEqual(capture._topic, "camera/annotated")
            self.assertEqual(capture._url, "tcp/192.168.1.100:7450")
            self.assertEqual(capture._fps, 2.0)
            self.assertFalse(bgr_input)
            self.assertEqual(description, "Zenoh topic=camera/annotated fps=2")
        finally:
            capture.release()

    def test_zenoh_frame_read_waits_two_seconds(self) -> None:
        capture = Mock()
        capture.read.return_value = (False, None)

        result = main._read_capture_frame(capture, "zenoh")

        self.assertEqual(result, (False, None))
        capture.read.assert_called_once_with(timeout=2.0)

    def test_zenoh_source_returns_rgb_capture_with_client_fps(self) -> None:
        environment = {
            "VIDEO_SOURCE": "zenoh",
            "ZENOH_TOPIC": "camera/annotated",
            "ZENOH_URL": "",
            "ZENOH_CLIENT_FPS": "2",
        }
        with patch.dict(os.environ, environment, clear=False):
            capture, bgr_input, description = main._create_video_source()

        try:
            self.assertEqual(capture.__class__.__name__, "ZenohImageCapture")
            self.assertEqual(capture._topic, "camera/annotated")
            self.assertEqual(capture._fps, 2.0)
            self.assertFalse(bgr_input)
            self.assertEqual(description, "Zenoh topic=camera/annotated fps=2")
        finally:
            capture.release()

    def test_rosbridge_source_returns_rgb_capture_with_url(self) -> None:
        environment = {
            "VIDEO_SOURCE": "rosbridge",
            "FOXGLOVE_TOPIC": "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed",
            "ROSBRIDGE_URL": "ws://192.168.217.100:9090",
        }
        with patch.dict(os.environ, environment, clear=False):
            capture, bgr_input, description = main._create_video_source()

        try:
            self.assertEqual(capture.__class__.__name__, "RosbridgeImageCapture")
            self.assertEqual(capture._topic, "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed")
            self.assertEqual(capture._url, "ws://192.168.217.100:9090")
            self.assertFalse(bgr_input)
            self.assertEqual(
                description,
                "ROS1 rosbridge topic=/zj_humanoid/sensor/realsense_head/color/image_raw/compressed",
            )
        finally:
            capture.release()


if __name__ == "__main__":
    unittest.main()
