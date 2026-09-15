import unittest
from types import SimpleNamespace

import numpy as np

from agent_monitor_server.config import MonitorConfig
from agent_monitor_server.orchestrator import ContinuousFrameOrchestrator
from agent_monitor_server.schemas import MonitorRequest
from agent_monitor_server.video_source import CapturedFrame


class FakeCompletions:
    def __init__(self) -> None:
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"event": null}'))]
        )


class ContinuousFrameOrchestratorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.completions = FakeCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))
        config = MonitorConfig(main_model="monitor-vlm", max_context_turns=2)
        request = MonitorRequest("raise_hand", "AGENT COMPLETE PROMPT", 60)
        self.orchestrator = ContinuousFrameOrchestrator(config, request, client=client)

    def frame(self, sequence: int) -> CapturedFrame:
        return CapturedFrame(
            sequence=sequence,
            image=np.zeros((4, 4, 3), dtype=np.uint8),
            captured_at=f"2026-08-31T00:00:0{sequence}+08:00",
            bgr_input=True,
        )

    async def test_uses_agent_prompt_verbatim_as_only_system_message(self) -> None:
        await self.orchestrator.infer(self.frame(1))

        request = self.completions.requests[-1]
        system_messages = [m for m in request["messages"] if m["role"] == "system"]
        self.assertEqual(
            system_messages,
            [{"role": "system", "content": "AGENT COMPLETE PROMPT"}],
        )
        self.assertEqual(request["model"], "monitor-vlm")

    async def test_disables_thinking_for_compatible_model_servers(self) -> None:
        client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))
        orchestrator = ContinuousFrameOrchestrator(
            MonitorConfig(disable_thinking=True),
            MonitorRequest("wave", "prompt", 30),
            client=client,
        )

        await orchestrator.infer(self.frame(1))

        self.assertEqual(
            self.completions.requests[-1]["extra_body"],
            {
                "enable_thinking": False,
                "chat_template_kwargs": {
                    "enable_thinking": False,
                    "enable_thinking_assert": False,
                },
            },
        )

    async def test_context_is_bounded_and_reset_removes_old_prompt_and_frames(self) -> None:
        for sequence in range(1, 5):
            output = await self.orchestrator.infer(self.frame(sequence))
            self.orchestrator.record_response(output)

        before_reset = self.completions.requests[-1]["messages"]
        self.assertLessEqual(len(before_reset), 1 + 2 * 2 + 1)

        self.orchestrator.reset(MonitorRequest("wave", "NEW PROMPT", 30))
        await self.orchestrator.infer(self.frame(5))
        after_reset = self.completions.requests[-1]["messages"]

        self.assertEqual(after_reset[0], {"role": "system", "content": "NEW PROMPT"})
        self.assertEqual(len(after_reset), 2)


if __name__ == "__main__":
    unittest.main()
