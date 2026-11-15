"""TTL-aware SQLite cache for non-sensitive, read-only study data."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import BaseModel

from app.storage.database import CacheRecord, SQLiteDatabase


class CacheCategory(str, Enum):
    COURSES = "courses"
    ASSIGNMENTS = "assignments"
    ANNOUNCEMENTS = "announcements"
    ED_DISCUSSION = "ed_discussion"
    ED_LESSONS = "ed_lessons"


CACHE_TTLS: dict[str, timedelta] = {
    CacheCategory.COURSES.value: timedelta(hours=24),
    CacheCategory.ASSIGNMENTS.value: timedelta(hours=1),
    CacheCategory.ANNOUNCEMENTS.value: timedelta(minutes=30),
    CacheCategory.ED_DISCUSSION.value: timedelta(minutes=15),
    CacheCategory.ED_LESSONS.value: timedelta(hours=24),
}


class UnknownCacheCategory(ValueError):
    pass


def _category(value: CacheCategory | str) -> str:
    result = value.value if isinstance(value, CacheCategory) else str(value)
    if result not in CACHE_TTLS:
        raise UnknownCacheCategory(f"Unsupported cache category: {result}")
    return result


def _json_value(value: Any) -> Any:
    """Convert supported model values to safe JSON-compatible primitives."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Cached timestamps must include a timezone.")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported cache value type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class CachedValue:
    value: Any
    fetched_at: datetime
    expires_at: datetime


_MISSING = object()


class Cache:
    """A cache with fixed, category-specific TTLs and explicit refresh bypass."""

    def __init__(
        self,
        database: SQLiteDatabase | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database or SQLiteDatabase()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Cache clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)

    def set(
        self,
        category: CacheCategory | str,
        value: Any,
        *,
        key: str = "default",
    ) -> CachedValue:
        category_name = _category(category)
        fetched_at = self._now()
        expires_at = fetched_at + CACHE_TTLS[category_name]
        safe_value = _json_value(value)
        self.database.put_cache_entry(
            category_name, str(key), safe_value, fetched_at, expires_at
        )
        return CachedValue(safe_value, fetched_at, expires_at)

    def get_entry(
        self,
        category: CacheCategory | str,
        *,
        key: str = "default",
        allow_expired: bool = False,
    ) -> CachedValue | None:
        category_name = _category(category)
        record: CacheRecord | None = self.database.get_cache_entry(
            category_name, str(key)
        )
        if record is None:
            return None
        if not allow_expired and record.expires_at <= self._now():
            self.database.delete_cache_entry(category_name, str(key))
            return None
        return CachedValue(record.payload, record.fetched_at, record.expires_at)

    def get(
        self,
        category: CacheCategory | str,
        *,
        key: str = "default",
        default: Any = None,
        allow_expired: bool = False,
    ) -> Any:
        entry = self.get_entry(
            category, key=key, allow_expired=allow_expired
        )
        return entry.value if entry is not None else default

    def is_fresh(self, category: CacheCategory | str, *, key: str = "default") -> bool:
        return self.get_entry(category, key=key) is not None

    def get_or_fetch(
        self,
        category: CacheCategory | str,
        fetcher: Callable[[], Any],
        *,
        key: str = "default",
        refresh: bool = False,
    ) -> Any:
        """Return cached data, or call ``fetcher``; refresh always bypasses cache."""

        if not refresh:
            cached = self.get(category, key=key, default=_MISSING)
            if cached is not _MISSING:
                return cached
        fresh = fetcher()
        self.set(category, fresh, key=key)
        return fresh

    def invalidate(
        self, category: CacheCategory | str, *, key: str | None = None
    ) -> int:
        return self.database.delete_cache_entry(_category(category), key)

    def clear(self) -> int:
        return self.database.clear_cache()

    def purge_expired(self) -> int:
        return self.database.purge_expired_cache(self._now())
