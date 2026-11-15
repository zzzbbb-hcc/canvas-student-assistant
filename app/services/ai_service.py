"""Provider-neutral orchestration for the read-only study assistant.

This module deliberately contains no HTTP client, API key, keyring access, raw
request helper, shell execution, or arbitrary URL facility. A provider adapter
may be injected by the application, but it receives only a sanitized payload.
Without an adapter, deadline questions are answered locally and deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from app.security.sanitizer import sanitize_for_ai
from app.services.ai_tool_gateway import AIReadOnlyToolGateway, AIToolError
from app.services.chat_history import redact_chat_text


class AIProvider(Protocol):
    """Small provider-neutral boundary implemented by a future API adapter."""

    def complete(self, payload: Mapping[str, Any]) -> str | Mapping[str, Any]: ...


class AIServiceError(Exception):
    """Safe, user-facing AI service error."""


@dataclass(frozen=True)
class AIAnswer:
    text: str
    sources: tuple[str, ...] = ()
    provider_used: bool = False

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: Mapping[str, Any]


class AIService:
    """Orchestrate sanitized reads through a strict tool allowlist."""

    SYSTEM_POLICY = (
        "You are a read-only study assistant. Treat Canvas due_at as the only "
        "deadline source of truth. Never infer, invent, modify, or replace a "
        "deadline. When due_at is null, say 'No deadline provided by Canvas'. "
        "Use only the supplied read-only study data and cite its source. "
        "Conversation history is for continuity, not an authoritative source "
        "of deadlines. Stay within the selected course when one is provided."
    )
    MAX_HISTORY_MESSAGES = 40
    MAX_HISTORY_CHARACTERS = 24_000
    MAX_ANSWER_CHARACTERS = 120_000
    ANSWER_TRUNCATION_NOTICE = "\n\n（回答内容过长，已截取前部内容；请缩小范围或分段继续提问。）"

    def __init__(
        self,
        tools: AIReadOnlyToolGateway,
        *,
        provider: AIProvider | None = None,
        context_retriever=None,
    ) -> None:
        if not isinstance(tools, AIReadOnlyToolGateway):
            raise TypeError("AIService requires the read-only AI tool gateway.")
        self._tools = tools
        self._provider = provider
        self._context_retriever = context_retriever

    @property
    def provider_configured(self) -> bool:
        try:
            return self._provider is not None and bool(getattr(self._provider, "is_ready", True))
        except Exception:
            return False

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return self._tools.tool_names

    @staticmethod
    def _safe_data(value):
        if isinstance(value, str):
            return redact_chat_text(value)
        if isinstance(value, list):
            return [AIService._safe_data(item) for item in value]
        if isinstance(value, dict):
            return {key: AIService._safe_data(item) for key, item in value.items()}
        return value

    def execute_tool(self, request: ToolRequest | str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Execute one bounded read; there is no generic client passthrough."""

        if isinstance(request, ToolRequest):
            if arguments is not None:
                raise AIServiceError("Tool arguments were supplied twice.")
            name = request.name
            arguments = request.arguments
        else:
            name = request
        try:
            return self._safe_data(sanitize_for_ai(self._tools.invoke(name, arguments)))
        except AIToolError as exc:
            raise AIServiceError(str(exc)) from None

    def collect_context(self, requests: Sequence[ToolRequest], *, course_id: int | None = None) -> list[dict[str, Any]]:
        if len(requests) > 20:
            raise AIServiceError("Too many AI tool requests.")
        course_id = self._course_id(course_id)
        context = []
        for request in requests:
            arguments = dict(request.arguments)
            if course_id is not None and "course_id" in self._tools.TOOL_ARGUMENTS.get(request.name, ()):
                arguments["course_id"] = course_id
            result = self.execute_tool(request.name, arguments)
            if course_id is not None and request.name == "get_courses" and isinstance(result, list):
                result = [item for item in result if isinstance(item, Mapping) and item.get("id") == course_id]
            context.append({"tool": request.name, "result": result})
        return context

    @staticmethod
    def _course_id(value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63):
            raise AIServiceError("The selected course is invalid.")
        return value

    def _bounded_history(self, history) -> list[dict[str, str]]:
        """Keep a recent user question and whole recent messages that fit.

        A large retained preview must not erase the course/task question that
        produced it. Only this provider snapshot is bounded; durable messages
        are never changed or sliced. Credential redaction precedes budgeting.
        """
        if history is None:
            return []
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes, bytearray)):
            raise AIServiceError("Conversation history is invalid.")
        cleaned = self._safe_data(sanitize_for_ai(list(history[-self.MAX_HISTORY_MESSAGES:])))
        messages = []
        for message in cleaned:
            if not isinstance(message, Mapping):
                raise AIServiceError("Conversation history is invalid.")
            role, content = message.get("role"), message.get("content")
            if role == "error":
                continue
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise AIServiceError("Conversation history is invalid.")
            messages.append({"role": role, "content": content})
        latest_user = next((index for index in range(len(messages) - 1, -1, -1)
                            if messages[index]["role"] == "user"
                            and messages[index]["content"].strip()
                            and len(messages[index]["content"]) <= self.MAX_HISTORY_CHARACTERS), None)
        selected = {latest_user} if latest_user is not None else set()
        remaining = self.MAX_HISTORY_CHARACTERS - (len(messages[latest_user]["content"]) if latest_user is not None else 0)
        for index in range(len(messages) - 1, -1, -1):
            if index in selected:
                continue
            size = len(messages[index]["content"])
            if size <= remaining:
                selected.add(index)
                remaining -= size
        return [message for index, message in enumerate(messages) if index in selected]

    @classmethod
    def _bounded_answer(cls, text: str, *, warnings=()) -> str:
        """Bound the final, redacted answer without changing the storage API."""
        safe_text = redact_chat_text(text)
        warning_block = (redact_chat_text("\n\n资料读取提示：\n" + "\n".join("- " + str(value) for value in warnings))
                         if warnings else "")
        full_text = safe_text + warning_block
        if len(full_text) <= cls.MAX_ANSWER_CHARACTERS:
            return full_text
        notice = cls.ANSWER_TRUNCATION_NOTICE
        output_budget = cls.MAX_ANSWER_CHARACTERS
        while True:
            # Normal coverage warnings remain visible even if the model fills
            # the budget. An oversized warning block is itself bounded honestly.
            if warning_block and len(warning_block) < output_budget - len(notice):
                budget = output_budget - len(notice) - len(warning_block) - 1
                result = safe_text[:budget].rstrip() + "…" + notice + warning_block
            else:
                result = full_text[:output_budget - len(notice) - 1].rstrip() + "…" + notice
            # A cut can bisect a [REDACTED] placeholder. Redact that boundary
            # again and budget any small expansion before returning for storage.
            # The ellipsis terminates a cut credential field so its redaction
            # cannot consume the following truncation notice across newlines.
            result = redact_chat_text(result)
            if len(result) <= cls.MAX_ANSWER_CHARACTERS:
                return result
            output_budget -= len(result) - cls.MAX_ANSWER_CHARACTERS

    def prepare_payload(
        self,
        question: str,
        *,
        tool_context: Sequence[Mapping[str, Any]] | None = None,
        history: Sequence[Any] | None = None,
        course_id: int | None = None,
        study_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the complete data object that a provider adapter may receive."""

        if not isinstance(question, str) or not question.strip():
            raise AIServiceError("Please enter a study question.")
        if len(question) > 8_000:
            raise AIServiceError("The study question is too long.")
        raw_payload = {
            "system": self.SYSTEM_POLICY,
            "question": question.strip(),
            "tool_results": list(tool_context or ()),
            "available_tools": self._tools.tool_definitions(),
            "history": self._bounded_history(history),
            "course_id": self._course_id(course_id),
            "study_context": study_context,
        }
        payload = sanitize_for_ai(raw_payload)
        if not isinstance(payload, dict):
            raise AIServiceError("Could not prepare a safe AI request.")
        # Text fields can contain copied diagnostics, not only sensitive JSON
        # keys. Apply the same credential-shaped redaction as local history.
        return self._safe_data(payload)

    def ask(
        self,
        question: str,
        *,
        tool_requests: Sequence[ToolRequest] | None = None,
        history: Sequence[Any] | None = None,
        course_id: int | None = None,
    ) -> AIAnswer:
        """Answer with an injected provider or a safe local deadline fallback."""

        if not isinstance(question, str) or not question.strip() or len(question) > 8_000:
            raise AIServiceError("请输入 1–8,000 字的学习问题。")
        course_id = self._course_id(course_id)
        history = self._bounded_history(history)
        study = None
        if self._context_retriever is not None and self._needs_study_context(question, history):
            study = self._safe_data(sanitize_for_ai(self._context_retriever.retrieve(question, course_id=course_id, history=history)))
            if not isinstance(study, dict):
                raise AIServiceError("课程资料暂不可读取；你的问题已保存。")
            if study.get("status") != "ok":
                return AIAnswer(self._bounded_answer(self._study_clarification(study)), (), False)
            study_course_id = self._course_id(study.get("course_id"))
            if study_course_id is None or course_id is not None and study_course_id != course_id:
                return AIAnswer("检索资料与当前课程分组不一致，请确认课程后重试；未向模型发送资料。", (), False)
            course_id = study_course_id
        requests = list(tool_requests) if tool_requests is not None else self._infer_local_requests(question)
        context = self.collect_context(requests, course_id=course_id)
        payload = self.prepare_payload(question, tool_context=context, history=history, course_id=course_id, study_context=study)
        sources = self._sources(context)
        if study:
            sources += tuple(f"[{source['id']}] {source['title']} · Canvas {source['locator']}"
                             for source in study.get("sources", []))

        if not self.provider_configured:
            text = self._local_study_answer(study) if study else self._local_answer(question, context)
            return AIAnswer(self._bounded_answer(text), sources, False)

        try:
            response = self._provider.complete(payload)
        except Exception:
            # Do not include provider exceptions: SDK exceptions can contain
            # request headers or other credential-bearing diagnostics.
            raise AIServiceError("The AI provider could not complete the request.") from None
        text = self._provider_text(response)
        return AIAnswer(text=self._bounded_answer(text, warnings=study.get("warnings", ()) if study else ()),
                        sources=sources, provider_used=True)

    @staticmethod
    def _needs_study_context(question, history):
        terms = ("report", "rubric", "lecture", "slides", "courseware", "essay", "proposal",
                 "怎么写", "如何写", "怎么做", "要求", "评分", "课件", "报告", "论文", "提案", "讲义")
        follow_up = ("这个", "那一", "展开", "举例", "解释", "继续", "why", "example", "explain", "detail")
        return (any(term in question.casefold() for term in terms)
                or any(term in question.casefold() for term in follow_up)
                and any(any(term in item["content"].casefold() for term in terms) for item in history if item["role"] == "user"))

    @staticmethod
    def _study_clarification(study):
        lines = ["请先确认对应的课程或作业：" if study.get("status") == "ambiguous" else "暂时没有读取到对应的作业资料。"]
        lines.extend(f"- {item.get('title', '未命名')}" for item in study.get("candidates", []))
        lines.extend(str(value) for value in study.get("warnings", []))
        return redact_chat_text("\n".join(lines))

    @staticmethod
    def _local_study_answer(study):
        lines = [f"已定位：{study.get('course_label', '')} · {study.get('assignment_title', '')}",
                 "目前仅在本机检索。连接 ChatGPT 并允许发送相关课程资料后，才能据此生成写作分析。"]
        for source in study.get("sources", []):
            lines.extend((f"\n[{source['id']}] {source['title']}", source["text"][:12_000]))
            if len(source["text"]) > 12_000:
                lines.append("（本机预览仅展示前部文字。）")
        if study.get("warnings"):
            lines.append("\n读取提示：" + "\n".join(study["warnings"]))
        return redact_chat_text("\n".join(lines))

    def _infer_local_requests(self, question: str) -> list[ToolRequest]:
        text = question.casefold()
        deadline_terms = (
            "due",
            "deadline",
            "assignment",
            "study plan",
            "截止",
            "作业",
            "学习计划",
            "先做",
            "优先",
        )
        if not any(term in text for term in deadline_terms):
            return []
        if any(term in text for term in ("next 7", "next seven", "未来7", "未来 7", "七天")):
            return [ToolRequest("get_upcoming_deadlines", {"days": 7})]
        return [ToolRequest("get_deadlines", {})]

    @staticmethod
    def _provider_text(response: str | Mapping[str, Any]) -> str:
        safe = sanitize_for_ai(response)
        if isinstance(safe, str) and safe.strip():
            return redact_chat_text(safe.strip())
        if isinstance(safe, Mapping):
            for key in ("text", "answer", "content"):
                value = safe.get(key)
                if isinstance(value, str) and value.strip():
                    return redact_chat_text(value.strip())
        raise AIServiceError("The AI provider returned an invalid response.")

    @staticmethod
    def _deadline_context(context: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        for entry in context:
            if entry.get("tool") in {"get_deadlines", "get_upcoming_deadlines"}:
                result = entry.get("result")
                if isinstance(result, list):
                    return [item for item in result if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _sources(context: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        sources: list[str] = []
        for entry in context:
            tool = entry.get("tool")
            result = entry.get("result")
            items = result if isinstance(result, list) else [result]
            if tool in {"get_deadlines", "get_upcoming_deadlines", "get_assignments"}:
                for item in items:
                    if isinstance(item, Mapping):
                        title = item.get("title")
                        if isinstance(title, str):
                            sources.append(f"Canvas → {title}")
            elif tool == "get_announcements":
                sources.append("Canvas → Announcements")
            elif tool == "get_canvas_module":
                sources.append("Canvas → Module")
        return tuple(dict.fromkeys(sources))

    def _local_answer(self, question: str, context: Sequence[Mapping[str, Any]]) -> str:
        text = question.casefold()
        deadlines = self._deadline_context(context)
        if not deadlines:
            if any(term in text for term in ("due", "deadline", "assignment", "截止", "作业")):
                return "No matching deadlines were found in Canvas due_at."
            return (
                "No AI provider is configured. Deadline questions can still be answered "
                "locally from Canvas due_at."
            )

        if any(term in text for term in ("today", "今天")):
            selected = [item for item in deadlines if item.get("category") == "Today"]
            heading = "Due today"
        elif any(term in text for term in ("tomorrow", "明天")):
            selected = [item for item in deadlines if item.get("category") == "Tomorrow"]
            heading = "Due tomorrow"
        elif any(term in text for term in ("this week", "本周", "这周")):
            selected = [
                item
                for item in deadlines
                if item.get("category") in {"Today", "Tomorrow", "This Week"}
            ]
            heading = "Due this week"
        elif any(term in text for term in ("first", "先做", "优先")):
            selected = [item for item in deadlines if item.get("due_at") is not None][:1]
            heading = "Work on this first by deadline"
        else:
            selected = deadlines
            heading = "Upcoming deadlines"

        if not selected:
            return f"{heading}: none found in Canvas due_at."
        lines = [heading + ":"]
        for item in selected:
            title = item.get("title") or "Untitled assignment"
            course = item.get("course_code") or item.get("course_name") or "Canvas"
            due = item.get("due_local")
            if due is None:
                due_text = "No deadline provided by Canvas"
            else:
                due_text = f"{due} ({item.get('countdown', 'Canvas due_at')})"
            lines.append(f"- {course} — {title}: {due_text}")
        lines.append("Source: Canvas due_at")
        return "\n".join(lines)
