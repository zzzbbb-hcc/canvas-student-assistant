"""Quiz deadline discovery is read-only, complete and preserves user dates."""

from datetime import UTC, datetime
import threading
import time

import httpx
import pytest

from app.models.assignment import Assignment
from app.models.course import Course
from app.services.assignment_loader import load_assignments
from app.services.canvas_client import CanvasClient, CanvasClientError
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache


COURSE = Course(id=7, name="Testing", course_code="TEST1000")
BASE = "https://canvas.sydney.edu.au"
DUE = "2026-10-23T12:59:00Z"


def client(handler):
    return CanvasClient("fake-value", transport=httpx.MockTransport(handler), sleep=lambda _: None)


def route_handler(assignments=(), classic=(), new=(), *, failures=None, courses=None):
    calls = []
    failures = failures or {}

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "canvas.sydney.edu.au"
        assert "access_token" not in request.url.params
        assert request.headers.get("Authorization") == "Bearer fake-value"
        path = request.url.path
        if path in failures:
            return httpx.Response(failures[path], json={"private": "not-retained"}, request=request)
        if path == "/api/v1/courses":
            data = courses if courses is not None else [COURSE.model_dump()]
        elif path.endswith("/assignments"):
            assert request.url.params["include[]"] == "submission"
            assert request.url.params["override_assignment_dates"] == "true"
            data = list(assignments)
        elif path.startswith("/api/quiz/v1/") and path.endswith("/quizzes"):
            data = list(new)
        elif path.endswith("/quizzes"):
            data = list(classic)
        else:
            pytest.fail(f"Unexpected request path: {path}")
        return httpx.Response(200, json=data, request=request)

    return handler, calls


@pytest.mark.parametrize(
    "extra,engine,quiz_id",
    [
        ({"quiz_id": 12}, "classic", 12),
        ({"submission_types": ["online_quiz"]}, "classic", None),
        ({"is_quiz_assignment": True}, "classic", None),
        ({"is_quiz_lti_assignment": True, "submission_types": ["external_tool"]}, "new", 5),
        ({"submission_types": ["external_tool"]}, None, None),
    ],
)
def test_official_assignment_quiz_flags(extra, engine, quiz_id):
    assignment = CanvasClient._assignment({"id": 5, "name": "Quiz", **extra}, COURSE)
    assert assignment.quiz_engine == engine
    assert assignment.quiz_id == quiz_id
    assert assignment.is_quiz == (engine is not None)
    assert assignment.quiz_assignment_id == (5 if engine else None)


def test_model_online_quiz_property_supports_old_cached_records():
    assignment = Assignment(
        assignment_id=1, course_id=7, course_name="Testing", title="Quiz",
        submission_types=["online_quiz"],
    )
    assert assignment.is_quiz


def test_missing_quiz_lock_is_distinguishable_from_explicit_unlocked():
    missing = CanvasClient._assignment({"id": 5, "quiz_id": 1}, COURSE)
    unlocked = CanvasClient._assignment({"id": 5, "quiz_id": 1, "locked_for_user": False}, COURSE)
    assert missing.quiz_locked is False and "quiz_locked" not in missing.model_fields_set
    assert unlocked.quiz_locked is False and "quiz_locked" in unlocked.model_fields_set


def test_merge_linked_classic_quiz_keeps_per_user_dates_and_submission():
    user_due = "2026-10-25T12:59:00Z"
    handler, calls = route_handler(
        assignments=[{
            "id": 10, "name": "Quiz", "quiz_id": 15,
            "due_at": user_due, "unlock_at": None, "lock_at": None,
            "submission": {"workflow_state": "submitted", "submitted_at": DUE, "attempt": 1},
        }],
        classic=[{
            "id": 15, "assignment_id": 10, "title": "Quiz", "due_at": DUE,
            "unlock_at": DUE, "lock_at": DUE, "time_limit": 30,
            "quiz_type": "assignment", "description": "<p>Instructions</p>",
        }],
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
        assert canvas.quiz_coverage_warnings == []
    assert len(result) == 1
    quiz = result[0]
    assert quiz.assignment_id == 10
    assert quiz.due_at == datetime.fromisoformat(user_due)
    assert quiz.unlock_at is None and quiz.lock_at is None
    assert quiz.is_submitted and quiz.submission_attempt == 1
    assert quiz.quiz_type == "assignment" and quiz.quiz_time_limit == 30
    assert quiz.description == "<p>Instructions</p>"
    assert len(calls) == 4  # No per-quiz requests.


def test_explicit_no_due_date_is_not_replaced_by_base_quiz_date_or_lock():
    handler, _ = route_handler(
        assignments=[{"id": 10, "name": "Quiz", "quiz_id": 15, "due_at": None}],
        classic=[{"id": 15, "assignment_id": 10, "title": "Quiz", "due_at": DUE, "lock_at": DUE}],
    )
    with client(handler) as canvas:
        quiz = canvas.get_upcoming_assignments()[0]
    assert quiz.due_at is None
    assert not quiz.is_submitted
    assert quiz.submission_status is None


def test_new_quiz_deduplicates_by_assignment_id_with_user_status():
    handler, _ = route_handler(
        assignments=[{
            "id": 11, "name": "New quiz", "is_quiz_lti_assignment": True,
            "due_at": None, "submission": {"workflow_state": "unsubmitted", "missing": True},
        }],
        new=[{
            "id": "11", "title": "New quiz", "due_at": DUE,
            "instructions": "<p>Instructions</p>",
            "quiz_settings": {"has_time_limit": True, "session_time_limit_in_seconds": 90},
        }],
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
    assert len(result) == 1
    assert result[0].quiz_engine == "new" and result[0].quiz_id == 11
    assert result[0].quiz_assignment_id == 11
    assert result[0].quiz_time_limit == 1.5
    assert result[0].due_at is None
    assert result[0].submission_missing is True
    assert not result[0].is_submitted


def test_practice_survey_and_new_quizzes_absent_assignments_are_discovered():
    handler, _ = route_handler(
        assignments=[{"id": 50, "name": "Essay"}],
        classic=[
            {"id": 1, "title": "Same title", "quiz_type": "practice_quiz", "due_at": DUE},
            {"id": 2, "title": "Same title", "quiz_type": "survey", "lock_at": DUE},
            {"id": 3, "assignment_id": 53, "title": "Graded survey", "quiz_type": "graded_survey", "due_at": DUE},
        ],
        new=[{"id": "1", "title": "Same title", "due_at": DUE}],
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
    assert len(result) == 5
    quizzes = [item for item in result if item.is_quiz]
    assert len(quizzes) == 4
    assert len({item.assignment_id for item in result}) == 5
    assert all(item.submission_status is None and not item.is_submitted for item in quizzes)
    assert next(item for item in quizzes if item.quiz_type == "survey").due_at is None
    assert {item.quiz_engine for item in quizzes} == {"classic", "new"}


def test_quiz_duplicates_deduplicate_by_engine_id_not_title():
    handler, _ = route_handler(
        classic=[{"id": 1, "title": "Quiz"}, {"id": 1, "title": "Quiz"}, {"id": 2, "title": "Quiz"}],
        new=[{"id": "1", "title": "Quiz"}, {"id": "1", "title": "Quiz"}],
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
    assert len(result) == 3
    assert {(item.quiz_engine, item.quiz_id) for item in result} == {("classic", 1), ("classic", 2), ("new", 1)}


def test_quiz_without_assignment_link_deduplicates_using_assignment_quiz_id():
    handler, _ = route_handler(
        assignments=[{"id": 9, "name": "Quiz", "quiz_id": 4, "due_at": DUE}],
        classic=[{"id": 4, "title": "Quiz", "quiz_type": "assignment", "time_limit": 10}],
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
    assert len(result) == 1 and result[0].assignment_id == 9
    assert result[0].quiz_assignment_id == 9


def test_both_quiz_list_endpoints_are_paginated():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        path = request.url.path
        if path == "/api/v1/courses":
            data, headers = [COURSE.model_dump()], {}
        elif path.endswith("/assignments"):
            data, headers = [], {}
        elif path.endswith("/quizzes"):
            second = request.url.params.get("page") == "2"
            data = [{"id": ("22" if second else "21") if "/api/quiz/" in path else (12 if second else 11), "title": "Quiz"}]
            headers = {} if second else {"Link": f'<{BASE}{path}?page=2>; rel="next"'}
        else:
            pytest.fail(path)
        return httpx.Response(200, json=data, headers=headers, request=request)

    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
    assert len(result) == 4
    assert len(calls) == 6


def test_quiz_pagination_cannot_follow_question_or_attempt_links():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/courses":
            data, headers = [COURSE.model_dump()], {}
        elif request.url.path.endswith("/assignments"):
            data, headers = [{"id": 5, "name": "Essay"}], {}
        elif request.url.path == "/api/v1/courses/7/quizzes":
            data, headers = [], {"Link": f'<{BASE}/api/v1/courses/7/quizzes/1/questions>; rel="next"'}
        elif request.url.path == "/api/quiz/v1/courses/7/quizzes":
            data, headers = [], {}
        else:
            pytest.fail("Quiz pagination escaped its metadata endpoint")
        return httpx.Response(200, json=data, headers=headers, request=request)

    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
        assert len(canvas.quiz_coverage_warnings) == 1
    assert len(result) == 1
    assert not any("questions" in path or "submissions" in path for path in calls)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_quiz_access_failure_keeps_assignments_and_other_quiz_engine(status):
    handler, _ = route_handler(
        assignments=[{"id": 10, "name": "Essay", "due_at": DUE}],
        new=[{"id": "22", "title": "New quiz", "due_at": DUE}],
        failures={"/api/v1/courses/7/quizzes": status},
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
        warnings = canvas.quiz_coverage_warnings
    assert [item.assignment_id for item in result] == [10, 22]
    assert len(warnings) == 1
    assert "Course 7" in warnings[0] and "Classic" in warnings[0]
    assert "not-retained" not in str(warnings)
    assert "Authorization" not in str(warnings)


def test_new_quiz_permission_denied_keeps_flagged_assignment_deadline():
    handler, _ = route_handler(
        assignments=[{"id": 10, "name": "New quiz", "is_quiz_lti_assignment": True, "due_at": DUE}],
        failures={"/api/quiz/v1/courses/7/quizzes": 403},
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
        assert len(canvas.quiz_coverage_warnings) == 1
    assert len(result) == 1 and result[0].is_quiz
    assert result[0].due_at == datetime.fromisoformat(DUE)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_assignment_list_failure_still_discovers_accessible_quizzes(status):
    handler, calls = route_handler(
        classic=[{"id": 3, "title": "Practice quiz", "quiz_type": "practice_quiz", "due_at": DUE}],
        new=[{"id": "22", "title": "New quiz", "due_at": DUE}],
        failures={"/api/v1/courses/7/assignments": status},
    )
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
        warnings = canvas.quiz_coverage_warnings
    assert {(item.quiz_engine, item.quiz_id) for item in result} == {("classic", 3), ("new", 22)}
    assert all(item.due_at == datetime.fromisoformat(DUE) for item in result)
    assert all(item.submission_status is None and not item.is_submitted for item in result)
    assert len(warnings) == 1
    assert "TEST1000" in warnings[0] and "assignment list unavailable" in warnings[0]
    assert "not-retained" not in str(warnings)
    assert {request.url.path for request in calls} >= {
        "/api/v1/courses/7/quizzes", "/api/quiz/v1/courses/7/quizzes"
    }


def test_inaccessible_course_assignments_do_not_discard_healthy_course_or_its_user_dates():
    courses = [COURSE.model_dump(), {"id": 8, "name": "Healthy course", "course_code": "HEALTHY"}]
    user_due = "2026-10-25T12:59:00Z"

    def handler(request):
        assert request.method == "GET"
        path = request.url.path
        if path == "/api/v1/courses":
            data = courses
        elif path == "/api/v1/courses/7/assignments":
            return httpx.Response(403, json={"private": "not-retained"}, request=request)
        elif path == "/api/v1/courses/7/quizzes":
            data = [{"id": 3, "title": "Accessible practice", "quiz_type": "practice_quiz", "due_at": DUE}]
        elif path == "/api/v1/courses/8/assignments":
            data = [
                {"id": 40, "name": "Essay", "due_at": DUE},
                {"id": 41, "name": "Personal quiz", "quiz_id": 4, "due_at": user_due,
                 "submission": {"workflow_state": "submitted", "submitted_at": DUE, "attempt": 1}},
                {"id": 42, "name": "No due quiz", "quiz_id": 5, "due_at": None},
            ]
        elif path == "/api/v1/courses/8/quizzes":
            data = [
                {"id": 4, "assignment_id": 41, "title": "Personal quiz", "due_at": DUE},
                {"id": 5, "assignment_id": 42, "title": "No due quiz", "due_at": DUE},
            ]
        elif path in {"/api/quiz/v1/courses/7/quizzes", "/api/quiz/v1/courses/8/quizzes"}:
            data = []
        else:
            pytest.fail(path)
        return httpx.Response(200, json=data, request=request)

    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
        assert len(canvas.quiz_coverage_warnings) == 1
    assert len(result) == 4
    assert next(item for item in result if item.course_id == 7).title == "Accessible practice"
    by_id = {item.assignment_id: item for item in result if item.course_id == 8}
    assert set(by_id) == {40, 41, 42}
    assert by_id[41].due_at == datetime.fromisoformat(user_due)
    assert by_id[41].is_submitted
    assert by_id[42].due_at is None


def test_bad_quiz_record_reports_incomplete_coverage_without_dropping_good_record():
    handler, _ = route_handler(classic=[{"id": 1, "title": "Valid"}, {"title": "Missing id"}, {"id": 3, "due_at": "not-a-date"}])
    with client(handler) as canvas:
        result = canvas.get_upcoming_assignments()
        assert len(canvas.quiz_coverage_warnings) == 1
    assert len(result) == 1 and result[0].title == "Valid"


def test_isolated_quiz_ids_are_stable_engine_and_course_namespaced():
    one = CanvasClient._quiz_assignment({"id": 3, "title": "Quiz"}, COURSE, "classic")
    repeat = CanvasClient._quiz_assignment({"id": 3, "title": "Renamed"}, COURSE, "classic")
    other_course = CanvasClient._quiz_assignment({"id": 3}, Course(id=8, name="Other"), "classic")
    assert one.assignment_id == repeat.assignment_id < 0
    assert one.assignment_id != other_course.assignment_id < 0
    assert one.quiz_assignment_id is None


def test_large_isolated_quiz_ids_fit_sqlite_notification_history(tmp_path):
    quiz = CanvasClient._quiz_assignment(
        {"id": 2**63 - 1}, Course(id=2**63 - 2, name="Global course"), "classic"
    )
    assert -(2**63) <= quiz.assignment_id < 0
    database = SQLiteDatabase(tmp_path / "reminder.sqlite3")
    try:
        assert database.record_reminder_sent(quiz.assignment_id, "1_day", datetime.now(UTC))
        assert database.reminder_was_sent(quiz.assignment_id, "1_day")
    finally:
        database.close()


@pytest.mark.parametrize("engine,prefix", [("classic", "/api/v1"), ("new", "/api/quiz/v1")])
def test_quiz_detail_uses_only_read_only_metadata_endpoint(engine, prefix):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path == "/api/v1/courses/7":
            data = COURSE.model_dump()
        else:
            assert request.url.path == f"{prefix}/courses/7/quizzes/3"
            data = {"id": 3, "title": "Quiz", "due_at": DUE, "locked_for_user": True,
                    "description": "<p>Classic</p>", "instructions": "<p>New</p>"}
        return httpx.Response(200, json=data, request=request)

    with client(handler) as canvas:
        quiz = canvas.get_quiz_assignment(7, 3, engine=engine)
    assert quiz.is_quiz and quiz.quiz_locked
    assert quiz.description == ("<p>New</p>" if engine == "new" else "<p>Classic</p>")
    assert quiz.submission_status is None and not quiz.is_submitted
    assert len(calls) == 2
    if engine == "classic":
        expected = CanvasClient._quiz_assignment({"id": 3}, COURSE, engine).assignment_id
        assert quiz.assignment_id == expected


@pytest.mark.parametrize("engine,quiz_id", [("unknown", 1), ("classic", -1), ("classic", True), ("new", "../questions")])
def test_invalid_quiz_detail_identifiers_do_not_make_network_requests(engine, quiz_id):
    with client(lambda _: pytest.fail("Invalid quiz must not make requests")) as canvas:
        with pytest.raises(CanvasClientError, match="Could not load"):
            canvas.get_quiz_assignment(7, quiz_id, engine=engine)


def test_quiz_detail_bad_json_is_a_safe_error():
    def handler(request):
        if request.url.path == "/api/v1/courses/7":
            return httpx.Response(200, json=COURSE.model_dump(), request=request)
        return httpx.Response(200, text="not-json", request=request)

    with client(handler) as canvas:
        with pytest.raises(CanvasClientError, match="Could not load this Canvas quiz"):
            canvas.get_quiz_assignment(7, 3)


def test_refresh_course_concurrency_is_bounded_to_four():
    lock = threading.Lock()
    active = 0
    max_active = 0

    def handler(request):
        nonlocal active, max_active
        if request.url.path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": i, "name": f"Course {i}"} for i in range(1, 9)], request=request)
        assert request.method == "GET"
        if request.url.path.endswith("/assignments"):
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
        return httpx.Response(200, json=[], request=request)

    with client(handler) as canvas:
        assert canvas.get_upcoming_assignments() == []
    assert 1 < max_active <= 4


def test_loader_keeps_only_display_safe_quiz_metadata_in_cache(tmp_path, monkeypatch):
    from app.services import assignment_loader

    raw = {
        "id": 5, "title": "Practice quiz", "due_at": DUE,
        "description": '<p><a href="https://example.invalid/?signed=value">Instructions</a></p>',
        "access_code": "not-retained", "ip_filter": "not-retained",
        "mobile_url": "not-retained", "quiz_submission_html_url": "not-retained",
    }
    quiz = CanvasClient._quiz_assignment(raw, COURSE, "classic")

    class FakeClient:
        quiz_coverage_warnings = ["Course 7: New quiz list unavailable; quiz deadlines may be incomplete."]

        def __init__(self, _token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get_upcoming_assignments(self):
            return [quiz]

    class Store:
        def get_canvas_token(self):
            return "dummy"

    monkeypatch.setattr(assignment_loader, "CanvasClient", FakeClient)
    database = SQLiteDatabase(tmp_path / "cache.sqlite3")
    try:
        cache = Cache(database)
        loaded = load_assignments(Store(), refresh=True, cache=cache)
        cached = cache.get("assignments")
        assert loaded[0].description
        assert cached[0]["quiz_id"] == 5 and cached[0]["quiz_engine"] == "classic"
        assert "description" not in cached[0] and "html_url" not in cached[0]
        assert "not-retained" not in str(cached)
        assert "signed=value" not in str(cached)
        assert session_cache.get("quiz_coverage_warnings") == FakeClient.quiz_coverage_warnings
        cached_quiz = Assignment.model_validate(cached[0])
        assert cached_quiz.is_quiz and cached_quiz.due_at == datetime.fromisoformat(DUE)
    finally:
        session_cache.clear()
        database.close()
