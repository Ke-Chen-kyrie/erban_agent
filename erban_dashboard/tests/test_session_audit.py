import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from main import SESSION_AUDIT_KEY, create_app
from erban_dashboard_app.session_audit import SessionAuditLog


class SessionAuditLogTest(unittest.TestCase):
    def test_each_logger_uses_a_new_jsonl_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = SessionAuditLog(directory)
            second = SessionAuditLog(directory)

            self.assertNotEqual(first.path, second.path)
            self.assertTrue(first.path.is_file())
            self.assertTrue(second.path.is_file())
            self.assertCountEqual(list(Path(directory).glob("dashboard_*.jsonl")), [first.path, second.path])

    def test_append_writes_one_valid_json_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = SessionAuditLog(directory)

            audit.append("chat_message", {"role": "user", "content": "您好"})
            audit.append("events_cleared")

            records = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["type"] for record in records], ["chat_message", "events_cleared"])
            self.assertEqual(records[0]["payload"], {"role": "user", "content": "您好"})
            self.assertEqual(records[1]["payload"], {})
            self.assertTrue(all(record["timestamp"] for record in records))

    def test_append_drops_embedded_media_but_keeps_safe_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = SessionAuditLog(directory)
            audit.append(
                "system_event",
                {
                    "event": "fall",
                    "raw": b"jpeg bytes",
                    "image": "AAAA",
                    "images": [
                        "AAAA",
                        "data:image/jpeg;base64,AAAA",
                        "https://example.test/fall.jpg",
                        "/recordings/fall.png",
                    ],
                    "nested": {"frame": "A" * 400, "description": "检测到跌倒"},
                },
            )

            record = json.loads(audit.path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["payload"],
                {
                    "event": "fall",
                    "images": ["https://example.test/fall.jpg", "/recordings/fall.png"],
                    "nested": {"description": "检测到跌倒"},
                },
            )


class SessionAuditApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["DASHBOARD_CAPTURE_ENABLED"] = "false"
        os.environ["PROCESS_RESTORE_ENABLED"] = "false"
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.app = create_app(data_dir=self.temporary_directory.name)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temporary_directory.cleanup()

    def records(self) -> list[dict]:
        path = self.app[SESSION_AUDIT_KEY].path
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    async def test_system_event_is_audited_without_embedded_images(self) -> None:
        response = await self.client.post(
            "/system_event",
            json={
                "event": "fall",
                "description": "检测到跌倒",
                "images": ["https://example.test/fall.jpg", "data:image/jpeg;base64,AAAA"],
                "videos": ["/recordings/fall.mp4"],
            },
        )

        self.assertEqual(response.status, 200)
        record = self.records()[-1]
        self.assertEqual(record["type"], "system_event")
        self.assertEqual(record["payload"]["event"], "fall")
        self.assertEqual(record["payload"]["images"], ["https://example.test/fall.jpg"])
        self.assertEqual(record["payload"]["videos"], ["/recordings/fall.mp4"])
        self.assertNotIn("base64", json.dumps(record))

    async def test_system_event_carries_person_name(self) -> None:
        response = await self.client.post(
            "/system_event",
            json={"event": "fall", "description": "检测到跌倒", "name": "张三"},
        )
        self.assertEqual(response.status, 200)

        state = await self.client.get("/api/state")
        body = await state.json()
        events = body["events"]
        self.assertEqual(events[-1]["event"], "fall")
        self.assertEqual(events[-1]["name"], "张三")
        self.assertEqual(events[-1]["description"], "检测到跌倒")

    async def test_system_event_without_name_defaults_empty(self) -> None:
        response = await self.client.post(
            "/system_event",
            json={"event": "fall", "description": "检测到跌倒"},
        )
        self.assertEqual(response.status, 200)

        state = await self.client.get("/api/state")
        body = await state.json()
        self.assertEqual(body["events"][-1]["name"], "")

    async def test_completed_chat_message_is_audited(self) -> None:
        response = await self.client.post(
            "/chat_message",
            json={"role": "user", "content": "请查看现场"},
        )

        self.assertEqual(response.status, 200)
        record = self.records()[-1]
        self.assertEqual(record["type"], "chat_message")
        self.assertEqual(record["payload"]["role"], "user")
        self.assertEqual(record["payload"]["content"], "请查看现场")

    async def test_clear_events_appends_an_audit_marker(self) -> None:
        await self.client.post(
            "/system_event",
            json={"event": "fall", "description": "检测到跌倒"},
        )

        response = await self.client.post("/api/events/clear")

        self.assertEqual(response.status, 200)
        self.assertEqual(self.records()[-1]["type"], "events_cleared")
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(state["events"], [])

    async def test_new_app_does_not_replay_previous_run(self) -> None:
        await self.client.post(
            "/system_event",
            json={"event": "fall", "description": "检测到跌倒"},
        )
        first_path = self.app[SESSION_AUDIT_KEY].path

        second_app = create_app(data_dir=self.temporary_directory.name)
        second_client = TestClient(TestServer(second_app))
        await second_client.start_server()
        try:
            state = await (await second_client.get("/api/state")).json()
            self.assertEqual(state["events"], [])
            self.assertEqual(state["messages"], [])
            self.assertNotEqual(second_app[SESSION_AUDIT_KEY].path, first_path)
        finally:
            await second_client.close()

    async def test_agent_stream_completion_is_audited(self) -> None:
        agent = await self.client.ws_connect("/agent/ws")
        await agent.receive_json()
        await agent.send_json({"type": "chat_start", "message_id": "audit-reply", "role": "agent"})
        await agent.receive_json()
        await agent.send_json({"type": "chat_delta", "message_id": "audit-reply", "delta": "已完成"})
        await agent.send_json({"type": "chat_end", "message_id": "audit-reply"})
        await agent.receive_json()
        await agent.close()

        record = self.records()[-1]
        self.assertEqual(record["type"], "agent_message_completed")
        self.assertEqual(record["payload"]["content"], "已完成")

    async def test_render_messages_and_interruption_are_audited(self) -> None:
        render = await self.client.ws_connect("/render")
        await render.receive_json()
        await render.send_json({"type": "user_speech", "text": "请回应"})
        await render.receive_json()
        await render.send_json({"type": "agent_response_start"})
        await render.receive_json()
        await render.send_json({"type": "agent_token", "text": "正在", "index": 0})
        await render.receive_json()
        await render.send_json({"type": "agent_response_interrupted", "partial_text": "正在"})
        await render.receive_json()
        await render.close()

        records = self.records()
        self.assertEqual([record["type"] for record in records], ["chat_message", "agent_message_interrupted"])
        self.assertEqual(records[0]["payload"]["content"], "请回应")
        self.assertEqual(records[1]["payload"]["content"], "正在")

    async def test_render_system_event_is_audited(self) -> None:
        render = await self.client.ws_connect("/render")
        await render.receive_json()
        await render.send_json(
            {"type": "system_event", "event_name": "fall", "description": "检测到跌倒"}
        )
        await render.receive_json()
        await render.close()

        record = self.records()[-1]
        self.assertEqual(record["type"], "system_event")
        self.assertEqual(record["payload"]["event"], "fall")


if __name__ == "__main__":
    unittest.main()
