from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from app.services.canvas_client import CanvasClientError


class CanvasWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, operation: Callable[[], Any]) -> None:
        super().__init__()
        self._operation = operation

    @Slot()
    def run(self) -> None:
        try:
            self.succeeded.emit(self._operation())
        except CanvasClientError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit("Operation failed.")
        finally:
            self.finished.emit()


def is_thread_running(thread) -> bool:
    """Return False for a QThread whose underlying C++ object was deleted."""
    if thread is None:
        return False
    try:
        return thread.isRunning()
    except RuntimeError:
        return False
