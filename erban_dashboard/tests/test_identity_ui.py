import asyncio
import os
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from main import create_app


class ElementAttributeParser(HTMLParser):
    def __init__(self, element_ids: set[str]) -> None:
        super().__init__()
        self.element_ids = element_ids
        self.attributes: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id in self.element_ids:
            self.attributes[element_id] = attributes


class IdentityPageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA_DIR"] = self.temporary_directory.name
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ["PROCESS_RESTORE_ENABLED"] = "false"
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        for name in (
            "DASHBOARD_DATA_DIR",
            "DASHBOARD_CAPTURE_ENABLED",
            "PROCESS_RESTORE_ENABLED",
        ):
            os.environ.pop(name, None)
        self.temporary_directory.cleanup()

    async def test_home_has_links_to_each_function(self) -> None:
        response = await self.client.get("/")
        home_html = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn('href="/register"', home_html)
        self.assertIn('href="/users"', home_html)
        self.assertIn('href="/dashboard"', home_html)
        self.assertIn("用户注册", home_html)
        self.assertIn("用户管理", home_html)
        self.assertNotIn("智能照护事件感知平台", home_html)
        self.assertIn("坚持热爱　追求卓越", home_html)
        self.assertIn('data-title="小伴agent"', home_html)
        self.assertIn("<h2>小伴agent</h2>", home_html)
        self.assertIn("进入小伴agent", home_html)
        self.assertIn('class="icon robot-icon"', home_html)

    async def test_home_can_host_features_without_leaving_fullscreen_shell(self) -> None:
        response = await self.client.get("/")
        home_html = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn('id="fullscreenToggle"', home_html)
        self.assertIn('id="featureFrame"', home_html)
        self.assertIn("requestFullscreen", home_html)
        self.assertIn("loadFeature(", home_html)
        self.assertIn('data-path="/register"', home_html)
        self.assertIn('data-path="/users"', home_html)
        self.assertIn('data-path="/dashboard"', home_html)

    async def test_dashboard_is_served_from_its_own_route(self) -> None:
        response = await self.client.get("/dashboard")

        self.assertEqual(response.status, 200)
        self.assertIn("守护视界 · 实时事件中心", await response.text())

    async def test_dashboard_does_not_render_person_names(self) -> None:
        response = await self.client.get("/dashboard")
        dashboard_html = await response.text()

        self.assertEqual(response.status, 200)
        self.assertNotIn('id="eventNameBadge"', dashboard_html)
        self.assertNotIn('class="name-badge"', dashboard_html)
        self.assertNotIn("event.name", dashboard_html)
        self.assertNotIn("item.name", dashboard_html)

    async def test_identity_navigation_is_detached_from_the_dashboard(self) -> None:
        dashboard = await self.client.get("/dashboard")
        dashboard_html = await dashboard.text()

        self.assertEqual(dashboard.status, 200)
        self.assertIn('href="/"', dashboard_html)
        self.assertIn("返回首页", dashboard_html)
        self.assertNotIn('href="/register"', dashboard_html)
        self.assertNotIn('href="/users"', dashboard_html)

        for path in ("/register", "/users"):
            response = await self.client.get(path)
            page_html = await response.text()

            self.assertEqual(response.status, 200)
            self.assertIn('href="/"', page_html)
            self.assertIn("返回首页", page_html)
            self.assertNotIn("返回大屏", page_html)

    async def test_identity_pages_can_still_be_opened(self) -> None:
        register = await self.client.get("/register")
        self.assertEqual(register.status, 200)
        self.assertIn("用户注册", await register.text())

        users = await self.client.get("/users")
        self.assertEqual(users.status, 200)
        self.assertIn("用户管理", await users.text())

    async def test_registration_capture_controls_start_in_the_idle_state(self) -> None:
        response = await self.client.get("/register")
        parser = ElementAttributeParser({"cameraPreview", "audioBtn"})
        parser.feed(await response.text())

        self.assertIn("display:none", parser.attributes["cameraPreview"].get("style", ""))
        audio_classes = (parser.attributes["audioBtn"].get("class") or "").split()
        self.assertNotIn("recording", audio_classes)

    async def test_registration_uses_a_three_step_consumer_wizard(self) -> None:
        response = await self.client.get("/register")
        parser = ElementAttributeParser(
            {"profileStep", "faceStep", "voiceStep", "backBtn", "nextBtn", "submitBtn"}
        )
        parser.feed(await response.text())

        self.assertEqual(
            set(parser.attributes),
            {"profileStep", "faceStep", "voiceStep", "backBtn", "nextBtn", "submitBtn"},
        )
        self.assertEqual(parser.attributes["profileStep"].get("data-step"), "1")
        self.assertEqual(parser.attributes["faceStep"].get("data-step"), "2")
        self.assertEqual(parser.attributes["voiceStep"].get("data-step"), "3")

    async def test_user_management_has_no_bulk_database_clear_action(self) -> None:
        response = await self.client.get("/users")
        html = await response.text()

        self.assertNotIn("一键清库", html)
        self.assertNotIn('id="clearBtn"', html)
        self.assertNotIn("clearDatabase", html)

    async def test_custom_dashboard_html_is_isolated_from_packaged_home_and_identity_pages(self) -> None:
        custom_dir = Path(self.temporary_directory.name) / "custom-static"
        custom_dir.mkdir()
        custom_dashboard = custom_dir / "dashboard.html"
        custom_dashboard.write_text("<html><body>custom dashboard</body></html>", encoding="utf-8")

        try:
            custom_app = create_app(custom_dashboard)
        except FileNotFoundError as exc:
            self.fail(f"custom dashboard should reuse packaged static resources: {exc}")
        custom_client = TestClient(TestServer(custom_app))
        await custom_client.start_server()
        try:
            home = await custom_client.get("/")
            self.assertIn('href="/dashboard"', await home.text())
            self.assertNotIn("custom dashboard", await home.text())
            dashboard = await custom_client.get("/dashboard")
            self.assertIn("custom dashboard", await dashboard.text())
            register = await custom_client.get("/register")
            self.assertEqual(register.status, 200)
            self.assertIn("用户注册", await register.text())
            users = await custom_client.get("/users")
            self.assertEqual(users.status, 200)
            self.assertIn("用户管理", await users.text())
            icon_preview = await custom_client.get("/icons")
            self.assertEqual(icon_preview.status, 200)
            self.assertIn("事件图标", await icon_preview.text())
            event_icon = await custom_client.get("/event_icons/fall.png")
            self.assertEqual(event_icon.status, 200)
            self.assertEqual(event_icon.content_type, "image/png")
        finally:
            await custom_client.close()


class IdentityProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA_DIR"] = self.temporary_directory.name
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ["PROCESS_RESTORE_ENABLED"] = "false"

        upstream_app = web.Application()
        upstream_app.router.add_get("/api/user/list", self._list_users)
        upstream_app.router.add_post("/api/user/register", self._register_user)
        upstream_app.router.add_get("/api/user/{user_id}/face", self._get_face)
        upstream_app.router.add_delete("/api/user/{user_id}", self._delete_user)
        upstream_app.router.add_post("/api/admin/sync", self._sync_users)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        os.environ["IDENTITY_API_BASE"] = str(self.upstream.make_url("/")).rstrip("/")

        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.upstream.close()
        for name in (
            "DASHBOARD_DATA_DIR",
            "DASHBOARD_CAPTURE_ENABLED",
            "PROCESS_RESTORE_ENABLED",
            "IDENTITY_API_BASE",
        ):
            os.environ.pop(name, None)
        self.temporary_directory.cleanup()

    async def _list_users(self, request: web.Request) -> web.Response:
        if request.query.get("delay"):
            await asyncio.sleep(float(request.query["delay"]))
        return web.json_response(
            {
                "total": 1,
                "users": [
                    {
                        "user_id": "care01",
                        "name": "张阿姨",
                        "role": "老人",
                        "description": "重点关注",
                        "created_at": "2026-08-26 08:00:00",
                    }
                ],
            }
        )

    async def _register_user(self, request: web.Request) -> web.Response:
        fields: dict[str, str | int] = {}
        async for part in await request.multipart():
            if part.filename:
                fields[part.name] = len(await part.read())
            else:
                fields[part.name] = await part.text()
        return web.json_response(fields, status=201)

    async def _get_face(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"user_id": request.match_info["user_id"], "face_image": "ZmFrZS1qcGVn"}
        )

    async def _delete_user(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"user_id": request.match_info["user_id"], "deleted": True}, status=202
        )

    async def _sync_users(self, request: web.Request) -> web.Response:
        return web.json_response({"voice_cleanup": [], "face_cleanup": ["orphan-1"]})

    async def test_user_list_is_forwarded_to_identity_service(self) -> None:
        response = await self.client.get("/api/identity/users")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["users"][0]["user_id"], "care01")

    async def test_registration_multipart_is_forwarded_without_losing_media(self) -> None:
        form = FormData()
        form.add_field("user_id", "care02")
        form.add_field("name", "李叔叔")
        form.add_field("role", "老人")
        form.add_field("description", "")
        form.add_field(
            "face_image", b"jpeg-content", filename="face.jpg", content_type="image/jpeg"
        )
        form.add_field(
            "voice_audio", b"wav-content", filename="voice.wav", content_type="audio/wav"
        )

        response = await self.client.post("/api/identity/users/register", data=form)
        self.assertEqual(response.status, 201)
        self.assertEqual(
            await response.json(),
            {
                "user_id": "care02",
                "name": "李叔叔",
                "role": "老人",
                "description": "",
                "face_image": 12,
                "voice_audio": 11,
            },
        )

    async def test_user_face_and_delete_preserve_the_selected_user_id(self) -> None:
        user_id = "care 03"
        face = await self.client.get("/api/identity/users/care%2003/face")
        self.assertEqual(face.status, 200)
        self.assertEqual((await face.json())["user_id"], user_id)

        deleted = await self.client.delete("/api/identity/users/care%2003")
        self.assertEqual(deleted.status, 202)
        self.assertEqual(await deleted.json(), {"user_id": user_id, "deleted": True})

    async def test_sync_is_forwarded_to_identity_admin_api(self) -> None:
        response = await self.client.post("/api/identity/sync")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["face_cleanup"], ["orphan-1"])

    async def test_unavailable_identity_service_returns_a_clear_gateway_error(self) -> None:
        await self.upstream.close()

        response = await self.client.get("/api/identity/users")

        self.assertEqual(response.status, 502)
        self.assertEqual(await response.json(), {"detail": "身份服务暂时不可用"})

    async def test_identity_timeout_is_configurable_and_returns_a_gateway_timeout(self) -> None:
        os.environ["IDENTITY_API_TIMEOUT"] = "0.05"
        timeout_client = TestClient(TestServer(create_app()))
        await timeout_client.start_server()
        try:
            response = await timeout_client.get("/api/identity/users?delay=0.1")
            self.assertEqual(response.status, 504)
            self.assertEqual(await response.json(), {"detail": "身份服务请求超时"})
        finally:
            await timeout_client.close()
            os.environ.pop("IDENTITY_API_TIMEOUT", None)


class IdentityPageBrowserLogicTest(unittest.TestCase):
    def run_users_scenario(self, scenario: str) -> None:
        tests_dir = Path(__file__).resolve().parent
        result = subprocess.run(
            [
                "node",
                str(tests_dir / "users_page_harness.js"),
                scenario,
                str(tests_dir.parent / "erban_dashboard_app" / "static" / "users.html"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_stale_face_response_cannot_replace_current_user_photo(self) -> None:
        self.run_users_scenario("face-race")

    def test_delete_false_is_reported_as_a_failure(self) -> None:
        self.run_users_scenario("delete-false")


if __name__ == "__main__":
    unittest.main()
