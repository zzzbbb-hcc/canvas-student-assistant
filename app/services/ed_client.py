"""Safe boundary for the currently unavailable Ed Discussion API.

Ed exposes an API-token screen to some accounts, but its public documentation
does not currently define a stable endpoint/authentication contract.  Keeping
the unsupported state in one service prevents UI code from quietly falling
back to guessed, reverse-engineered routes.
"""

from __future__ import annotations

from typing import NoReturn


UNSUPPORTED_API = "UNSUPPORTED_API"


class EdClientError(RuntimeError):
    """Base error safe to display without request or credential details."""


class EdUnsupportedAPI(EdClientError):
    code = UNSUPPORTED_API


class EdClient:
    """Read-only Ed client placeholder with a deliberately closed boundary."""

    unsupported_reason = (
        "Ed Discussion API access is not available for this account. "
        "No official endpoint contract has been confirmed."
    )

    def __init__(self, token: str | None = None) -> None:
        # Keep the token only in process memory. It is never logged or persisted.
        self._token = token

    def close(self) -> None:
        self._token = None

    def __enter__(self) -> "EdClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _unsupported(self) -> NoReturn:
        raise EdUnsupportedAPI(self.unsupported_reason)

    def test_connection(self):
        self._unsupported()

    def get_current_user(self):
        self._unsupported()

    def get_courses(self):
        self._unsupported()

    def get_threads(self, course_id):
        self._unsupported()

    def get_thread(self, course_id, thread_id):
        self._unsupported()

    def get_thread_comments(self, course_id, thread_id):
        self._unsupported()

    def search_threads(self, course_id, keyword):
        self._unsupported()

    def get_recent_threads(self, course_id):
        self._unsupported()
