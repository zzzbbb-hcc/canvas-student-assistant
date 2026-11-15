"""Aggregate course announcements; rich content never leaves process memory."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.models.announcement import Announcement, sort_announcements
from app.models.course import Course
from app.services.canvas_client import CanvasClient, CanvasClientError
from app.storage.session_cache import session_cache


def _courses(cache):
    raw = session_cache.get("courses")
    if raw is None and cache is not None:
        raw = cache.get("courses", default=[], allow_expired=True)
    result = []
    for value in raw or []:
        try:
            result.append(value if isinstance(value, Course) else Course.model_validate(value))
        except (ValueError, TypeError):
            continue
    return result


def _normalize(raw, course):
    result = []
    for value in raw or []:
        try:
            item = value if isinstance(value, Announcement) else Announcement.from_canvas(value, course)
            if item.course_id == course.id:
                result.append(item)
        except (ValueError, TypeError, KeyError):
            continue
    return result


def _cached_course(course, cache):
    raw = session_cache.get(f"announcements:{course.id}")
    if raw is None and cache is not None:
        raw = cache.get("announcements", key=f"course:{course.id}", default=None, allow_expired=True)
    if raw is None:
        raw = [item for item in session_cache.get("announcements:all", []) or []
               if isinstance(item, Announcement) and item.course_id == course.id]
    return _normalize(raw, course)


def cached_announcements(cache=None):
    """Return known announcements immediately without retrieving credentials."""
    combined = list(session_cache.get("announcements:all", []) or [])
    for course in _courses(cache):
        # A known empty per-course result is authoritative too, not a reason
        # to resurrect a stale item from a previous aggregate snapshot.
        combined = [item for item in combined if not isinstance(item, Announcement) or item.course_id != course.id]
        combined.extend(_cached_course(course, cache))
    return sort_announcements(item for item in combined if isinstance(item, Announcement))


def load_announcements(store, *, refresh=True, cache=None):
    """Fetch only announcement/course metadata, with at most four course jobs."""
    if not refresh:
        known = cached_announcements(cache)
        if known:
            return known
    token = store.get_canvas_token()
    if not token:
        raise CanvasClientError("No Canvas token is stored.")
    courses = _courses(cache)
    if not courses:
        with CanvasClient(token) as client:
            courses = client.get_courses()
        session_cache.set("courses", courses)
    warnings = []

    def fetch_course(course):
        try:
            with CanvasClient(token) as client:
                raw = client.get_announcements(course.id)
            result = _normalize(raw, course)
            # Course details consume dictionaries too. Keep their original
            # readonly metadata shape, only in this process's session cache.
            session_cache.set(f"announcements:{course.id}", raw)
            return result, None
        except CanvasClientError:
            prior = _cached_course(course, cache)
            label = course.course_code or course.name
            return prior, f"{label}：公告暂不可读" + ("，保留已缓存公告。" if prior else "，此课程公告可能缺失。")

    result = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(courses)))) as pool:
        for items, warning in pool.map(fetch_course, courses):
            result.extend(items)
            if warning:
                warnings.append(warning)
    session_cache.set("announcement_coverage_warnings", warnings)
    if warnings and len(warnings) == len(courses) and not result:
        # Never replace an existing readable view with an empty error result.
        raise CanvasClientError("Canvas 公告刷新失败，保留当前列表；请稍后重试。")
    result = sort_announcements(result)
    session_cache.set("announcements:all", result)
    return result
