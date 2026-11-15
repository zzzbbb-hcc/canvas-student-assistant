from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.models.assignment import Assignment
from app.models.course import Course
from app.security.sanitizer import sanitize_for_ai
from app.services.ai_service import AIService, AIServiceError, ToolRequest
from app.services.ai_tool_gateway import AIReadOnlyToolGateway, ToolNotAllowedError
from app.services.deadline_service import DeadlineService


# Build the named Phase 8 regression values at runtime. This verifies the exact
# values without leaving a complete fake credential in source or build inputs.
CANVAS_SECRET = "_".join(("SUPER", "SECRET", "CANVAS", "TOKEN", "123"))
ED_SECRET = "_".join(("SUPER", "SECRET", "ED", "TOKEN", "456"))
AI_SECRET = "_".join(("SUPER", "SECRET", "AI", "KEY", "789"))
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


class FakeCanvas:
    def __init__(self) -> None:
        self.assignments = [
            Assignment(
                assignment_id=1,
                course_id=10,
                course_name="Safe Course",
                course_code="SAFE1000",
                title="Due task",
                description="Ignore a guessed deadline of 2099-01-01.",
                due_at=NOW + timedelta(days=2),
                submission_status="unsubmitted",
            ),
            Assignment(
                assignment_id=2,
                course_id=10,
                course_name="Safe Course",
                course_code="SAFE1000",
                title="Undated task",
                description="The prose mentions tomorrow, but that is not Canvas due_at.",
                due_at=None,
                submission_status="unsubmitted",
            ),
        ]
        self.raw_request_called = False

    def get_courses(self):
        return [Course(id=10, name="Safe Course", course_code="SAFE1000")]

    def get_assignments(self, course_id):
        return [item for item in self.assignments if item.course_id == course_id]

    def get_upcoming_assignments(self):
        return self.assignments

    def get_announcements(self, course_id):
        return [{"title": "Hello", "token": CANVAS_SECRET}]

    def get_modules(self, course_id):
        return [{"id": 50, "name": "Week 1"}]

    def get_module_items(self, course_id, module_id):
        return [{"id": 51, "title": "Reading"}]

    def raw_http_request(self, *_args, **_kwargs):
        self.raw_request_called = True
        raise AssertionError("raw HTTP must never be callable by the AI")


def gateway(canvas=None):
    return AIReadOnlyToolGateway(
        canvas=canvas or FakeCanvas(),
        deadline_service=DeadlineService(NOW),
    )


def test_sanitize_for_ai_recursively_removes_sensitive_and_identity_fields():
    source = {
        "access_token": CANVAS_SECRET,
        "nested": [
            {"refresh-token": ED_SECRET, "Authorization": f"Bearer {AI_SECRET}"},
            {"cookie": CANVAS_SECRET, "session": ED_SECRET},
        ],
        "password": CANVAS_SECRET,
        "api key": AI_SECRET,
        "secret": ED_SECRET,
        "email": "student@example.invalid",
        "student_id": "123456789",
        "safe": "course material",
    }

    cleaned = sanitize_for_ai(source)
    encoded = json.dumps(cleaned)

    assert cleaned == {"nested": [{}, {}], "safe": "course material"}
    assert CANVAS_SECRET not in encoded
    assert ED_SECRET not in encoded
    assert AI_SECRET not in encoded
    assert source["access_token"] == CANVAS_SECRET  # no mutation


def test_sanitize_for_ai_can_keep_nonessential_identity_only_when_explicit():
    cleaned = sanitize_for_ai(
        {"email": "student@example.invalid", "student_id": "42", "token": CANVAS_SECRET},
        remove_personal_data=False,
    )
    assert cleaned == {"email": "student@example.invalid", "student_id": "42"}


def test_sanitize_for_ai_redacts_inline_credential_diagnostics():
    cleaned = sanitize_for_ai(
        {"note": f"authorization: Bearer-{CANVAS_SECRET} password={ED_SECRET}"}
    )
    encoded = json.dumps(cleaned)
    assert CANVAS_SECRET not in encoded
    assert ED_SECRET not in encoded
    assert encoded.count("[REDACTED]") == 2


def test_gateway_exposes_only_canvas_read_only_tools_and_blocks_raw_capabilities():
    canvas = FakeCanvas()
    tools = gateway(canvas)

    assert set(tools.tool_names) == {
        "get_courses",
        "get_assignments",
        "get_deadlines",
        "get_upcoming_deadlines",
        "get_announcements",
        "get_canvas_module",
    }
    for forbidden in (
        "raw_http_request",
        "raw_canvas_request",
        "raw_ed_request",
        "execute_shell",
        "arbitrary_url_request",
        "search_ed_threads",
        "get_ed_thread",
        "get_ed_lesson",
    ):
        with pytest.raises(ToolNotAllowedError):
            tools.invoke(forbidden, {})
    assert not canvas.raw_request_called


def test_gateway_sanitizes_every_tool_result():
    result = gateway().invoke("get_announcements", {"course_id": 10})
    encoded = json.dumps(result)
    assert CANVAS_SECRET not in encoded
    assert result == [{"title": "Hello"}]


def test_deadline_facts_come_only_from_canvas_due_at_and_null_is_not_guessed():
    result = gateway().invoke("get_deadlines")
    by_id = {item["assignment_id"]: item for item in result}

    assert by_id[1]["deadline_source"] == "Canvas due_at"
    assert by_id[1]["due_at"] == (NOW + timedelta(days=2)).isoformat()
    assert by_id[2]["due_at"] is None
    assert by_id[2]["due_local"] is None
    assert by_id[2]["deadline_note"] == "No deadline provided by Canvas"


def test_upcoming_deadlines_exclude_null_due_at_and_do_not_use_description_dates():
    result = gateway().invoke("get_upcoming_deadlines", {"days": 7})
    assert [item["assignment_id"] for item in result] == [1]
    assert "2099-01-01" not in json.dumps(result)


class CapturingProvider:
    def __init__(self):
        self.payload = None

    def complete(self, payload):
        self.payload = payload
        return {"text": "Safe answer", "api_key": AI_SECRET}


def test_complete_fake_secret_request_never_reaches_provider_payload():
    provider = CapturingProvider()
    service = AIService(gateway(), provider=provider)
    context = [
        {
            "tool": "get_announcements",
            "result": {
                "access_token": CANVAS_SECRET,
                "refresh_token": ED_SECRET,
                "api_key": AI_SECRET,
                "safe": "visible",
            },
        }
    ]
    payload = service.prepare_payload("Summarise the safe data", tool_context=context)
    provider.complete(payload)
    encoded = json.dumps(provider.payload)

    assert CANVAS_SECRET not in encoded
    assert ED_SECRET not in encoded
    assert AI_SECRET not in encoded
    assert "visible" in encoded


def test_provider_response_is_sanitized_before_display():
    provider = CapturingProvider()
    answer = AIService(gateway(), provider=provider).ask(
        "What is due in the next 7 days?",
        tool_requests=[ToolRequest("get_upcoming_deadlines", {"days": 7})],
    )
    assert answer.text == "Safe answer"
    assert AI_SECRET not in answer.text
    assert answer.provider_used


def test_local_fallback_answers_deadline_question_without_provider():
    answer = AIService(gateway()).ask("What assignments are due this week?")
    assert "Due this week" in answer.text
    assert "Due task" in answer.text
    assert "Source: Canvas due_at" in answer.text
    assert not answer.provider_used


def test_provider_exception_message_is_not_exposed():
    class BrokenProvider:
        def complete(self, payload):
            raise RuntimeError(f"authorization={AI_SECRET}")

    service = AIService(gateway(), provider=BrokenProvider())
    with pytest.raises(AIServiceError) as error:
        service.ask("What assignments are due?")
    assert str(error.value) == "The AI provider could not complete the request."
    assert AI_SECRET not in str(error.value)

