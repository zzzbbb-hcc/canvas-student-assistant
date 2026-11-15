from __future__ import annotations

from threading import RLock
from typing import Any


class SessionCache:
    """Process-memory cache for non-sensitive display data only."""
    def __init__(self):
        self._values: dict[str, Any] = {}
        self._lock = RLock()

    def get(self, key: str, default=None):
        with self._lock:
            return self._values.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._values[key] = value

    def clear(self) -> int:
        """Clear process-memory display data and return the number removed."""
        with self._lock:
            count = len(self._values)
            self._values.clear()
            return count


session_cache = SessionCache()
