import asyncio
import unittest

import numpy as np

from agent_monitor_server.config import MonitorConfig
from agent_monitor_server.publishers import PublishOutcome
from agent_monitor_server.schemas import MonitorRequest
from agent_monitor_server.task_manager import MonitorTaskManager
from agent_monitor_server.video_source import CapturedFrame


class FakeCapture:
    def __init__(self) -> None:
        self.started = 0
        self.cleared = 0
        self.closed = 0
        self.frames = []

    def start(self):
        self.started += 1

    def clear(self):
        self.cleared += 1
        self.frames.clear()

    def close(self):
        self.closed += 1

    def latest(self, after_sequence, timeout=0):
        for frame in self.frames:
            if frame.sequence > after_sequence:
                return frame
        return None

    @property
    def running(self):
        return self.started > self.closed


class FakeOrchestrator:
    def __init__(self, output='{"event": null}', gate=None) -> None:
        self.output = output
        self.gate = gate
        self.recorded = []

    async def infer(self, frame):
        if self.gate is not None:
            await self.gate.wait()
        return self.output

    def record_response(self, text):
        self.recorded.append(text)


class FakePublishers:
    def __init__(self) -> None:
        self.calls = []

    async def publish_match(self, event, image_bytes, task_id, sequence, detected_at):
        self.calls.append((event, task_id, sequence))
        return PublishOutcome(True, True)

    async def close(self):
        return None


def frame(sequence: int) -> CapturedFrame:
    return CapturedFrame(
        sequence,
        np.zeros((4, 4, 3), dtype=np.uint8),
        f"time-{sequence}",
        True,
    )


async def wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met")
        await asyncio.sleep(0.005)


class MonitorTaskManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.capture = FakeCapture()
        self.publishers = FakePublishers()
        self.orchestrators = []

    def make_manager(self, orchestrator_factory=None):
        def default_factory(request):
            orchestrator = FakeOrchestrator(
                f'{{"event":"{request.event_name}","desc":"matched"}}'
            )
            self.orchestrators.append(orchestrator)
            return orchestrator

        return MonitorTaskManager(
            MonitorConfig(frame_interval=0),
            capture=self.capture,
            publishers=self.publishers,
            orchestrator_factory=orchestrator_factory or default_factory,
        )

    async def test_task_starts_capture_and_every_matching_frame_is_published(self) -> None:
        manager = self.make_manager()
        accepted = await manager.replace(MonitorRequest("wave", "prompt", 10))
        self.capture.frames.extend([frame(1), frame(2)])

        await wait_until(lambda: len(self.publishers.calls) == 2)
        snapshot = manager.snapshot()

        self.assertEqual(self.capture.started, 1)
        self.assertEqual(snapshot.task_id, accepted.task_id)
        self.assertEqual(snapshot.match_count, 2)
        self.assertEqual([call[2] for call in self.publishers.calls], [1, 2])
        await manager.close()
        self.assertEqual(
            [task for task in asyncio.all_tasks() if task is not asyncio.current_task()],
            [],
        )

    async def test_replacement_discards_in_flight_old_result(self) -> None:
        gate = asyncio.Event()

        def factory(request):
            if request.event_name == "old":
                return FakeOrchestrator('{"event":"old","desc":"old"}', gate)
            return FakeOrchestrator('{"event": null}')

        manager = self.make_manager(factory)
        first = await manager.replace(MonitorRequest("old", "old prompt", 10))
        self.capture.frames.append(frame(1))
        await asyncio.sleep(0.02)

        second = await manager.replace(MonitorRequest("new", "new prompt", 10))
        gate.set()
        await asyncio.sleep(0.05)

        self.assertEqual(second.replaced_task_id, first.task_id)
        self.assertEqual(self.publishers.calls, [])
        self.assertEqual(self.capture.started, 1)
        await manager.close()

    async def test_expiry_stops_capture(self) -> None:
        manager = self.make_manager()
        await manager.replace(MonitorRequest("wave", "prompt", 0.03))

        await wait_until(lambda: self.capture.closed == 1)

        self.assertEqual(manager.snapshot().status, "idle")
        self.assertEqual(manager.last_snapshot().status, "timed_out")
        await manager.close()

    async def test_deadline_cancels_in_flight_inference(self) -> None:
        cancelled = asyncio.Event()

        class BlockingOrchestrator(FakeOrchestrator):
            async def infer(self, frame):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        manager = self.make_manager(lambda request: BlockingOrchestrator())
        await manager.replace(MonitorRequest("wave", "prompt", 0.03))
        self.capture.frames.append(frame(1))

        await wait_until(cancelled.is_set)

        self.assertEqual(manager.snapshot().status, "idle")
        self.assertEqual(manager.last_snapshot().status, "timed_out")
        await manager.close()

    async def test_stop_preserves_terminal_snapshot(self) -> None:
        manager = self.make_manager()
        accepted = await manager.replace(MonitorRequest("wave", "prompt", 10))

        stopped = await manager.stop()

        self.assertEqual(stopped.task_id, accepted.task_id)
        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(manager.last_snapshot().status, "stopped")
        self.assertIsNotNone(manager.last_snapshot().ended_at)
        await manager.close()

    async def test_unrecoverable_processing_error_is_reported(self) -> None:
        manager = self.make_manager()
        await manager.replace(MonitorRequest("wave", "prompt", 10))
        bad_frame = frame(1)
        object.__setattr__(bad_frame, "image", "not-an-image")
        self.capture.frames.append(bad_frame)

        await wait_until(lambda: manager.snapshot().status == "idle")

        terminal = manager.last_snapshot()
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.error, "processing_failed:error")
        self.assertEqual(manager.health()["last_error"], "processing_failed:error")
        await manager.close()

    async def test_deadline_does_not_wait_for_cancellation_resistant_inference(self) -> None:
        release = asyncio.Event()

        class ResistantOrchestrator(FakeOrchestrator):
            async def infer(self, frame):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return '{"event": null}'

        manager = self.make_manager(lambda request: ResistantOrchestrator())
        await manager.replace(MonitorRequest("wave", "prompt", 0.03))
        self.capture.frames.append(frame(1))

        await wait_until(lambda: manager.snapshot().status == "idle", timeout=0.3)

        self.assertEqual(manager.last_snapshot().status, "timed_out")
        release.set()
        await asyncio.sleep(0.02)
        await manager.close()

    async def test_factory_failure_keeps_existing_task_running(self) -> None:
        def factory(request):
            if request.event_name == "new":
                raise RuntimeError("secret model URL")
            return FakeOrchestrator('{"event": null}')

        manager = self.make_manager(factory)
        first = await manager.replace(MonitorRequest("old", "old prompt", 10))

        with self.assertRaises(RuntimeError):
            await manager.replace(MonitorRequest("new", "new prompt", 10))

        self.assertEqual(manager.snapshot().task_id, first.task_id)
        self.assertEqual(manager.health()["last_error"], "setup_failed:RuntimeError")
        self.assertNotIn("secret", manager.health()["last_error"])
        await manager.close()

    async def test_deadline_cancels_in_flight_publish_before_shutdown(self) -> None:
        cancelled = asyncio.Event()

        class BlockingPublishers(FakePublishers):
            async def publish_match(
                self, event, image_bytes, task_id, sequence, detected_at
            ):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        publishers = BlockingPublishers()
        manager = MonitorTaskManager(
            MonitorConfig(frame_interval=0),
            capture=self.capture,
            publishers=publishers,
            orchestrator_factory=lambda request: FakeOrchestrator(
                f'{{"event":"{request.event_name}","desc":"matched"}}'
            ),
        )
        await manager.replace(MonitorRequest("wave", "prompt", 0.03))
        self.capture.frames.append(frame(1))

        await wait_until(cancelled.is_set)

        self.assertEqual(manager.last_snapshot().status, "timed_out")
        await manager.close()
        self.assertEqual(
            [task for task in asyncio.all_tasks() if task is not asyncio.current_task()],
            [],
        )


if __name__ == "__main__":
    unittest.main()
