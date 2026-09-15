import time
import unittest
from unittest.mock import Mock

from agent_monitor_server.config import MonitorConfig
from agent_monitor_server.video_source import LatestFrameCapture


class FakeSource:
    def __init__(self) -> None:
        self.frames = [(True, "frame-1"), (True, "frame-2")]
        self.released = False

    def read(self, timeout=1.0):
        if self.frames:
            return self.frames.pop(0)
        time.sleep(0.005)
        return False, None

    def release(self) -> None:
        self.released = True


class LatestFrameCaptureTest(unittest.TestCase):
    def test_source_is_created_only_when_started(self) -> None:
        source = FakeSource()
        factory = Mock(return_value=(source, False, "fake"))
        capture = LatestFrameCapture(MonitorConfig(), factory=factory)

        self.assertEqual(factory.call_count, 0)
        capture.start()
        self.assertEqual(factory.call_count, 1)
        capture.close()
        self.assertTrue(source.released)

    def test_latest_returns_newest_frame_and_clear_hides_old_frame(self) -> None:
        source = FakeSource()
        capture = LatestFrameCapture(
            MonitorConfig(frame_interval=0),
            factory=Mock(return_value=(source, False, "fake")),
        )
        capture.start()

        deadline = time.monotonic() + 1
        frame = None
        while (frame is None or frame.image != "frame-2") and time.monotonic() < deadline:
            frame = capture.latest(after_sequence=0, timeout=0.05)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.image, "frame-2")
        capture.clear()
        self.assertIsNone(capture.latest(after_sequence=0, timeout=0.01))
        capture.close()

    def test_close_is_idempotent(self) -> None:
        source = FakeSource()
        capture = LatestFrameCapture(
            MonitorConfig(), factory=Mock(return_value=(source, False, "fake"))
        )
        capture.start()
        capture.close()
        capture.close()

        self.assertTrue(source.released)

    def test_read_exception_is_exposed_without_raw_message(self) -> None:
        class BrokenSource(FakeSource):
            def read(self, timeout=1.0):
                raise RuntimeError("rtsp://user:password@camera")

        capture = LatestFrameCapture(
            MonitorConfig(frame_interval=0),
            factory=Mock(return_value=(BrokenSource(), False, "broken")),
        )
        capture.start()
        deadline = time.monotonic() + 1
        while capture.last_error is None and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertEqual(capture.last_error, "video_read_failed:RuntimeError")
        capture.close()


if __name__ == "__main__":
    unittest.main()
