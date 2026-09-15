"""Ownership and lifecycle for the single active monitor task."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable

from .config import MonitorConfig
from .event_parser import parse_matching_event
from .monitor import encode_frame_jpeg
from .orchestrator import ContinuousFrameOrchestrator
from .publishers import EventPublishers
from .schemas import MonitorRequest
from .video_source import LatestFrameCapture


logger = logging.getLogger(__name__)


def _safe_error(operation: str, exc: BaseException) -> str:
    return f"{operation}_failed:{type(exc).__name__}"


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


@dataclass(frozen=True)
class TaskAccepted:
    task_id: str
    replaced_task_id: str | None
    event_name: str
    timeout_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {"status": "accepted", **asdict(self)}


@dataclass(frozen=True)
class TaskSnapshot:
    status: str
    task_id: str | None = None
    event_name: str | None = None
    timeout_seconds: float | None = None
    elapsed_seconds: float | None = None
    remaining_seconds: float | None = None
    inference_count: int = 0
    match_count: int = 0
    agent_push_count: int = 0
    dashboard_push_count: int = 0
    ended_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.status == "idle":
            return {"status": "idle"}
        return asdict(self)


@dataclass
class _ActiveTask:
    generation: int
    task_id: str
    request: MonitorRequest
    started_at: float
    deadline: float
    orchestrator: Any
    inference_count: int = 0
    match_count: int = 0
    agent_push_count: int = 0
    dashboard_push_count: int = 0


class MonitorTaskManager:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        capture: Any | None = None,
        publishers: Any | None = None,
        orchestrator_factory: Callable[[MonitorRequest], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._capture = capture or LatestFrameCapture(config)
        self._publishers = publishers or EventPublishers(config)
        self._orchestrator_factory = orchestrator_factory or (
            lambda request: ContinuousFrameOrchestrator(config, request)
        )
        self._clock = clock
        self._lock = asyncio.Lock()
        self._generation = 0
        self._active: _ActiveTask | None = None
        self._worker: asyncio.Task[None] | None = None
        self._capture_running = False
        self._last: TaskSnapshot | None = None
        self._last_error: str | None = None

    async def replace(self, request: MonitorRequest) -> TaskAccepted:
        try:
            orchestrator = self._orchestrator_factory(request)
        except Exception as exc:
            self._last_error = _safe_error("setup", exc)
            logger.error(
                "monitor task setup failed error_type=%s", type(exc).__name__
            )
            raise
        async with self._lock:
            previous = self._active
            replaced_task_id = previous.task_id if previous else None
            if not self._capture_running:
                try:
                    self._capture.start()
                except Exception as exc:
                    self._last_error = _safe_error("video_start", exc)
                    logger.error(
                        "monitor video capture failed to start error_type=%s",
                        type(exc).__name__,
                    )
                    raise
                self._capture_running = True
            self._generation += 1
            generation = self._generation
            old_worker, self._worker = self._worker, None
            self._active = None
            if old_worker is not None:
                old_worker.cancel()
            if previous is not None:
                self._last = self._terminal_snapshot(previous, "replaced")

            self._capture.clear()
            started_at = self._clock()
            task_id = uuid.uuid4().hex
            active = _ActiveTask(
                generation=generation,
                task_id=task_id,
                request=request,
                started_at=started_at,
                deadline=started_at + request.timeout_seconds,
                orchestrator=orchestrator,
            )
            self._active = active
            self._worker = asyncio.create_task(self._run(active))
            self._last_error = None
            logger.info("monitor task accepted task_id=%s event=%s", task_id, request.event_name)
            accepted = TaskAccepted(
                task_id=task_id,
                replaced_task_id=replaced_task_id,
                event_name=request.event_name,
                timeout_seconds=request.timeout_seconds,
            )
        if old_worker is not None:
            await asyncio.gather(old_worker, return_exceptions=True)
        return accepted

    async def stop(self) -> TaskSnapshot | None:
        async with self._lock:
            if self._active is None:
                return None
            snapshot = self._terminal_snapshot(self._active, "stopped")
            self._generation += 1
            worker, self._worker = self._worker, None
            self._active = None
            self._last = snapshot
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
            self._close_capture()
        if worker is not None and worker is not asyncio.current_task():
            await asyncio.gather(worker, return_exceptions=True)
        return snapshot

    def snapshot(self) -> TaskSnapshot:
        active = self._active
        return self._snapshot_active(active) if active else TaskSnapshot(status="idle")

    def last_snapshot(self) -> TaskSnapshot | None:
        return self._last

    def health(self) -> dict[str, Any]:
        capture_running = getattr(self._capture, "running", self._capture_running)
        capture_error = getattr(self._capture, "last_error", None)
        return {
            "capture_running": bool(capture_running),
            "last_error": capture_error or self._last_error,
        }

    def _snapshot_active(self, active: _ActiveTask) -> TaskSnapshot:
        now = self._clock()
        return TaskSnapshot(
            status="running",
            task_id=active.task_id,
            event_name=active.request.event_name,
            timeout_seconds=active.request.timeout_seconds,
            elapsed_seconds=round(max(0.0, now - active.started_at), 3),
            remaining_seconds=round(max(0.0, active.deadline - now), 3),
            inference_count=active.inference_count,
            match_count=active.match_count,
            agent_push_count=active.agent_push_count,
            dashboard_push_count=active.dashboard_push_count,
        )

    async def _run(self, active: _ActiveTask) -> None:
        after_sequence = 0
        terminal_status = "timed_out"
        terminal_error: str | None = None
        try:
            while self._is_current(active):
                remaining = active.deadline - self._clock()
                if remaining <= 0:
                    break
                frame = self._capture.latest(after_sequence, 0)
                if frame is None:
                    await asyncio.sleep(0.005)
                    continue
                after_sequence = frame.sequence
                if not self._is_current(active):
                    return
                try:
                    output = await self._run_until_deadline(
                        active.orchestrator.infer(frame), active.deadline
                    )
                except TimeoutError:
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = _safe_error("inference", exc)
                    logger.error(
                        "monitor inference failed task_id=%s error_type=%s",
                        active.task_id,
                        type(exc).__name__,
                    )
                    continue
                if not self._is_current(active):
                    return
                active.inference_count += 1
                active.orchestrator.record_response(output)
                event = parse_matching_event(output, active.request.event_name)
                if event is None:
                    continue
                active.match_count += 1
                try:
                    outcome = await self._publish_until_deadline(
                        self._publishers.publish_match(
                            event,
                            encode_frame_jpeg(frame),
                            active.task_id,
                            active.match_count,
                            frame.captured_at,
                        ),
                        active.deadline,
                    )
                except TimeoutError:
                    break
                if not self._is_current(active):
                    return
                active.agent_push_count += int(outcome.agent_succeeded)
                active.dashboard_push_count += int(outcome.dashboard_succeeded)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            terminal_status = "failed"
            terminal_error = _safe_error("processing", exc)
            self._last_error = terminal_error
            logger.error(
                "monitor task failed task_id=%s error_type=%s",
                active.task_id,
                type(exc).__name__,
            )
        finally:
            await self._expire_if_current(active, terminal_status, terminal_error)

    async def _run_until_deadline(self, operation: Any, deadline: float) -> Any:
        task = asyncio.create_task(operation)
        try:
            remaining = max(0.0, deadline - self._clock())
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task in done:
                return task.result()
            task.cancel()
            task.add_done_callback(_consume_task_result)
            raise TimeoutError
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(_consume_task_result)
            raise

    async def _publish_until_deadline(self, operation: Any, deadline: float) -> Any:
        remaining = max(0.0, deadline - self._clock())
        async with asyncio.timeout(remaining):
            return await operation

    def _is_current(self, active: _ActiveTask) -> bool:
        return (
            self._active is active
            and active.generation == self._generation
            and self._clock() < active.deadline
        )

    async def _expire_if_current(
        self, active: _ActiveTask, status: str, error: str | None
    ) -> None:
        async with self._lock:
            if self._active is active and active.generation == self._generation:
                self._last = self._terminal_snapshot(active, status, error)
                self._active = None
                self._worker = None
                self._close_capture()
                logger.info("monitor task ended task_id=%s status=%s", active.task_id, status)

    def _terminal_snapshot(
        self, active: _ActiveTask, status: str, error: str | None = None
    ) -> TaskSnapshot:
        snapshot = self._snapshot_active(active)
        values = asdict(snapshot)
        values.update(
            status=status,
            remaining_seconds=(
                0.0 if status == "timed_out" else snapshot.remaining_seconds
            ),
            ended_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            error=error,
        )
        return TaskSnapshot(**values)

    def _close_capture(self) -> None:
        if self._capture_running:
            self._capture.close()
            self._capture_running = False

    async def close(self) -> None:
        await self.stop()
        await self._publishers.close()
