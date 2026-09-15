import unittest
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from agent_monitor_server.app import create_app
from agent_monitor_server.config import MonitorConfig
from agent_monitor_server.prompt_builder import build_monitor_prompt
from agent_monitor_server.task_manager import TaskAccepted, TaskSnapshot


class FakeExpander:
    def __init__(self) -> None:
        self.calls = []
        self.closed = False

    async def expand(self, request):
        self.calls.append(request)
        return build_monitor_prompt(request.event_name, request.prompt)

    async def close(self):
        self.closed = True


class FakeManager:
    def __init__(self) -> None:
        self.requests = []
        self.current = TaskSnapshot(status="idle")
        self.closed = False
        self.last = None

    async def replace(self, request):
        self.requests.append(request)
        self.current = TaskSnapshot(status="running", task_id="new-task", event_name=request.event_name)
        return TaskAccepted("new-task", "old-task", request.event_name, request.timeout_seconds)

    def snapshot(self):
        return self.current

    async def stop(self):
        stopped = self.current if self.current.status == "running" else None
        self.current = TaskSnapshot(status="idle")
        return stopped

    def last_snapshot(self):
        return self.last

    def health(self):
        return {"capture_running": False, "last_error": None}

    async def close(self):
        self.closed = True


class MonitorAppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = FakeManager()
        self.expander = FakeExpander()
        self.config = MonitorConfig(auth_token="token", max_timeout_seconds=60)
        self.client = TestClient(
            TestServer(create_app(self.config, self.manager, self.expander))
        )
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer token"}

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_create_returns_202_and_replacement_information(self) -> None:
        response = await self.client.post(
            "/monitor",
            json={"event_name": "wave", "prompt": "complete", "timeout_seconds": 30},
            headers=self.headers,
        )
        body = await response.json()

        self.assertEqual(response.status, 202)
        self.assertEqual(body["task_id"], "new-task")
        self.assertEqual(body["replaced_task_id"], "old-task")
        self.assertNotIn("prompt", body)

    async def test_create_expands_task_before_handing_to_manager(self) -> None:
        response = await self.client.post(
            "/monitor",
            json={
                "event_name": "drinking_water",
                "instruction": "监测画面中的人有没有喝水",
                "timeout_seconds": 30,
            },
            headers=self.headers,
        )
        body = await response.json()

        self.assertEqual(response.status, 202)
        self.assertEqual(body["event_name"], "drinking_water")
        self.assertNotIn("instruction", body)
        self.assertNotIn("prompt", body)
        self.assertEqual(
            self.expander.calls[-1].prompt, "监测画面中的人有没有喝水"
        )
        self.assertIn("只有存在充分视觉证据", self.manager.requests[-1].prompt)

    async def test_invalid_request_returns_400_without_calling_manager(self) -> None:
        response = await self.client.post(
            "/monitor",
            json={"event_name": "bad label", "prompt": "complete", "timeout_seconds": 30},
            headers=self.headers,
        )
        body = await response.json()

        self.assertEqual(response.status, 400)
        self.assertEqual(body["field"], "event_name")
        self.assertEqual(self.manager.requests, [])

    async def test_monitor_routes_require_token_but_health_does_not(self) -> None:
        unauthorized = await self.client.get("/monitor/current")
        health = await self.client.get("/health")

        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(health.status, 200)
        self.assertEqual((await health.json())["task_status"], "idle")
        self.assertFalse((await health.json())["capture_running"])

    async def test_idle_current_includes_last_terminal_task(self) -> None:
        self.manager.last = TaskSnapshot(
            status="timed_out", task_id="old-task", event_name="wave"
        )

        current = await self.client.get("/monitor/current", headers=self.headers)
        body = await current.json()

        self.assertEqual(body["status"], "idle")
        self.assertEqual(body["last_task"]["status"], "timed_out")
        self.assertEqual(body["last_task"]["task_id"], "old-task")

    async def test_current_and_delete_expose_task_lifecycle(self) -> None:
        await self.client.post(
            "/monitor",
            json={"event_name": "wave", "prompt": "complete", "timeout_seconds": 30},
            headers=self.headers,
        )
        current = await self.client.get("/monitor/current", headers=self.headers)
        stopped = await self.client.delete("/monitor", headers=self.headers)

        self.assertEqual((await current.json())["status"], "running")
        self.assertEqual((await stopped.json())["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
