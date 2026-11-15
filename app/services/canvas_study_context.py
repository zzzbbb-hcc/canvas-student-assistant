"""Bounded, read-only Canvas evidence retrieval for course-specific questions.

This is not a web searcher or an agent with arbitrary HTTP access. It only uses
the Canvas client's whitelisted GET endpoints, keeps downloaded files in memory,
and returns plain, credential-redacted text with stable source locators. Canvas
content is evidence, never instructions governing the assistant or its tools.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from app.models.course import Course
from app.security.canvas_resources import _same_origin_path, canvas_file_links
from app.security.html_sanitizer import safe_canvas_html
from app.services.canvas_client import CanvasClient, CanvasClientError
from app.services.chat_history import redact_chat_text
from app.services.file_preview import preview_kind
from app.storage.session_cache import session_cache


@dataclass(frozen=True, slots=True)
class StudyReadLimits:
    """Validated reading budgets; resource/security limits remain independent."""

    pages: int = 200
    source_chars: int = 100_000
    context_chars: int = 400_000

    def __post_init__(self):
        if not _valid_read_limits(self):
            raise ValueError("Unsupported study reading limits.")


def _valid_read_limits(value) -> bool:
    return (isinstance(value, StudyReadLimits)
            and type(value.pages) is int and 1 <= value.pages <= 200
            and type(value.source_chars) is int and 1 <= value.source_chars <= 100_000
            and type(value.context_chars) is int and value.source_chars <= value.context_chars <= 400_000)


STANDARD_READ_LIMITS = StudyReadLimits(pages=50, source_chars=25_000, context_chars=100_000)
EXPANDED_READ_LIMITS = StudyReadLimits()


def read_limits_for_profile(profile) -> StudyReadLimits:
    """Select a fixed bounded profile; unknown preferences use the new default."""
    return STANDARD_READ_LIMITS if isinstance(profile, str) and profile == "standard" else EXPANDED_READ_LIMITS


def _safe_read_limits(value=None) -> StudyReadLimits:
    try:
        return value if _valid_read_limits(value) else EXPANDED_READ_LIMITS
    except (AttributeError, TypeError, ValueError):
        return EXPANDED_READ_LIMITS


MAX_SOURCES = 8
MAX_CONTEXT_TEXT = EXPANDED_READ_LIMITS.context_chars
MAX_SOURCE_TEXT = EXPANDED_READ_LIMITS.source_chars
MAX_FILE_BYTES = 8_000_000
MAX_MODULES = 8
MAX_INDEX_ITEMS = 200
MAX_PDF_PAGES = EXPANDED_READ_LIMITS.pages
MAX_XML_BYTES = 2_000_000
MAX_OFFICE_EXPANSION = 12_000_000
MAX_OFFICE_ENTRIES = 500

_IGNORED = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript"}
_BREAKS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "blockquote"}
_STOP = {"how", "can", "you", "write", "writing", "the", "this", "that", "for", "and", "with", "please", "help", "assignment", "assessment", "course", "due", "deadline", "what", "does", "should", "work", "about", "need", "me", "my", "it", "is", "to", "of", "a", "an", "why", "explain", "example", "examples", "give", "outline", "structure", "summarize", "summary", "detail", "details", "more", "further", "continue"}
_CONCEPT_STOP = _STOP | {"word", "words", "page", "pages", "format", "task", "requirements", "submission", "submit", "student", "students", "report", "group", "individual", "presentation", "point", "points", "include", "criteria", "criterion", "score", "mark", "marks", "must", "will", "your", "from", "using", "use", "been", "only", "not", "also", "are", "be", "evidence", "references", "academic", "style", "section", "sections", "length", "formatting", "time", "date", "have", "which", "each", "their", "than", "these", "they", "into", "required", "following", "based", "provided", "relevant", "within", "information", "assessment", "teacher", "lecturer", "excellent", "good", "poor"}
_ALIASES = {
    "report": ("报告", "report"), "group": ("小组", "团队", "group"),
    "individual": ("个人", "individual"), "presentation": ("展示", "演讲", "汇报", "presentation"),
    "reflection": ("反思", "reflection"), "essay": ("论文", "essay"),
    "proposal": ("提案", "计划书", "proposal"), "project": ("项目", "project"),
    "rubric": ("评分", "标准", "rubric"),
}


def _value(item: Any, key: str, default=None):
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 < value < 2**63:
        return value
    return None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked: list[str] = []
        self.invalid_block_closing = False

    def handle_starttag(self, tag, attrs):
        if tag in _IGNORED:
            if tag != "embed":
                self.blocked.append(tag)
        elif not self.blocked and tag in _BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _IGNORED:
            if tag in self.blocked:
                matching = len(self.blocked) - 1 - self.blocked[::-1].index(tag)
                del self.blocked[matching:]
            elif tag != "embed":
                self.invalid_block_closing = True
        elif not self.blocked and tag in _BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(data)


def plain_canvas_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    try:
        # Fail closed on malformed blocked-tag closures before passing content
        # through the shared reader sanitizer. Such closures must not expose
        # text that was inside an object/iframe as ordinary evidence.
        validator = _PlainText()
        validator.feed(value[:250_000])
        validator.close()
        if validator.invalid_block_closing:
            return ""
        # Reuse the same HTML allowlist as the in-app reader before converting
        # to evidence text; no scripts, remote images or active HTML survive.
        parser.feed(safe_canvas_html(value[:250_000]))
        parser.close()
    except Exception:
        return ""
    text = "".join(parser.parts)
    return redact_chat_text("\n".join(line.strip() for line in text.splitlines() if line.strip()))


class _PageLinks(HTMLParser):
    def __init__(self, course_id: int, base_url: str):
        super().__init__(convert_charrefs=True)
        self.course_id, self.base_url = course_id, base_url
        self.pages: list[str] = []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        if tag in _IGNORED:
            self.blocked += 1
            return
        if self.blocked or tag != "a":
            return
        for key, value in attrs:
            if key not in {"href", "data-api-endpoint"}:
                continue
            path = _same_origin_path(value, self.base_url)
            if path is None:
                continue
            match = re.fullmatch(rf"/(?:api/v1/)?courses/{self.course_id}/pages/([^/]+)", path)
            if match:
                slug = unquote(match[1])
                if slug not in self.pages and 0 < len(slug) <= 300:
                    self.pages.append(slug)

    def handle_endtag(self, tag):
        if tag in _IGNORED:
            self.blocked = max(0, self.blocked - 1)


def _terms(value: str) -> set[str]:
    lower = value.casefold()
    terms = {word for word in re.findall(r"[a-z]{2,}|\d+", lower) if word not in _STOP and not re.fullmatch(r"\d{4,}", word)}
    for canonical, aliases in _ALIASES.items():
        if any(alias in lower for alias in aliases):
            terms.add(canonical)
    return terms


def _match_score(title: str, question: str, terms: set[str]) -> int:
    title_terms = _terms(title)
    score = sum(4 if term in _ALIASES else 2 for term in terms & title_terms)
    folded = " ".join(title.casefold().split())
    if len(folded) > 5 and folded in " ".join(question.casefold().split()):
        score += 30
    return score


def _concept_terms(text: str) -> set[str]:
    """Small deterministic index of task concepts, not an invented summary."""
    words = [word for word in re.findall(r"[a-z]{4,}", text[:50_000].casefold()) if word not in _CONCEPT_STOP]
    counts = Counter(words)
    return {word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:40]}


def _extract_pdf_text(content: bytes, *, limits: StudyReadLimits | None = None) -> tuple[str, list[str]]:
    limits = _safe_read_limits(limits)
    try:
        from pypdf import PdfReader, apply_configuration
        # Public context-local configuration enforces bounds *during* decoding,
        # not only after get_data() has already inflated a suspicious stream.
        # https://pypdf.readthedocs.io/en/stable/user/security.html
        with apply_configuration(
            maximum_declared_stream_length=MAX_XML_BYTES,
            array_based_stream_maximum_output_length=MAX_XML_BYTES,
            jbig2_maximum_output_length=MAX_XML_BYTES,
            lzw_maximum_output_length=MAX_XML_BYTES,
            run_length_maximum_output_length=MAX_XML_BYTES,
            zlib_maximum_output_length=MAX_XML_BYTES,
            zlib_maximum_recovery_input_length=250_000,
            brotli_maximum_output_length=MAX_XML_BYTES,
            stream_filters_maximum_length=4,
            stream_decoding_work_maximum_length=8_000_000,
            stream_parsing_work_maximum_count=100_000,
            image_maximum_buffer_size=MAX_XML_BYTES,
            xmp_maximum_input_length=250_000,
            page_tree_maximum_entries=500,
            page_tree_maximum_depth=30,
            xform_maximum_invocations_per_extraction=100,
            jbig2dec_binary=None,
            disable_legacy_handling=True,
        ):
            reader = PdfReader(BytesIO(content), strict=False, root_object_recovery_limit=1_000)
            if reader.is_encrypted:
                return "", ["PDF 已加密，未读取。"]
            parts: list[str] = []
            total = 0
            warnings: list[str] = []
            page_count = len(reader.pages)
            consumed_pages = 0
            character_truncated = False
            for number in range(min(page_count, limits.pages)):
                if total >= limits.source_chars:
                    character_truncated = True
                    break
                text = redact_chat_text(reader.pages[number].extract_text() or "")
                segment = ("\n\n" if parts else "") + f"[第 {number + 1} 页]\n{text}"
                remaining = limits.source_chars - total
                consumed_pages += 1
                parts.append(segment[:remaining])
                total += len(parts[-1])
                if len(segment) > remaining:
                    character_truncated = True
                    break
            if page_count > limits.pages and consumed_pages == limits.pages:
                warnings.append(f"PDF 达到页数上限，仅提取前 {limits.pages} 页；其余页未读取。")
            if character_truncated:
                warnings.append(f"PDF 达到单份资料 {limits.source_chars:,} 字符上限，部分文字未提取。")
            result = "".join(parts).strip()
            actual = re.sub(r"\[第 \d+ 页\]", "", result).strip()
            if not actual:
                return "", warnings or ["PDF 未提取到文字，可能是扫描件；需要 OCR，不能视为已读课件。"]
            return result, warnings
    except ImportError:
        return "", ["PDF 安全文本组件未安装或版本不兼容，未读取此 PDF。"]
    except Exception:
        return "", ["PDF 文本提取失败或触及安全资源上限，未读取此资料。"]


def extract_study_file_text(
    metadata: Mapping, content: bytes, *, limits: StudyReadLimits | None = None,
) -> tuple[str, list[str]]:
    """Extract bounded text only. No macros, external XML, rendering or OCR."""
    limits = _safe_read_limits(limits)
    if not isinstance(content, bytes) or len(content) > MAX_FILE_BYTES:
        return "", ["资料超过 8 MB，未读取。"]
    kind = preview_kind(dict(metadata))
    if kind == "text":
        mime = str(metadata.get("content-type") or "").casefold()
        name = str(metadata.get("display_name") or metadata.get("filename") or "").casefold()
        text = content.decode("utf-8-sig", errors="replace")
        if mime == "text/html" or name.endswith((".html", ".htm")):
            text = plain_canvas_text(text)
        text = redact_chat_text(text)
        return text[:limits.source_chars], ([f"文本资料达到单份资料 {limits.source_chars:,} 字符上限，仅读取前部内容。"]
                                           if len(text) > limits.source_chars else [])
    if kind == "pdf":
        return _extract_pdf_text(content, limits=limits)
    if kind not in {"docx", "pptx"}:
        return "", ["此资料格式暂不支持文本提取，未读取。"]
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_OFFICE_ENTRIES or sum(info.file_size for info in entries) > MAX_OFFICE_EXPANSION:
                return "", ["Office 资料解压后过大，未读取。"]
            if any(info.flag_bits & 1 for info in entries):
                return "", ["Office 资料已加密，未读取。"]
            if kind == "docx":
                names = ["word/document.xml"]
            else:
                names = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                               key=lambda name: int(re.search(r"slide(\d+)", name)[1]))
            warnings = []
            parts = []
            total = 0
            consumed_pages = 0
            character_truncated = False
            for number, name in enumerate(names[:limits.pages], 1):
                if total >= limits.source_chars:
                    character_truncated = True
                    break
                consumed_pages += 1
                info = archive.getinfo(name)
                if info.file_size > MAX_XML_BYTES:
                    warnings.append("Office 资料的某个文本段过大，已跳过。")
                    continue
                data = archive.read(info)
                if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    return "", ["Office XML 含不受支持的实体声明，未读取。"]
                root = ElementTree.fromstring(data)
                if kind == "docx":
                    paragraphs = ["".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t"))
                                  for paragraph in root.iter() if paragraph.tag.endswith("}p")]
                    text = "\n".join(part for part in paragraphs if part.strip())
                else:
                    text = "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
                    if text.strip():
                        text = f"[第 {number} 张幻灯片]\n{text}"
                if text.strip():
                    segment = ("\n\n" if parts else "") + redact_chat_text(text)
                    remaining = limits.source_chars - total
                    parts.append(segment[:remaining])
                    total += len(parts[-1])
                    if len(segment) > remaining:
                        character_truncated = True
                        break
            if kind == "pptx" and len(names) > limits.pages and consumed_pages == limits.pages:
                warnings.append(f"幻灯片达到页数上限，仅读取前 {limits.pages} 张；其余幻灯片未读取。")
            if character_truncated:
                warnings.append(f"Office 资料达到单份资料 {limits.source_chars:,} 字符上限，部分文字未提取。")
            result = "".join(parts).strip()
            return result, warnings if result else warnings + ["Office 资料没有可提取的文字，未读取。"]
    except (BadZipFile, KeyError, ElementTree.ParseError, OSError, ValueError, RuntimeError):
        return "", ["Office 资料无法安全读取，未读取。"]


class CanvasStudyContextRetriever:
    def __init__(self, credentials, *, client_factory=CanvasClient, cache=session_cache, limits_provider=None):
        self._credentials = credentials
        self._client_factory = client_factory
        self._cache = cache
        self._limits_provider = limits_provider

    @staticmethod
    def _course_mentions(question: str, courses: Sequence[Course]) -> list[Course]:
        normalized = re.sub(r"\s+", "", question).casefold()
        codes = set(re.findall(r"(?<![a-z0-9])[a-z]{2,8}\s*\d{4}(?!\d)", question.casefold()))
        codes = {re.sub(r"\s+", "", code) for code in codes}
        numbers = set(re.findall(r"(?<!\d)\d{4}(?!\d)", question))
        result = []
        for course in courses:
            labels = f"{course.course_code} {course.name}".casefold()
            course_codes = set(re.findall(r"[a-z]{2,8}\s*\d{4}", labels))
            course_codes = {re.sub(r"\s+", "", code) for code in course_codes}
            course_numbers = set(re.findall(r"(?<!\d)\d{4}(?!\d)", labels))
            full_name = re.sub(r"\s+", "", course.name).casefold()
            if codes & course_codes or numbers & course_numbers or (len(full_name) > 5 and full_name in normalized):
                result.append(course)
        return result

    @staticmethod
    def _history_questions(history) -> list[str]:
        return [str(_value(item, "content", ""))[:8_000] for item in list(history)[-8:]
                if _value(item, "role") == "user" and isinstance(_value(item, "content"), str)]

    def retrieve(self, question: str, course_id: int | None = None, history=()) -> dict:
        try:
            limits = _safe_read_limits(self._limits_provider() if self._limits_provider is not None else None)
        except Exception:
            limits = EXPANDED_READ_LIMITS
        result = {"course_id": None, "course_label": "", "status": "notfound", "candidates": [], "sources": [], "warnings": []}
        if not isinstance(question, str) or not question.strip() or len(question) > 8_000:
            result["warnings"].append("请输入具体的课程和作业问题。")
            return result
        if course_id is not None and _positive_id(course_id) is None:
            result["warnings"].append("课程标识无效。")
            return result
        try:
            token = self._credentials.get_canvas_token()
        except Exception:
            token = None
        if not token:
            result["warnings"].append("尚未配置 Canvas 连接，无法读取作业要求和课件。")
            return result
        try:
            with self._client_factory(token) as client:
                return self._retrieve(client, question, course_id, history, result, limits=limits)
        except Exception:
            # Never echo transport errors, request URLs or headers.
            result["warnings"].append("Canvas 资料读取失败，请检查连接后重试；未读取的资料不会作为依据。")
            return result

    def _retrieve(self, client, question, course_id, history, result, *, limits: StudyReadLimits):
        def parse_courses(raw):
            parsed = []
            for item in list(raw)[:MAX_INDEX_ITEMS]:
                try:
                    parsed.append(item if isinstance(item, Course) else Course.model_validate(item))
                except (TypeError, ValueError):
                    continue
            return parsed

        cached_courses = self._cache.get("courses")
        courses = parse_courses(cached_courses or client.get_study_courses())
        explicit_course_reference = bool(re.search(r"[A-Za-z]{2,8}\s*\d{4}|(?<!\d)\d{4}(?!\d)", question))
        # The UI course cache can predate a new enrolment. Refresh only when it
        # cannot resolve an explicit unit or the selected group's course; avoid
        # both unnecessary inventory calls and false negatives from stale data.
        missing_selected = course_id is not None and not any(course.id == course_id for course in courses)
        missing_named = explicit_course_reference and not self._course_mentions(question, courses)
        if cached_courses and (missing_selected or missing_named):
            try:
                courses = parse_courses(client.get_study_courses())
            except Exception:
                result["warnings"].append("课程列表刷新失败，暂用本机已有课程；结果可能不完整。")
        previous = self._history_questions(history)
        mentions = self._course_mentions(question, courses)
        if not mentions and course_id is None:
            for previous_question in reversed(previous):
                mentions = self._course_mentions(previous_question, courses)
                if mentions:
                    break
        if course_id is not None:
            selected = next((course for course in courses if course.id == course_id), None)
            if selected and (not mentions or all(item.id == course_id for item in mentions)):
                mentions = [selected]
            elif selected and mentions:
                mentions = [selected] + [item for item in mentions if item.id != course_id]
        if not mentions and len(courses) == 1 and not explicit_course_reference:
            mentions = courses
        if len(mentions) != 1:
            result["status"] = "ambiguous" if courses else "notfound"
            candidates = mentions or courses
            result["candidates"] = [{"id": course.id, "course_id": course.id, "title": redact_chat_text(course.course_code or course.name), "kind": "course"}
                                    for course in candidates[:10]]
            result["warnings"].append("请确认课程（例如 PMGT5891），避免引用其他课程的资料。")
            return result
        course = mentions[0]
        result["course_id"], result["course_label"] = course.id, redact_chat_text(course.course_code or course.name)
        cached = self._cache.get("assignments") or []
        cached_assignments = [{"id": _value(item, "assignment_id"), "name": _value(item, "title")}
                              for item in list(cached)[:2_000] if _value(item, "course_id") == course.id and not _value(item, "quiz_engine")]
        # Study questions need the current assignment inventory, not merely the
        # deadline cache. A nonempty cache can be partial and must neither hide
        # a new report nor make one of two similarly named reports look unique.
        # This is one bounded read for this selected course, with cache only as
        # an explicitly disclosed fallback if refreshing fails.
        try:
            assignments = client.get_study_assignments(course.id)
        except Exception:
            assignments = cached_assignments
            result["warnings"].append("作业列表刷新失败，暂用本机已有列表；结果可能不完整。")
        if getattr(client, "study_index_truncated", False):
            result["warnings"].append("资料索引达到本次读取上限，搜索结果可能不完整。")
        terms = _terms(question)
        query = question
        if not terms and previous:
            query += " " + previous[-1]
            terms = _terms(query)
        def rank(search_query, search_terms):
            matches = []
            for assignment in list(assignments)[:MAX_INDEX_ITEMS]:
                identity, title = _positive_id(assignment.get("id")), assignment.get("name")
                if identity is None or not isinstance(title, str):
                    continue
                score = _match_score(title, search_query, search_terms)
                if score > 0:
                    matches.append((score, identity, title))
            return sorted(matches, key=lambda item: (-item[0], item[1]))

        ranked = rank(query, terms)
        fresh_assignment_hint = bool((_terms(question) & (set(_ALIASES) - {"rubric"})) or re.search(r"(?i)\ba\d+\b", question))
        if not ranked and previous and not fresh_assignment_hint:
            for previous_question in reversed(previous):
                previous_courses = self._course_mentions(previous_question, courses)
                if previous_courses and any(item.id != course.id for item in previous_courses):
                    continue
                expanded = question + " " + previous_question
                ranked = rank(expanded, _terms(expanded))
                if ranked:
                    query, terms = expanded, _terms(expanded)
                    break
        if not ranked:
            result["warnings"].append("没有找到对应作业。请提供作业名称、A1/A2 编号或更具体关键词。")
            return result
        close = [item for item in ranked if item[0] >= ranked[0][0] - 1]
        if len(close) > 1:
            result["status"] = "ambiguous"
            result["candidates"] = [{"id": identity, "course_id": course.id, "title": redact_chat_text(title), "kind": "assignment"}
                                    for _, identity, title in close[:10]]
            result["warnings"].append("找到多个相近作业，请选择或补充完整名称，不能自动猜测。")
            return result
        _, assignment_id, title = ranked[0]
        details = client.get_assignment_study_details(course.id, assignment_id)
        due_at = details.get("due_at")
        if due_at is not None:
            try:
                if not isinstance(due_at, str) or len(due_at) > 64 or datetime.fromisoformat(due_at.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError("Invalid Canvas date")
            except ValueError:
                due_at = None
                result["warnings"].append("Canvas due_at 格式无效，不能确认截止时间。")
        result.update(status="ok", assignment_id=assignment_id, assignment_title=redact_chat_text(title), due_at=due_at)
        description = details.get("description") if isinstance(details.get("description"), str) else ""
        self._add_source(result, f"assignment:{course.id}:{assignment_id}", title, "assignment",
                         plain_canvas_text(description), f"/courses/{course.id}/assignments/{assignment_id}", limits=limits)
        if not plain_canvas_text(description):
            result["warnings"].append("Canvas 作业介绍没有可读取的正文；不会编造作业要求。")
        rubric = details.get("rubric")
        if isinstance(rubric, list) and rubric:
            lines = []
            for criterion in rubric[:100]:
                if not isinstance(criterion, Mapping):
                    continue
                lines.append(f"{plain_canvas_text(criterion.get('description'))} ({criterion.get('points', '未提供')} 分)")
                lines.append(plain_canvas_text(criterion.get("long_description")))
                for rating in (criterion.get("ratings") or [])[:20]:
                    if isinstance(rating, Mapping):
                        lines.append(f"- {plain_canvas_text(rating.get('description'))}: {plain_canvas_text(rating.get('long_description'))} ({rating.get('points', '未提供')} 分)")
            self._add_source(result, f"rubric:{course.id}:{assignment_id}", f"{title} · 评分标准", "rubric", "\n".join(lines),
                             f"/courses/{course.id}/assignments/{assignment_id}#rubric", limits=limits)
        else:
            result["warnings"].append("Canvas 没有返回结构化评分标准；可能写在附件中，未读取前不能确认。")
        base_url = getattr(client, "BASE_URL", CanvasClient.BASE_URL)
        file_queue = [(link.file_id, link.title) for link in canvas_file_links(description, base_url)][:MAX_SOURCES]
        page_parser = _PageLinks(course.id, base_url)
        page_parser.feed(description[:250_000])
        page_queue = [(slug, slug) for slug in page_parser.pages[:MAX_SOURCES]]
        concept_text = plain_canvas_text(description)
        if isinstance(rubric, list):
            concept_text += "\n" + "\n".join(plain_canvas_text(item.get("description")) + " " + plain_canvas_text(item.get("long_description"))
                                             for item in rubric[:100] if isinstance(item, Mapping))
        related_terms = _terms(title) | terms | _concept_terms(concept_text)
        try:
            modules = client.get_study_modules(course.id)
            if len(modules) > MAX_MODULES:
                result["warnings"].append("本次最多检索 8 个相关模块，其他模块尚未遍历。")
            modules = sorted(modules, key=lambda item: -_match_score(str(item.get("name", "")), query, related_terms))[:MAX_MODULES]
            for module in modules:
                identity = _positive_id(module.get("id"))
                if identity is None:
                    continue
                items = client.get_study_module_items(course.id, identity)
                for item in items[:MAX_INDEX_ITEMS]:
                    item_title = str(item.get("title") or "")
                    if _match_score(item_title, title, related_terms) < 4:
                        continue
                    if item.get("type") == "File" and _positive_id(item.get("content_id")):
                        file_queue.append((item["content_id"], item_title))
                    elif item.get("type") == "Page" and isinstance(item.get("page_url"), str):
                        page_queue.append((item["page_url"], item_title))
        except Exception:
            result["warnings"].append("部分模块索引不可读取，相关课件可能不完整。")
        if len(page_queue) < 3:
            try:
                pages = client.get_study_pages(course.id)
                for page in pages[:MAX_INDEX_ITEMS]:
                    page_title = str(page.get("title") or "")
                    slug = page.get("url")
                    if isinstance(slug, str) and 0 < len(slug) <= 300 and _match_score(page_title, title, related_terms) >= 4:
                        page_queue.append((slug, page_title))
            except Exception:
                result["warnings"].append("页面索引不可读取，相关课程说明可能不完整。")
        # Direct assignment links are higher confidence than title matches.
        seen_pages = set()
        for slug, page_title in page_queue[:3]:
            if slug in seen_pages or len(result["sources"]) >= MAX_SOURCES:
                continue
            seen_pages.add(slug)
            try:
                page = client.get_page(course.id, slug)
                body = page.get("body") if isinstance(page.get("body"), str) else ""
                self._add_source(result, f"page:{course.id}:{slug}", page.get("title") or page_title, "page",
                                 plain_canvas_text(body), f"/courses/{course.id}/pages/{slug}", limits=limits)
                file_queue.extend((link.file_id, link.title) for link in canvas_file_links(body, base_url))
            except Exception:
                result["warnings"].append("某个相关 Canvas 页面不可读取，未作为依据。")
        seen_files = set()
        for file_id, file_title in file_queue[:MAX_SOURCES]:
            if file_id in seen_files or len(result["sources"]) >= MAX_SOURCES:
                continue
            seen_files.add(file_id)
            try:
                metadata, content = client.get_study_file_content(file_id, max_bytes=MAX_FILE_BYTES)
                text, warnings = extract_study_file_text(metadata, content, limits=limits)
                result["warnings"].extend(warnings)
                self._add_source(result, f"file:{file_id}", metadata.get("display_name") or file_title, "file", text, f"/files/{file_id}", limits=limits)
            except Exception:
                result["warnings"].append("某份附件不可读取或超过大小限制，未作为依据。")
        if getattr(client, "study_index_truncated", False):
            result["warnings"].append("资料索引达到本次读取上限，搜索结果可能不完整。")
        result["warnings"] = list(dict.fromkeys(result["warnings"]))[:20]
        return result

    @staticmethod
    def _add_source(result, identity, title, kind, text, locator, *, limits: StudyReadLimits | None = None):
        limits = _safe_read_limits(limits)
        if not text or len(result["sources"]) >= MAX_SOURCES:
            return
        remaining = limits.context_chars - sum(len(source["text"]) for source in result["sources"])
        if remaining <= 0:
            result["warnings"].append(f"本次资料总量达到 {limits.context_chars:,} 字符上限，部分来源未加入。")
            return
        full_safe = redact_chat_text(text)
        safe = full_safe[:min(limits.source_chars, remaining)]
        result["sources"].append({"id": redact_chat_text(str(identity)), "title": redact_chat_text(str(title))[:300], "kind": kind,
                                  "text": safe, "locator": redact_chat_text(str(locator))})
        if len(full_safe) > limits.source_chars:
            result["warnings"].append(f"单个来源达到 {limits.source_chars:,} 字符上限，本次只读取限定文本范围。")
        if min(len(full_safe), limits.source_chars) > remaining:
            result["warnings"].append(f"本次资料总量达到 {limits.context_chars:,} 字符上限，部分文字未加入。")
