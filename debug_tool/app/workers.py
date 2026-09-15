from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class TaskSignals(QObject):
    success = Signal(object)
    failure = Signal(str)
    done = Signal()


class ApiTask(QRunnable):
    def __init__(self, action: Callable[[], Any]) -> None:
        super().__init__()
        self.action = action
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.success.emit(self.action())
        except Exception as exc:
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.done.emit()


class TaskRunner:
    def __init__(self) -> None:
        self.pool = QThreadPool.globalInstance()
        self._active: set[ApiTask] = set()

    def run(
        self,
        action: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_failure: Callable[[str], None],
    ) -> None:
        task = ApiTask(action)
        task.signals.success.connect(on_success)
        task.signals.failure.connect(on_failure)
        task.signals.done.connect(lambda t=task: self._active.discard(t))
        self._active.add(task)
        self.pool.start(task)
