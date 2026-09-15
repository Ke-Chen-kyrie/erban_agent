import io
import tempfile
import unittest

from PIL import Image

from main import DASHBOARD_HTML_KEY, ICON_PREVIEW_HTML_KEY, create_app


class ShoulderEventDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.app = create_app(data_dir=self.temporary_directory.name)

    def test_dashboard_localizes_rub_shoulder(self) -> None:
        dashboard = self.app[DASHBOARD_HTML_KEY].read_text(encoding="utf-8")

        self.assertIn('rub_shoulder: "揉肩"', dashboard)

    def test_icon_preview_lists_rub_shoulder(self) -> None:
        preview = self.app[ICON_PREVIEW_HTML_KEY].read_text(encoding="utf-8")

        self.assertIn('["rub_shoulder","揉肩"]', preview)

    def test_rub_shoulder_icon_is_a_transparent_png(self) -> None:
        icon_path = self.app[DASHBOARD_HTML_KEY].with_name("event_icons") / "rub_shoulder.png"
        self.assertTrue(icon_path.is_file(), "rub_shoulder.png must be provided to the dashboard")
        image_bytes = icon_path.read_bytes()

        with Image.open(io.BytesIO(image_bytes)) as icon:
            self.assertEqual(icon.format, "PNG")
            self.assertEqual(icon.mode, "RGBA")
            self.assertLess(icon.getchannel("A").getextrema()[0], 255)


if __name__ == "__main__":
    unittest.main()
