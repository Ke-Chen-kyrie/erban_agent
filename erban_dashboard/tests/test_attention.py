import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from erban_dashboard_app import create_app


class AttentionProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA_DIR"] = self.temporary_directory.name
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ["PROCESS_RESTORE_ENABLED"] = "false"

        upstream_app = web.Application()
        upstream_app.router.add_get("/render/queue", self._queue)
        upstream_app.router.add_get("/render/pop", self._pop)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        os.environ["ATTENTION_API_BASE"] = str(self.upstream.make_url("/")).rstrip("/")

        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.upstream.close()
        for name in (
            "DASHBOARD_DATA_DIR",
            "DASHBOARD_CAPTURE_ENABLED",
            "PROCESS_RESTORE_ENABLED",
            "ATTENTION_API_BASE",
        ):
            os.environ.pop(name, None)
        self.temporary_directory.cleanup()

    async def _queue(self, request: web.Request) -> web.Response:
        return web.json_response({"queue_size": 1})

    async def _pop(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "rendered": {
                    "image_base64": "data:image/png;base64,cG5n",
                    "frame": "head_camera",
                    "joints": ["head"],
                    "timestamp": 1_788_000_000,
                }
            }
        )

    async def test_attention_queue_and_pop_are_available_through_dashboard(self) -> None:
        queue_response = await self.client.get("/api/attention/queue")
        pop_response = await self.client.get("/api/attention/pop")

        self.assertEqual(queue_response.status, 200)
        self.assertEqual(await queue_response.json(), {"queue_size": 1})
        self.assertEqual(pop_response.status, 200)
        self.assertEqual(
            (await pop_response.json())["rendered"]["image_base64"],
            "data:image/png;base64,cG5n",
        )


class AttentionPageTest(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        tests_dir = Path(__file__).resolve().parent
        dashboard_html = tests_dir.parent / "erban_dashboard_app" / "static" / "dashboard.html"
        result = subprocess.run(
            [
                "node",
                str(tests_dir / "attention_page_harness.js"),
                scenario,
                str(dashboard_html),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_attention_toggle_overlays_a_loaded_result_for_three_seconds(self) -> None:
        self.run_scenario("loaded-result")

    def test_attention_starts_disabled_even_if_browser_restores_form_state(self) -> None:
        self.run_scenario("restored-form")

    def test_disabling_attention_aborts_an_unfinished_queue_request(self) -> None:
        self.run_scenario("abort-request")

    def test_attention_preserves_an_existing_image_data_url(self) -> None:
        self.run_scenario("data-url")

    def test_attention_uses_the_renderer_declared_image_mime_type(self) -> None:
        self.run_scenario("declared-mime")

    def test_attention_image_decode_failure_keeps_live_video_visible(self) -> None:
        self.run_scenario("image-error")


if __name__ == "__main__":
    unittest.main()
