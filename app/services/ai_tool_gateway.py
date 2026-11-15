"""Capability-limited, read-only data gateway for the study assistant.

Only the methods in :attr:`AIReadOnlyToolGateway.ALLOWED_TOOLS` can be invoked
by AI orchestration code. Network clients, credential storage, arbitrary URLs,
and raw request methods are intentionally absent from the public contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any, Protocol

from app.models.assignment import Assignment
from app.models.announcement import announcement_text
from app.security.sanitizer import sanitize_for_ai
from app.services.deadline_service import DeadlineService


class AIToolError(Exception):
    """Safe error raised for an unavailable or invalid AI tool invocation."""


class ToolNotAllowedError(AIToolError):
    """The requested operation is outside the read-only allowlist."""


class ToolUnavailableError(AIToolError):
    """The requested official data integration has not been configured."""


class CanvasReadOnlySource(Protocol):
    def get_courses(self) -> Sequence[Any]: ...
    def get_assignments(self, course_id: int) -> Sequence[Any]: ...
    def get_upcoming_assignments(self) -> Sequence[Any]: ...
    def get_announcements(self, course_id: int) -> Sequence[Any]: ...
    def get_modules(self, course_id: int) -> Sequence[Any]: ...
    def get_module_items(self, course_id: int, module_id: int) -> Sequence[Any]: ...


class AIReadOnlyToolGateway:
    """Expose a fixed set of study-data reads and nothing else.

    The gateway accepts already-authorized read-only sources. It never receives
    a token or credential store, and it never constructs a network request.
    """

    ALLOWED_TOOLS = (
        "get_courses",
        "get_assignments",
        "get_deadlines",
        "get_upcoming_deadlines",
        "get_announcements",
        "get_canvas_module",
    )

    TOOL_ARGUMENTS: Mapping[str, frozenset[str]] = {
        "get_courses": frozenset(),
        "get_assignments": frozenset({"course_id"}),
        "get_deadlines": frozenset({"course_id"}),
        "get_upcoming_deadlines": frozenset({"course_id", "days", "limit"}),
        "get_announcements": frozenset({"course_id"}),
        "get_canvas_module": frozenset({"course_id", "module_id"}),
    }

    def __init__(
        self,
        *,
        canvas: CanvasReadOnlySource | None = None,
        deadline_service: DeadlineService | None = None,
    ) -> None:
        self._canvas = canvas
        self._deadlines = deadline_service or DeadlineService()

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self.ALLOWED_TOOLS

    @staticmethod
    def tool_definitions() -> list[dict[str, Any]]:
        """Return provider-neutral descriptions without executable capabilities."""

        descriptions = {
            "get_courses": "List the student's Canvas courses.",
            "get_assignments": "List Canvas assignments, optionally for one course.",
            "get_deadlines": "List assignment deadlines sourced only from Canvas due_at.",
            "get_upcoming_deadlines": "List upcoming unsubmitted Canvas deadlines.",
            "get_announcements": "Read Canvas announcements for one course.",
            "get_canvas_module": "Read one Canvas module and its items.",
        }
        return [
            {
                "name": name,
                "description": descriptions[name],
                "allowed_arguments": sorted(AIReadOnlyToolGateway.TOOL_ARGUMENTS[name]),
                "read_only": True,
            }
            for name in AIReadOnlyToolGateway.ALLOWED_TOOLS
        ]

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Invoke exactly one allowlisted tool with allowlisted arguments."""

        if name not in self.ALLOWED_TOOLS:
            raise ToolNotAllowedError("This AI tool is not allowed.")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise AIToolError("AI tool arguments must be an object.")
        supplied = set(arguments)
        if supplied - self.TOOL_ARGUMENTS[name]:
            raise AIToolError("One or more AI tool arguments are not allowed.")

        # The dispatch table contains named, bounded reads only. Never replace
        # this with getattr(self, name) or a client/raw-request passthrough.
        dispatch: dict[str, Callable[..., Any]] = {
            "get_courses": self.get_courses,
            "get_assignments": self.get_assignments,
            "get_deadlines": self.get_deadlines,
            "get_upcoming_deadlines": self.get_upcoming_deadlines,
            "get_announcements": self.get_announcements,
            "get_canvas_module": self.get_canvas_module,
        }
        try:
            result = dispatch[name](**dict(arguments))
        except AIToolError:
            raise
        except (TypeError, ValueError):
            raise AIToolError("The AI tool arguments are invalid.") from None
        except Exception:
            # Upstream SDK exceptions can embed headers. Keep their diagnostic
            # object entirely behind this safe application boundary.
            raise AIToolError("Study data could not be read.") from None
        return sanitize_for_ai(result)

    def _require_canvas(self) -> CanvasReadOnlySource:
        if self._canvas is None:
            raise ToolUnavailableError("Canvas study data is unavailable.")
        return self._canvas

    @staticmethod
    def _positive_id(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise AIToolError(f"{label} must be a positive integer.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise AIToolError(f"{label} must be a positive integer.") from exc
        if parsed <= 0:
            raise AIToolError(f"{label} must be a positive integer.")
        return parsed

    @staticmethod
    def _optional_course_id(value: Any) -> int | None:
        if value is None:
            return None
        return AIReadOnlyToolGateway._positive_id(value, "course_id")

    def get_courses(self) -> list[Any]:
        return list(self._require_canvas().get_courses())

    def _read_assignments(self, course_id: int | None = None) -> list[Assignment]:
        source = self._require_canvas()
        parsed_course_id = self._optional_course_id(course_id)
        if parsed_course_id is None:
            assignments = source.get_upcoming_assignments()
        else:
            assignments = source.get_assignments(parsed_course_id)
        return [self._coerce_assignment(item) for item in assignments]

    def get_assignments(self, course_id: int | None = None) -> list[dict[str, Any]]:
        return [self._assignment_payload(item) for item in self._read_assignments(course_id)]

    @staticmethod
    def _coerce_assignment(value: Any) -> Assignment:
        if isinstance(value, Assignment):
            return value
        try:
            return Assignment.model_validate(value)
        except (TypeError, ValueError) as exc:
            raise AIToolError("Canvas returned an invalid assignment record.") from exc

    @classmethod
    def _assignment_payload(cls, value: Any) -> dict[str, Any]:
        assignment = cls._coerce_assignment(value)
        # Rich HTML and signed hrefs are not needed by this metadata tool.
        # Full study evidence is separately read as bounded plain text.
        payload = sanitize_for_ai(assignment.model_dump(exclude={"html_url", "description"}))
        if not isinstance(payload, dict):
            raise AIToolError("Canvas returned an invalid assignment record.")
        payload["description"] = announcement_text(assignment.description)
        payload["deadline_source"] = "Canvas due_at"
        if assignment.due_at is None:
            payload["deadline_note"] = "No deadline provided by Canvas"
        return payload

    def get_deadlines(self, course_id: int | None = None) -> list[dict[str, Any]]:
        return [
            self._deadline_payload(item)
            for item in self._deadlines.deadlines(self._read_assignments(course_id))
        ]

    @staticmethod
    def _deadline_payload(deadline: Any) -> dict[str, Any]:
        assignment = deadline.assignment
        result = {
            "assignment_id": assignment.assignment_id,
            "course_id": assignment.course_id,
            "course_name": assignment.course_name,
            "course_code": assignment.course_code,
            "title": assignment.title,
            "due_at": assignment.due_at,
            "due_local": deadline.due_local,
            "status": deadline.status,
            "category": deadline.category,
            "countdown": deadline.countdown,
            "submission_status": assignment.submission_label,
            "deadline_source": "Canvas due_at",
        }
        if assignment.due_at is None:
            result["deadline_note"] = "No deadline provided by Canvas"
        clean = sanitize_for_ai(result)
        assert isinstance(clean, dict)
        return clean

    def get_upcoming_deadlines(
        self,
        course_id: int | None = None,
        days: int = 7,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
            raise AIToolError("days must be an integer from 1 to 366.")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            raise AIToolError("limit must be an integer from 1 to 100.")
        cutoff = self._deadlines.now + timedelta(days=days)
        deadlines = self._deadlines.deadlines(self._read_assignments(course_id))
        upcoming = [
            self._deadline_payload(item)
            for item in deadlines
            if item.due_local is not None
            and self._deadlines.now <= item.due_local <= cutoff
            and item.status != "completed"
        ]
        return upcoming[:limit] if limit is not None else upcoming

    def get_announcements(self, course_id: int) -> list[Any]:
        course = self._positive_id(course_id, "course_id")
        return list(self._require_canvas().get_announcements(course))

    def get_canvas_module(self, course_id: int, module_id: int) -> dict[str, Any]:
        course = self._positive_id(course_id, "course_id")
        module = self._positive_id(module_id, "module_id")
        source = self._require_canvas()
        modules = list(source.get_modules(course))
        selected = next(
            (
                item
                for item in modules
                if isinstance(item, Mapping) and str(item.get("id")) == str(module)
            ),
            None,
        )
        if selected is None:
            raise AIToolError("Canvas module was not found.")
        return {"module": selected, "items": list(source.get_module_items(course, module))}

