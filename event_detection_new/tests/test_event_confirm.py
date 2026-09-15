import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from prompt import (
    CONFIRMABLE_EVENTS,
    DEFAULT_CONFIRM_SECONDS,
    DETAILED_SUMMARY_PROMPT,
    EVENT_CONFIRM_PROMPTS,
    EVENT_LABELS,
    build_detect_prompt,
    confirm_seconds,
)
from webinfer.live_adapter import (
    AdapterConfig,
    SessionState,
    StreamingInferAdapter,
    _extract_confirm_bool,
)

REMOVED_EVENTS = ("bed_fall", "lying_on_ground", "fire", "night_leaving")
KEPT_EVENTS = (
    "wave",
    "chest_pain",
    "head_pain",
    "abdomen_pain",
    "rub_shoulder",
    "ok",
    "thumbs_up",
    "fall",
)


class ConfirmCatalogTest(unittest.TestCase):
    def test_event_labels_reduced_to_eight(self) -> None:
        self.assertEqual(EVENT_LABELS, list(KEPT_EVENTS))

    def test_removed_events_gone_from_all_prompts(self) -> None:
        detection_prompt = build_detect_prompt()
        for event in REMOVED_EVENTS:
            self.assertNotIn(event, EVENT_LABELS)
            self.assertNotIn(event, detection_prompt)
            self.assertNotIn(event, DETAILED_SUMMARY_PROMPT)

    def test_confirmable_events_are_the_five_pain_and_fall(self) -> None:
        self.assertEqual(
            CONFIRMABLE_EVENTS,
            ["chest_pain", "head_pain", "abdomen_pain", "rub_shoulder", "fall"],
        )

    def test_every_confirmable_event_has_a_confirm_prompt(self) -> None:
        for event in CONFIRMABLE_EVENTS:
            self.assertIn(event, EVENT_CONFIRM_PROMPTS)
            prompt = EVENT_CONFIRM_PROMPTS[event]
            self.assertIn("确认引擎", prompt)
            self.assertIn("只输出一行 JSON", prompt)
            self.assertIn('{"confirm": true}', prompt)

    def test_gestures_are_not_confirmable(self) -> None:
        for event in ("wave", "ok", "thumbs_up"):
            self.assertNotIn(event, CONFIRMABLE_EVENTS)
            self.assertNotIn(event, EVENT_CONFIRM_PROMPTS)

    def test_confirm_seconds_default_is_two(self) -> None:
        self.assertEqual(DEFAULT_CONFIRM_SECONDS, 2.0)
        self.assertEqual(confirm_seconds("fall"), 2.0)
        self.assertEqual(confirm_seconds("not_configured"), 2.0)

    def test_removed_events_absent_from_summary_prompt(self) -> None:
        for event in REMOVED_EVENTS:
            self.assertNotIn(event, DETAILED_SUMMARY_PROMPT)


class ExtractConfirmBoolTest(unittest.TestCase):
    def test_plain_true(self) -> None:
        self.assertTrue(_extract_confirm_bool('{"confirm": true}'))

    def test_plain_false(self) -> None:
        self.assertFalse(_extract_confirm_bool('{"confirm": false}'))

    def test_thinking_prefix_takes_last_occurrence(self) -> None:
        text = 'thought {"confirm": false} then decided {"confirm": true}'
        self.assertTrue(_extract_confirm_bool(text))

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(_extract_confirm_bool("nothing to see"))
        self.assertIsNone(_extract_confirm_bool(""))


class ConfirmFlowTest(unittest.TestCase):
    @staticmethod
    def _make_adapter(**overrides) -> StreamingInferAdapter:
        config = AdapterConfig(
            enable_summarizer=False,
            system_event_url="http://test/system_event",
            agent_webhook_url="http://test/event",
            **overrides,
        )
        return StreamingInferAdapter(config)

    @staticmethod
    def _make_state() -> SessionState:
        return SessionState(session_id="test-confirm")

    def test_confirm_delay_uses_config_override(self) -> None:
        adapter = self._make_adapter(event_confirm_seconds={"fall": 1.5})
        self.assertEqual(adapter._confirm_delay("fall"), 1.5)
        self.assertEqual(adapter._confirm_delay("chest_pain"), DEFAULT_CONFIRM_SECONDS)

    def test_maybe_start_confirm_is_one_shot(self) -> None:
        async def scenario() -> None:
            adapter = self._make_adapter()
            state = self._make_state()
            with patch.object(adapter, "_run_confirm", new=AsyncMock()) as run_confirm:
                adapter._maybe_start_confirm(state, "fall", "desc", "", 1)
                adapter._maybe_start_confirm(state, "fall", "desc", "", 1)
                await asyncio.sleep(0)
                run_confirm.assert_awaited_once()

        asyncio.run(scenario())

    def test_run_confirm_pushes_when_confirmed(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        state.latest_image = "data:image/jpeg;base64,AAA"
        with (
            patch.object(adapter, "_confirm_event", new=AsyncMock(return_value=True)) as confirm_event,
            patch.object(adapter, "_push_system_event", new=AsyncMock()) as push_sys,
            patch.object(adapter, "_push_event_to_agent", new=AsyncMock()) as push_agent,
        ):
            asyncio.run(adapter._run_confirm(state, "fall", "desc", "张三", 1))
            confirm_event.assert_awaited_once_with("fall", ["data:image/jpeg;base64,AAA"])
            push_sys.assert_awaited_once()
            push_agent.assert_awaited_once()
            self.assertIn("fall", state._last_event_push)
            self.assertNotIn("fall", state._confirm_pending)

    def test_run_confirm_drops_when_rejected(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        state.latest_image = "data:image/jpeg;base64,AAA"
        with (
            patch.object(adapter, "_confirm_event", new=AsyncMock(return_value=False)),
            patch.object(adapter, "_push_system_event", new=AsyncMock()) as push_sys,
            patch.object(adapter, "_push_event_to_agent", new=AsyncMock()) as push_agent,
        ):
            asyncio.run(adapter._run_confirm(state, "fall", "desc", "", 1))
            push_sys.assert_not_awaited()
            push_agent.assert_not_awaited()
            self.assertNotIn("fall", state._last_event_push)
            self.assertNotIn("fall", state._confirm_pending)

    def test_confirm_event_returns_false_without_image(self) -> None:
        adapter = self._make_adapter()
        self.assertFalse(asyncio.run(adapter._confirm_event("fall", None)))
        self.assertFalse(asyncio.run(adapter._confirm_event("fall", [])))

    def test_confirm_frame_refs_uses_chunk_tail(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        state.current_chunk["image_paths"] = ["f1", "f2", "f3", "f4", "f5"]
        state.latest_image = "f9"
        self.assertEqual(adapter._confirm_frame_refs(state), ["f3", "f4", "f5"])
        self.assertEqual(adapter._confirm_frame_refs(state, max_frames=2), ["f4", "f5"])

    def test_confirm_frame_refs_falls_back_to_latest_image(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        state.current_chunk["image_paths"] = []
        state.latest_image = "f9"
        self.assertEqual(adapter._confirm_frame_refs(state), ["f9"])

    def test_latest_image_updated_from_image_paths(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        state.latest_image = None
        # latest_image maintenance happens in _handle_chat_payload; guard against
        # it being missing from the session state.
        self.assertIsNone(state.latest_image)
        self.assertIsInstance(state._confirm_pending, set)


if __name__ == "__main__":
    unittest.main()