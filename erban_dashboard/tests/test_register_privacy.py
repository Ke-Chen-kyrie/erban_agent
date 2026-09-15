import unittest
import subprocess
from html.parser import HTMLParser
from pathlib import Path


REGISTER_HTML = (
    Path(__file__).resolve().parents[1]
    / "erban_dashboard_app"
    / "static"
    / "register.html"
)


class RegistrationPrivacyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.user_id_attributes: dict[str, str | None] | None = None
        self.user_id_count = 0
        self.has_summary_id = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id") == "userId":
            self.user_id_attributes = attributes
            self.user_id_count += 1
        if attributes.get("id") == "summaryId":
            self.has_summary_id = True


class RegisterPrivacyTest(unittest.TestCase):
    def test_generated_user_id_is_not_visible(self) -> None:
        html = REGISTER_HTML.read_text(encoding="utf-8")
        parser = RegistrationPrivacyParser()
        parser.feed(html)

        self.assertIsNotNone(parser.user_id_attributes)
        self.assertEqual(parser.user_id_count, 1)
        self.assertEqual(parser.user_id_attributes.get("type"), "hidden")
        self.assertFalse(parser.has_summary_id)
        self.assertNotIn("用户 ID", html)

    def test_hidden_user_id_is_generated_and_submitted(self) -> None:
        harness = Path(__file__).with_name("register_page_harness.js")
        result = subprocess.run(
            ["node", str(harness), str(REGISTER_HTML)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
