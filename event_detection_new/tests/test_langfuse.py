import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from webinfer.live_adapter import AdapterConfig, SessionState, StreamingInferAdapter


class LangfuseRecordTest(unittest.TestCase):
    @staticmethod
    def _make_adapter(**overrides) -> StreamingInferAdapter:
        config = AdapterConfig(enable_summarizer=False, **overrides)
        return StreamingInferAdapter(config)

    @staticmethod
    def _make_state() -> SessionState:
        return SessionState(session_id="test-langfuse")

    def test_disabled_config_skips_event_marking(self) -> None:
        adapter = self._make_adapter(langfuse_chunk_record_enabled=False)
        state = self._make_state()
        with patch.object(adapter, "_langfuse_record_available", return_value=False):
            adapter._mark_langfuse_event_pushed(state, 1, "fall", "desc", "")
        self.assertEqual(state.langfuse_pushed_events, {})

    def test_mark_event_pushed_records_for_chunk(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        with patch.object(adapter, "_langfuse_record_available", return_value=True):
            adapter._mark_langfuse_event_pushed(state, 1, "fall", "跌倒", "张三")
        events = state.langfuse_pushed_events.get(1)
        self.assertIsNotNone(events)
        self.assertEqual(events[0]["label"], "fall")
        self.assertEqual(events[0]["desc"], "跌倒")
        self.assertEqual(events[0]["name"], "张三")

    def test_record_flushed_chunk_writes_trace(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        state.langfuse_pushed_events[1] = [{"label": "fall", "desc": "跌倒", "name": "张三"}]
        events = state.langfuse_pushed_events[1]
        with (
            patch.object(adapter, "_langfuse_record_available", return_value=True),
            patch.object(adapter, "_write_event_chunk_trace") as write_trace,
        ):
            asyncio.run(adapter._record_flushed_chunk_if_event(state))
            write_trace.assert_called_once_with(state, 1, events)

    def test_flushed_chunk_without_events_skips_trace(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        with (
            patch.object(adapter, "_langfuse_record_available", return_value=True),
            patch.object(adapter, "_write_event_chunk_trace") as write_trace,
        ):
            asyncio.run(adapter._record_flushed_chunk_if_event(state))
            write_trace.assert_not_called()

    def test_confirm_passed_marks_event(self) -> None:
        adapter = self._make_adapter()
        state = self._make_state()
        state.latest_image = "data:image/jpeg;base64,AAA"
        with (
            patch.object(adapter, "_confirm_event", new=AsyncMock(return_value=True)),
            patch.object(adapter, "_push_system_event", new=AsyncMock()),
            patch.object(adapter, "_push_event_to_agent", new=AsyncMock()),
            patch.object(adapter, "_langfuse_record_available", return_value=True),
        ):
            asyncio.run(adapter._run_confirm(state, "fall", "desc", "张三", 1))
        self.assertEqual(state.langfuse_pushed_events.get(1)[0]["label"], "fall")


if __name__ == "__main__":
    unittest.main()