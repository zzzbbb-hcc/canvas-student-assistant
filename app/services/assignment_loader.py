from app.models.assignment import Assignment
from app.services.canvas_client import CanvasClient, CanvasClientError
from app.storage.cache import Cache
from app.storage.session_cache import session_cache

_persistent_cache: Cache | None = None


def _cache() -> Cache:
    global _persistent_cache
    if _persistent_cache is None:
        _persistent_cache = Cache()
    return _persistent_cache


def cached_assignments(cache=None, *, allow_expired=True):
    """Load display-safe cached assignments without making a network request."""

    cache = cache or _cache()
    raw = cache.get(
        "assignments", default=[], allow_expired=allow_expired
    )
    assignments = [
        item if isinstance(item, Assignment) else Assignment.model_validate(item)
        for item in raw
    ]
    if assignments:
        session_cache.set("assignments", assignments)
    return assignments


def load_assignments(store, *, refresh=False, cache=None):
    cache = cache or _cache()

    def fetch():
        token = store.get_canvas_token()
        if token is None:
            raise CanvasClientError("No Canvas token is stored.")
        with CanvasClient(token) as client:
            assignments = client.get_upcoming_assignments()
            session_cache.set(
                "quiz_coverage_warnings", list(getattr(client, "quiz_coverage_warnings", []))
            )
            return assignments

    raw = None if refresh else cache.get("assignments")
    if raw is None:
        fresh = fetch()
        # Deadline views do not need rich HTML or user-specific Canvas URLs.
        # Omitting them from SQLite also prevents embedded signed resource
        # links from becoming long-lived cache data.
        cache.set(
            "assignments",
            [
                item.model_dump(
                    mode="json", exclude={"description", "html_url"}
                )
                if isinstance(item, Assignment)
                else Assignment.model_validate(item).model_dump(
                    mode="json", exclude={"description", "html_url"}
                )
                for item in fresh
            ],
        )
        raw = fresh
    assignments = [
        item if isinstance(item, Assignment) else Assignment.model_validate(item)
        for item in raw
    ]
    session_cache.set("assignments", assignments)
    return assignments
