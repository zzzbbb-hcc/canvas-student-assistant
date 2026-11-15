from datetime import UTC, datetime

from app.models.assignment import Assignment
from app.services.assignment_loader import load_assignments
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase


class Store:
    def get_canvas_token(self):
        return "dummy"


class FakeClient:
    def __init__(self, _token):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def get_upcoming_assignments(self):
        return [
            Assignment(
                assignment_id=1,
                course_id=2,
                course_name="Course",
                title="Assignment",
                due_at=datetime(2026, 10, 20, tzinfo=UTC),
                description='<img src="https://example.invalid/file?token=signed-value">',
                html_url="https://canvas.example/assignment?session=signed-value",
                submission_status="unsubmitted",
            )
        ]


def test_persistent_assignment_cache_omits_rich_html_and_user_urls(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.assignment_loader.CanvasClient", FakeClient)
    database = SQLiteDatabase(tmp_path / "cache.sqlite3")
    cache = Cache(database)
    try:
        live = load_assignments(Store(), refresh=True, cache=cache)
        cached = cache.get("assignments")
        assert live[0].description is not None
        assert "description" not in cached[0]
        assert "html_url" not in cached[0]
        assert "signed-value" not in str(cached)
    finally:
        database.close()
