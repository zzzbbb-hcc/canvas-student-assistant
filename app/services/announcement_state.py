"""Local-only read and important flags for Canvas announcements.

Only numeric identities and booleans reach the database; announcement content,
translations, links and credentials are not accepted or persisted here. These
flags intentionally do not sync with Canvas and survive clearing cached content.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.storage.database import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class AnnouncementState:
    is_read: bool = False
    is_starred: bool = False


class AnnouncementStateStore:
    """Persist flags while leaving connection locking to the SQLite gateway."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def get_states(self) -> dict[tuple[int, int], AnnouncementState]:
        """Load once for the whole announcement list; unknown IDs are unread."""

        return {
            identity: AnnouncementState(is_read, is_starred)
            for identity, (is_read, is_starred) in self.database.get_announcement_states().items()
        }

    def set_read(
        self, course_id: int, announcement_id: int, is_read: bool
    ) -> AnnouncementState:
        return AnnouncementState(
            *self.database.set_announcement_read(course_id, announcement_id, is_read)
        )

    def set_starred(
        self, course_id: int, announcement_id: int, is_starred: bool
    ) -> AnnouncementState:
        return AnnouncementState(
            *self.database.set_announcement_starred(course_id, announcement_id, is_starred)
        )
