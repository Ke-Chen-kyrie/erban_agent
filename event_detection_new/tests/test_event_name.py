import unittest
from unittest.mock import patch

from webinfer import live_adapter
from webinfer.live_adapter import (
    AdapterConfig,
    StreamingInferAdapter,
    extract_event,
    extract_response_payload,
    normalize_model_output,
)


class EventNameExtractionTest(unittest.TestCase):
    def test_json_with_name(self) -> None:
        result = extract_event('{"event": "fall", "desc": "检测到跌倒", "name": "张三"}')
        self.assertEqual(result, ("fall", "检测到跌倒", "张三"))

    def test_json_without_name_defaults_empty(self) -> None:
        result = extract_event('{"event": "fall", "desc": "检测到跌倒"}')
        self.assertEqual(result, ("fall", "检测到跌倒", ""))

    def test_null_event_is_none(self) -> None:
        self.assertIsNone(extract_event('{"event": null}'))

    def test_normalize_keeps_name(self) -> None:
        out = normalize_model_output('{"event": "fall", "desc": "x", "name": "张三"}')
        self.assertEqual(out, '{"event": "fall", "desc": "x", "name": "张三"}')

    def test_normalize_omits_empty_name(self) -> None:
        out = normalize_model_output('{"event": "fall", "desc": "x"}')
        self.assertEqual(out, '{"event": "fall", "desc": "x"}')

    def test_response_payload_includes_name(self) -> None:
        payload = extract_response_payload('{"event": "fall", "desc": "x", "name": "张三"}')
        self.assertIsNotNone(payload)
        self.assertIn('"name": "张三"', payload)

    def test_response_payload_empty_name_omitted(self) -> None:
        payload = extract_response_payload('{"event": "fall", "desc": "x"}')
        self.assertIsNotNone(payload)
        self.assertNotIn("name", payload)


def _adapter_with_webhook() -> StreamingInferAdapter:
    adapter = object.__new__(StreamingInferAdapter)
    adapter.config = AdapterConfig(agent_webhook_url="http://example.test/event")
    return adapter


class AgentPushNameTest(unittest.IsolatedAsyncioTestCase):
    async def test_agent_description_includes_person_name(self) -> None:
        captured: dict = {}

        async def fake_post(url: str, payload: dict):
            captured["url"] = url
            captured["payload"] = payload
            return 200

        with patch.object(live_adapter, "_post_event_json", new=fake_post):
            await _adapter_with_webhook()._push_event_to_agent("fall", "检测到跌倒", "张三")

        self.assertEqual(captured["url"], "http://example.test/event")
        self.assertEqual(captured["payload"]["event_type"], "fall")
        self.assertEqual(captured["payload"]["description"], "检测到跌倒（人员：张三）")
        self.assertEqual(captured["payload"]["name"], "张三")

    async def test_agent_description_without_name_is_unchanged(self) -> None:
        captured: dict = {}

        async def fake_post(url: str, payload: dict):
            captured["payload"] = payload
            return 200

        with patch.object(live_adapter, "_post_event_json", new=fake_post):
            await _adapter_with_webhook()._push_event_to_agent("fall", "检测到跌倒", "")

        self.assertEqual(captured["payload"]["description"], "检测到跌倒")
        self.assertEqual(captured["payload"]["name"], "")


if __name__ == "__main__":
    unittest.main()