import base64
import unittest

from agent_monitor_server.config import MonitorConfig
from agent_monitor_server.publishers import EventPublishers
from agent_monitor_server.schemas import DetectedEvent


class EventPublishersTest(unittest.IsolatedAsyncioTestCase):
    async def test_builds_agent_and_dashboard_payloads(self) -> None:
        calls = []

        async def post_json(url, payload):
            calls.append((url, payload))
            return True

        config = MonitorConfig(
            agent_webhook_url="http://agent/event",
            system_event_url="http://dashboard/system_event",
        )
        publishers = EventPublishers(config, post_json=post_json)
        outcome = await publishers.publish_match(
            DetectedEvent("raise_hand", "检测到举手", "张三"),
            image_bytes=b"jpeg",
            task_id="task-1",
            sequence=3,
            detected_at="2026-08-31T12:00:00+08:00",
        )

        self.assertTrue(outcome.agent_succeeded)
        self.assertTrue(outcome.dashboard_succeeded)
        by_url = dict(calls)
        action = by_url["http://agent/event"]["actions"][0]
        self.assertEqual(action["event_name"], "raise_hand")
        self.assertEqual(action["description"], "检测到举手（人员：张三）")
        self.assertEqual(action["task_id"], "task-1")
        self.assertEqual(action["image_base64"], base64.b64encode(b"jpeg").decode("ascii"))
        dashboard = by_url["http://dashboard/system_event"]
        self.assertEqual(dashboard["event"], "raise_hand")
        self.assertEqual(dashboard["level"], "alert")
        self.assertEqual(dashboard["sequence"], 3)

    async def test_destination_failure_is_isolated(self) -> None:
        async def post_json(url, payload):
            if "agent" in url:
                raise RuntimeError("agent unavailable")
            return True

        publishers = EventPublishers(
            MonitorConfig(
                agent_webhook_url="http://agent/event",
                system_event_url="http://dashboard/system_event",
            ),
            post_json=post_json,
        )
        outcome = await publishers.publish_match(
            DetectedEvent("wave", "挥手", ""), b"jpeg", "task-1", 1, "now"
        )

        self.assertFalse(outcome.agent_succeeded)
        self.assertTrue(outcome.dashboard_succeeded)


if __name__ == "__main__":
    unittest.main()
