"""Keep widget tests deterministic and independent of bundled native models."""

import pytest


@pytest.fixture(autouse=True)
def isolate_ui_history_database(monkeypatch):
    """Widget tests must never add fake conversations to a user's history."""
    from app.storage.database import SQLiteDatabase
    from app.ui import ai_chat, main_window
    from app.services import chatgpt_connection

    class IsolatedVault:
        def __init__(self, *_args, **_kwargs):
            self.record = {"profiles": {}}

        def load(self):
            from copy import deepcopy
            return deepcopy(self.record)

        def save(self, record):
            from copy import deepcopy
            self.record = deepcopy(record)

    monkeypatch.setattr(ai_chat, "SQLiteDatabase", lambda: SQLiteDatabase(":memory:"), raising=False)
    monkeypatch.setattr(main_window, "SQLiteDatabase", lambda: SQLiteDatabase(":memory:"))
    monkeypatch.setattr(chatgpt_connection, "ChatGPTVault", IsolatedVault)


@pytest.fixture(autouse=True)
def no_native_announcement_translation(monkeypatch):
    """Only explicitly injected fakes may translate during widget unit tests."""
    from app.ui import announcements

    class UnavailableTranslator:
        def is_available(self):
            return False

        def translate(self, _source):
            raise RuntimeError("Offline translation is disabled in unit tests")

    monkeypatch.setattr(announcements, "offline_translation", UnavailableTranslator())
