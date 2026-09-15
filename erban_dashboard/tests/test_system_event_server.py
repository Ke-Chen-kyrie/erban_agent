import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from main import PROCESS_MANAGER_KEY, create_app

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "dummy_proc"
SERVER_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class AgentStreamingWebSocketTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA_DIR"] = self.temporary_directory.name
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ.pop("AGENT_WS_TOKEN", None)
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        os.environ.pop("DASHBOARD_DATA_DIR", None)
        self.temporary_directory.cleanup()

    async def test_agent_stream_is_broadcast_and_persisted(self) -> None:
        browser = await self.client.ws_connect("/ws")
        connected = await browser.receive_json()
        self.assertEqual(connected["type"], "connected")

        agent = await self.client.ws_connect("/agent/ws")
        handshake = await agent.receive_json()
        self.assertEqual(handshake["protocol"], "agent-chat-stream.v1")

        await agent.send_json({"type": "chat_start", "message_id": "reply-1", "role": "agent"})
        self.assertEqual((await agent.receive_json())["event"], "chat_start")
        started = await browser.receive_json()
        self.assertEqual(started["type"], "chat_start")

        await agent.send_json({"type": "chat_delta", "message_id": "reply-1", "delta": "您好，"})
        delta = await browser.receive_json()
        self.assertEqual(delta["delta"], "您好，")

        await agent.send_json({"type": "chat_delta", "message_id": "reply-1", "delta": "正在分析。"})
        await browser.receive_json()
        await agent.send_json({"type": "chat_end", "message_id": "reply-1"})
        self.assertEqual((await agent.receive_json())["event"], "chat_end")
        ended = await browser.receive_json()
        self.assertEqual(ended["message"]["content"], "您好，正在分析。")

        response = await self.client.get("/api/state")
        state = await response.json()
        self.assertEqual(state["streams"], [])
        self.assertEqual(state["messages"][-1]["content"], "您好，正在分析。")

        await agent.close()
        await browser.close()

    async def test_agent_stream_can_require_a_token(self) -> None:
        os.environ["AGENT_WS_TOKEN"] = "test-secret"
        try:
            response = await self.client.get("/agent/ws")
            self.assertEqual(response.status, 401)

            agent = await self.client.ws_connect(
                "/agent/ws",
                headers={"Authorization": "Bearer test-secret"},
            )
            self.assertEqual((await agent.receive_json())["type"], "connected")
            await agent.close()
        finally:
            os.environ.pop("AGENT_WS_TOKEN", None)


class RenderSocketTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the /render endpoint (Agent push rendering, agent-side socket.md)."""

    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA_DIR"] = self.temporary_directory.name
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ.pop("AGENT_WS_TOKEN", None)
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        os.environ.pop("DASHBOARD_DATA_DIR", None)
        self.temporary_directory.cleanup()

    async def test_render_streams_a_full_reply(self) -> None:
        browser = await self.client.ws_connect("/ws")
        self.assertEqual((await browser.receive_json())["type"], "connected")

        render = await self.client.ws_connect("/render")
        self.assertEqual((await render.receive_json())["protocol"], "render-socket.v1")

        await render.send_json({"type": "user_speech_start"})
        self.assertEqual((await render.receive_json())["ref_type"], "user_speech_start")
        status = await browser.receive_json()
        self.assertEqual(status, {"type": "agent_status", "status": "listening"})

        await render.send_json({"type": "user_speech", "text": "今天天气怎么样"})
        self.assertEqual((await render.receive_json())["ref_type"], "user_speech")
        chat = await browser.receive_json()
        self.assertEqual(chat["type"], "chat")
        self.assertEqual(chat["message"]["role"], "user")
        self.assertEqual(chat["message"]["content"], "今天天气怎么样")

        await render.send_json({"type": "agent_response_start"})
        self.assertEqual((await render.receive_json())["ref_type"], "agent_response_start")
        started = await browser.receive_json()
        self.assertEqual(started["type"], "chat_start")

        for index, token in enumerate(["好的", "，我", "看一下"]):
            await render.send_json({"type": "agent_token", "text": token, "index": index})
            self.assertEqual((await render.receive_json())["ref_type"], "agent_token")
            delta = await browser.receive_json()
            self.assertEqual(delta["type"], "chat_delta")
            self.assertEqual(delta["delta"], token)

        await render.send_json({"type": "agent_response_end"})
        self.assertEqual((await render.receive_json())["ref_type"], "agent_response_end")
        ended = await browser.receive_json()
        self.assertEqual(ended["type"], "chat_end")
        self.assertEqual(ended["message"]["content"], "好的，我看一下")

        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(state["streams"], [])
        self.assertEqual(state["messages"][-1]["content"], "好的，我看一下")

        await render.close()
        await browser.close()

    async def test_render_token_only_stream_uses_index_zero(self) -> None:
        browser = await self.client.ws_connect("/ws")
        await browser.receive_json()
        render = await self.client.ws_connect("/render")
        await render.receive_json()

        # No agent_response_start — index=0 opens the bubble lazily and carries the first token.
        await render.send_json({"type": "agent_token", "text": "第", "index": 0})
        self.assertEqual((await render.receive_json())["ref_type"], "agent_token")
        self.assertEqual((await browser.receive_json())["type"], "chat_start")
        self.assertEqual((await browser.receive_json())["delta"], "第")

        await render.send_json({"type": "agent_token", "text": "一句", "index": 1})
        await render.receive_json()
        self.assertEqual((await browser.receive_json())["delta"], "一句")

        await render.send_json({"type": "agent_response_end"})
        await render.receive_json()
        await render.close()
        await browser.close()

    async def test_render_interrupted_keeps_partial_text(self) -> None:
        browser = await self.client.ws_connect("/ws")
        await browser.receive_json()
        render = await self.client.ws_connect("/render")
        await render.receive_json()

        await render.send_json({"type": "agent_response_start"})
        await render.receive_json()
        await browser.receive_json()

        await render.send_json({"type": "agent_token", "text": "今天天气", "index": 0})
        await render.receive_json()
        await browser.receive_json()

        await render.send_json({"type": "agent_response_interrupted", "partial_text": "今天天气"})
        self.assertEqual((await render.receive_json())["ref_type"], "agent_response_interrupted")
        ended = await browser.receive_json()
        self.assertEqual(ended["type"], "chat_end")
        self.assertEqual(ended["message"]["content"], "今天天气")
        self.assertTrue(ended["message"].get("interrupted"))

        await render.close()
        await browser.close()

    async def test_render_system_event_and_ping_pong(self) -> None:
        browser = await self.client.ws_connect("/ws")
        await browser.receive_json()
        render = await self.client.ws_connect("/render")
        await render.receive_json()

        await render.send_json({"type": "ping"})
        self.assertEqual((await render.receive_json())["type"], "pong")

        await render.send_json({"type": "system_event", "event_name": "fall_detected", "description": "检测到老人摔倒"})
        self.assertEqual((await render.receive_json())["ref_type"], "system_event")
        event = await browser.receive_json()
        self.assertEqual(event["type"], "event")
        self.assertEqual(event["event"]["event"], "fall_detected")
        notice = await browser.receive_json()
        self.assertEqual(notice["type"], "system_notice")
        self.assertEqual(notice["notice"]["title"], "fall_detected")

        await render.close()
        await browser.close()

    async def test_clear_chat_empties_history_and_broadcasts(self) -> None:
        browser = await self.client.ws_connect("/ws")
        await browser.receive_json()

        post = await self.client.post("/api/chat", json={"role": "user", "content": "今天天气怎么样"})
        self.assertEqual(post.status, 200)
        self.assertEqual((await browser.receive_json())["type"], "chat")

        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(len(state["messages"]), 1)

        cleared = await self.client.post("/api/chat/clear")
        self.assertEqual(cleared.status, 200)
        self.assertEqual((await browser.receive_json())["type"], "chat_cleared")

        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(state["messages"], [])

        await browser.close()


class ProcessManagerApiTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the one-click launch endpoints (/api/processes)."""

    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA_DIR"] = self.temporary_directory.name
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ["PROCESS_RESTORE_ENABLED"] = "false"
        # 本类是 host 后端测试：显式声明，避免被 .env 里部署用的 docker 后端影响。
        os.environ["EVENT_DETECTION_BACKEND"] = "host"
        os.environ.pop("LAUNCH_TOKEN", None)
        os.environ.pop("AGENT_WS_TOKEN", None)
        os.environ.pop("EVENT_DETECTION_DIR", None)
        os.environ.pop("AGENT_DIR", None)
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        os.environ.pop("DASHBOARD_DATA_DIR", None)
        os.environ.pop("EVENT_DETECTION_BACKEND", None)
        self.temporary_directory.cleanup()

    async def test_process_list_has_two_entries(self) -> None:
        response = await self.client.get("/api/processes")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(len(data["processes"]), 2)
        self.assertEqual({p["key"] for p in data["processes"]}, {"event_detection", "agent"})
        self.assertTrue(all(p["status"] == "stopped" for p in data["processes"]))

    async def test_default_process_directories_are_sibling_projects(self) -> None:
        repository_dir = Path(__file__).resolve().parents[2]
        manager = self.client.server.app[PROCESS_MANAGER_KEY]

        self.assertEqual(
            Path(manager.processes["event_detection"].cwd),
            repository_dir / "event_detection_new",
        )
        self.assertEqual(
            Path(manager.processes["agent"].cwd),
            repository_dir / "proactive_agent",
        )

    async def test_process_start_unknown_key_404(self) -> None:
        response = await self.client.post("/api/processes/bogus/start")
        self.assertEqual(response.status, 404)

    async def test_process_start_missing_dir_400(self) -> None:
        os.environ["EVENT_DETECTION_DIR"] = "/nonexistent/dir/xyz"
        client = TestClient(TestServer(create_app()))
        await client.start_server()
        try:
            response = await client.post("/api/processes/event_detection/start")
            self.assertEqual(response.status, 400)
        finally:
            await client.close()

    async def test_launch_endpoints_require_token(self) -> None:
        os.environ["LAUNCH_TOKEN"] = "secret-token"
        os.environ["EVENT_DETECTION_DIR"] = "/nonexistent/dir/xyz"
        client = TestClient(TestServer(create_app()))
        await client.start_server()
        try:
            no_token = await client.post("/api/processes/event_detection/start")
            self.assertEqual(no_token.status, 401)
            wrong_token = await client.post(
                "/api/processes/event_detection/start", headers={"X-Launch-Token": "wrong"}
            )
            self.assertEqual(wrong_token.status, 401)
            valid = await client.post(
                "/api/processes/event_detection/start", headers={"X-Launch-Token": "secret-token"}
            )
            # Token accepted -> falls through to the missing-dir check (400, not 401).
            self.assertEqual(valid.status, 400)
        finally:
            await client.close()

    async def test_process_start_stop_lifecycle(self) -> None:
        os.environ["EVENT_DETECTION_DIR"] = str(FIXTURE_DIR)
        client = TestClient(TestServer(create_app()))
        await client.start_server()
        pid_file = SERVER_LOG_DIR / "process_event_detection.pid"
        try:
            started = await client.post("/api/processes/event_detection/start")
            self.assertEqual(started.status, 200)
            process = (await started.json())["process"]
            self.assertEqual(process["status"], "running")
            self.assertIsNotNone(process["pid"])
            self.assertEqual(int(pid_file.read_text().strip()), process["pid"])

            # Double start is rejected.
            again = await client.post("/api/processes/event_detection/start")
            self.assertEqual(again.status, 409)

            stopped = await client.post("/api/processes/event_detection/stop")
            self.assertEqual(stopped.status, 200)
            self.assertEqual((await stopped.json())["process"]["status"], "stopped")
            self.assertFalse(pid_file.exists())
        finally:
            await client.post("/api/processes/event_detection/stop")  # ensure clean state
            await client.close()


class FakeDocker:
    """Stateful fake of `docker inspect/start/stop/kill/run` for ProcessManager tests."""

    def __init__(self, exists: bool = True, initial: str = "exited",
                 image_exists: bool = True) -> None:
        self.exists = exists
        self.status = initial  # running | exited | created
        self.pid = 4242
        self.exit_code = 0
        self.start_rc = 0
        self.start_stderr = ""
        self.start_calls = 0
        self.run_rc = 0
        self.run_stderr = ""
        self.run_calls = 0
        self.run_args: list[str] | None = None
        self.run_cwd: str | None = None
        self.image_exists = image_exists
        self.rm_calls = 0
        self.stop_calls = 0

    def run(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        cmd = list(args)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "inspect":
            if not self.exists:
                return subprocess.CompletedProcess(args, 1, "", "Error: No such object")
            fmt = cmd[cmd.index("-f") + 1]
            if fmt == "{{.State.Status}}":
                out = self.status
            elif fmt == "{{.State.Status}} {{.State.Pid}}":
                out = f"{self.status} {self.pid}"
            elif fmt == "{{.State.ExitCode}}":
                out = str(self.exit_code)
            else:
                out = ""
            return subprocess.CompletedProcess(args, 0, out + "\n", "")
        if sub == "image":
            # docker image inspect <image>
            if cmd[2] == "inspect":
                if not self.image_exists:
                    return subprocess.CompletedProcess(args, 1, "", "Error: No such image")
                return subprocess.CompletedProcess(args, 0, "sha256:abcdef\n", "")
        if sub == "rm":
            self.rm_calls += 1
            self.exists = False
            self.status = ""
            return subprocess.CompletedProcess(args, 0, "event-detection\n", "")
        if sub == "run":
            self.run_calls += 1
            self.run_args = cmd
            self.run_cwd = kwargs.get("cwd")
            if self.run_rc != 0:
                return subprocess.CompletedProcess(args, self.run_rc, "", self.run_stderr)
            self.status = "running"
            self.exists = True  # docker run 会新建容器
            return subprocess.CompletedProcess(args, 0, "0123456789abcdef\n", "")
        if sub == "start":
            self.start_calls += 1
            if self.start_rc != 0:
                return subprocess.CompletedProcess(args, self.start_rc, "", self.start_stderr)
            self.status = "running"
            return subprocess.CompletedProcess(args, 0, "event-detection\n", "")
        if sub == "stop":
            self.stop_calls += 1
            self.status = "exited"
            return subprocess.CompletedProcess(args, 0, "event-detection\n", "")
        if sub == "kill":
            self.status = "exited"
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class ProcessManagerDockerBackendTest(unittest.IsolatedAsyncioTestCase):
    """One-click launch against a containerized event_detection (docker backend)."""

    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA_DIR"] = self.temporary_directory.name
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ["PROCESS_RESTORE_ENABLED"] = "false"
        os.environ["EVENT_DETECTION_BACKEND"] = "docker"
        os.environ.pop("EVENT_DETECTION_CONTAINER", None)
        os.environ.pop("LAUNCH_TOKEN", None)
        os.environ.pop("AGENT_WS_TOKEN", None)
        os.environ.pop("EVENT_DETECTION_DIR", None)
        os.environ.pop("AGENT_DIR", None)
        self.event_dir = Path(self.temporary_directory.name) / "event_detection_new"
        self.event_dir.mkdir()
        os.environ["EVENT_DETECTION_DIR"] = str(self.event_dir)
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        os.environ.pop("DASHBOARD_DATA_DIR", None)
        os.environ.pop("EVENT_DETECTION_BACKEND", None)
        os.environ.pop("EVENT_DETECTION_CONTAINER", None)
        os.environ.pop("EVENT_DETECTION_DIR", None)
        self.temporary_directory.cleanup()

    def _manager(self):
        return self.client.server.app[PROCESS_MANAGER_KEY]

    @contextmanager
    def _docker_context(self, fake: FakeDocker):
        """Patch subprocess.run (docker CLI) and shutil.which (docker binary)."""
        with (
            patch("erban_dashboard_app.server.subprocess.run", side_effect=fake.run),
            patch("erban_dashboard_app.server.shutil.which", return_value="/usr/bin/docker"),
        ):
            yield

    async def test_event_detection_uses_docker_backend(self) -> None:
        manager = self._manager()
        detection = manager.processes["event_detection"]
        self.assertEqual(detection.backend, "docker")
        self.assertEqual(detection.container, "event-detection")
        # Agent stays on the host backend.
        self.assertEqual(manager.processes["agent"].backend, "host")

    async def test_custom_container_name(self) -> None:
        os.environ["EVENT_DETECTION_CONTAINER"] = "my-detector"
        client = TestClient(TestServer(create_app()))
        await client.start_server()
        try:
            manager = client.server.app[PROCESS_MANAGER_KEY]
            self.assertEqual(manager.processes["event_detection"].container, "my-detector")
        finally:
            await client.close()

    async def test_docker_start_without_docker_cli_is_400(self) -> None:
        with patch("erban_dashboard_app.server.shutil.which", return_value=None):
            response = await self.client.post("/api/processes/event_detection/start")
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertIn("docker", body["error"])

    async def test_docker_start_missing_container_is_400(self) -> None:
        fake = FakeDocker(exists=False)
        with self._docker_context(fake):
            response = await self.client.post("/api/processes/event_detection/start")
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertIn("docker-deploy.sh", body["error"])
        self.assertIn("不存在", body["error"])

    async def test_docker_start_failure_is_400_with_stderr(self) -> None:
        fake = FakeDocker()
        fake.start_rc = 125
        fake.start_stderr = "Error response from daemon"
        with self._docker_context(fake):
            response = await self.client.post("/api/processes/event_detection/start")
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertIn("Error response from daemon", body["error"])

    async def test_docker_start_stop_lifecycle(self) -> None:
        fake = FakeDocker()
        with self._docker_context(fake):
            started = await self.client.post("/api/processes/event_detection/start")
            self.assertEqual(started.status, 200)
            process = (await started.json())["process"]
            self.assertEqual(process["status"], "running")
            self.assertEqual(process["backend"], "docker")
            self.assertEqual(process["pid"], fake.pid)
            self.assertEqual(fake.compose_calls, 1)
            self.assertEqual(fake.start_calls, 0)

            # 容器已在运行 -> 拒绝再次启动
            again = await self.client.post("/api/processes/event_detection/start")
            self.assertEqual(again.status, 409)

            stopped = await self.client.post("/api/processes/event_detection/stop")
            self.assertEqual(stopped.status, 200)
            process = (await stopped.json())["process"]
            self.assertEqual(process["status"], "stopped")
            self.assertEqual(process["exit_code"], fake.exit_code)
            self.assertEqual(fake.stop_calls, 1)

    async def test_docker_start_falls_back_to_start_without_compose_file(self) -> None:
        # 没有 docker-compose.yml 时退回纯 docker start。
        no_compose_dir = Path(self.temporary_directory.name) / "legacy"
        no_compose_dir.mkdir()
        os.environ["EVENT_DETECTION_DIR"] = str(no_compose_dir)
        client = TestClient(TestServer(create_app()))
        await client.start_server()
        try:
            fake = FakeDocker()
            with self._docker_context(fake):
                response = await client.post("/api/processes/event_detection/start")
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["process"]["status"], "running")
            self.assertEqual(fake.start_calls, 1)
            self.assertEqual(fake.compose_calls, 0)
        finally:
            await client.close()

    async def test_docker_start_removes_legacy_non_compose_container(self) -> None:
        # 旧 deploy 脚本（docker create）创建的容器没有 compose 标签，
        # compose up 会撞名字冲突：应先 rm 再 compose up 重建。
        fake = FakeDocker()
        fake.compose_owned = False
        with self._docker_context(fake):
            response = await self.client.post("/api/processes/event_detection/start")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["process"]["status"], "running")
        self.assertEqual(fake.rm_calls, 1)
        self.assertEqual(fake.compose_calls, 1)
        self.assertEqual(fake.start_calls, 0)

    async def test_docker_start_keeps_compose_owned_container(self) -> None:
        # 已是 compose 管理的容器：不删除，直接 compose up。
        fake = FakeDocker()
        with self._docker_context(fake):
            response = await self.client.post("/api/processes/event_detection/start")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["process"]["status"], "running")
        self.assertEqual(fake.rm_calls, 0)
        self.assertEqual(fake.compose_calls, 1)

    async def test_docker_start_retries_compose_after_name_conflict(self) -> None:
        # compose 首次撞同名冲突 → 移除容器后重试一次并成功。
        fake = FakeDocker()
        fake.compose_conflict_once = True
        with self._docker_context(fake):
            response = await self.client.post("/api/processes/event_detection/start")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["process"]["status"], "running")
        self.assertEqual(fake.compose_calls, 2)
        self.assertEqual(fake.rm_calls, 1)

    async def test_docker_stop_idempotent(self) -> None:
        fake = FakeDocker()
        with self._docker_context(fake):
            response = await self.client.post("/api/processes/event_detection/stop")
        # 未运行 -> 幂等成功
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["process"]["status"], "stopped")

    async def test_docker_start_adopts_externally_running_container(self) -> None:
        fake = FakeDocker(initial="running")
        # 容器已由外部启动，但大屏尚未记录状态（status 仍是 stopped）。
        process = self._manager().processes["event_detection"]
        process.status = "stopped"
        process.pid = None
        with self._docker_context(fake):
            response = await self.client.post("/api/processes/event_detection/start")
        self.assertEqual(response.status, 200)
        adopted = (await response.json())["process"]
        self.assertEqual(adopted["status"], "running")
        self.assertEqual(adopted["pid"], fake.pid)
        # 接管后不应真的再 docker start / compose up。
        self.assertEqual(fake.start_calls, 0)
        self.assertEqual(fake.compose_calls, 0)


if __name__ == "__main__":
    unittest.main()
