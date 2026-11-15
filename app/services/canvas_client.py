"""Minimal read-only Canvas LMS client."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import time
import ipaddress
import math

import httpx

from app.models.assignment import Assignment
from app.models.course import Course


class CanvasClientError(Exception):
    """A safe, user-facing Canvas connection error."""


class CanvasClient:
    BASE_URL = "https://canvas.sydney.edu.au"

    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
        sleep=time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Canvas base URL must use HTTPS.")
        if not token:
            raise CanvasClientError("No Canvas token is stored.")
        self._origin = (parsed.scheme, parsed.netloc)
        self._sleep = sleep
        self.quiz_coverage_warnings: list[str] = []
        self.study_index_truncated = False
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        self._public_client = httpx.Client(
            headers={"Accept": "image/*"}, timeout=timeout, transport=transport, follow_redirects=False
        )

    def close(self) -> None:
        self._client.close()
        self._public_client.close()

    def __enter__(self) -> "CanvasClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _safe_url(self, url: str) -> str:
        absolute = urljoin(str(self._client.base_url) + "/", url)
        parsed = urlparse(absolute)
        if (parsed.scheme, parsed.netloc) != self._origin:
            raise CanvasClientError("Could not connect to Canvas.")
        return absolute

    def _request(self, url: str, params: dict[str, object] | None = None) -> httpx.Response:
        safe_url = self._safe_url(url)
        response = None
        for attempt in range(3):
            try:
                response = self._client.get(safe_url, params=params)
            except httpx.TimeoutException as exc:
                if attempt == 2:
                    raise CanvasClientError("Canvas connection timed out.") from exc
                self._sleep(0.5 * (attempt + 1))
                continue
            except httpx.HTTPError as exc:
                raise CanvasClientError("Could not connect to Canvas.") from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    break
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 2.0) if retry_after else 0.5 * (attempt + 1)
                except ValueError:
                    delay = 0.5 * (attempt + 1)
                self._sleep(delay)
                continue
            break

        if response is None:
            raise CanvasClientError("Could not connect to Canvas.")

        if response.status_code == 401:
            raise CanvasClientError("Canvas token is invalid or expired.")
        if response.status_code == 403:
            raise CanvasClientError("Canvas access denied.")
        if response.is_error:
            raise CanvasClientError("Could not connect to Canvas.")
        return response

    def _get_paginated(
        self, path: str, params: dict[str, object] | None = None, *, scoped_path: bool = False
    ) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = path
        next_params = params
        initial_path = urlparse(self._safe_url(path)).path.rstrip("/")
        seen_urls: set[str] = set()
        while next_url:
            if scoped_path:
                absolute_url = self._safe_url(next_url)
                if urlparse(absolute_url).path.rstrip("/") != initial_path or absolute_url in seen_urls:
                    raise CanvasClientError("Could not connect to Canvas.")
                seen_urls.add(absolute_url)
            response = self._request(next_url, next_params)
            next_params = None
            try:
                page = response.json()
            except ValueError as exc:
                raise CanvasClientError("Could not connect to Canvas.") from exc
            if not isinstance(page, list):
                raise CanvasClientError("Could not connect to Canvas.")
            items.extend(item for item in page if isinstance(item, dict))
            next_url = response.links.get("next", {}).get("url")
            if next_url:
                next_url = urljoin(str(response.url), next_url)
        return items

    def get_current_user(self) -> dict:
        response = self._request("/api/v1/users/self")
        try:
            data = response.json()
        except ValueError as exc:
            raise CanvasClientError("Could not connect to Canvas.") from exc
        if not isinstance(data, dict):
            raise CanvasClientError("Could not connect to Canvas.")
        return data

    def get_courses(self) -> list[Course]:
        data = self._get_paginated(
            "/api/v1/courses",
            {"enrollment_state": "active", "state[]": "available", "per_page": 100},
        )
        courses = []
        for item in data:
            if item.get("access_restricted_by_date"):
                continue
            try:
                courses.append(Course.model_validate(item))
            except ValueError:
                continue
        return courses

    def get_course(self, course_id: int) -> Course:
        try:
            return Course.model_validate(self._request(f"/api/v1/courses/{course_id}").json())
        except (ValueError, TypeError) as exc:
            raise CanvasClientError("Could not connect to Canvas.") from exc

    @staticmethod
    def _assignment(item: dict, course: Course) -> Assignment:
        submission = item.get("submission")
        submission = submission if isinstance(submission, dict) else {}
        quiz_id = CanvasClient._positive_id(item.get("quiz_id"))
        submission_types = item.get("submission_types") or []
        # Canvas's is_quiz_assignment identifies Classic graded quizzes;
        # is_quiz_lti_assignment identifies New Quizzes in the official source.
        if item.get("is_quiz_lti_assignment") is True:
            quiz_engine = "new"
            quiz_id = CanvasClient._positive_id(item.get("id"))
        elif quiz_id or "online_quiz" in submission_types or item.get("is_quiz_assignment") is True:
            quiz_engine = "classic"
        else:
            quiz_engine = None
        return Assignment(
            assignment_id=item["id"], course_id=course.id, course_name=course.name,
            course_code=course.course_code, title=item.get("name") or "Untitled assignment",
            description=item.get("description"), due_at=item.get("due_at"), unlock_at=item.get("unlock_at"),
            lock_at=item.get("lock_at"), html_url=item.get("html_url"),
            submission_types=submission_types, published=item.get("published", True),
            has_submitted_submissions=item.get("has_submitted_submissions", False),
            submission_status=submission.get("workflow_state"),
            submission_submitted_at=submission.get("submitted_at"),
            submission_attempt=submission.get("attempt"),
            submission_missing=submission.get("missing"),
            quiz_id=quiz_id, quiz_engine=quiz_engine,
            quiz_assignment_id=CanvasClient._positive_id(item.get("id")) if quiz_engine else None,
            **({"quiz_locked": item.get("locked_for_user") is True}
               if quiz_engine and "locked_for_user" in item else {}),
        )

    def get_assignments(self, course_id: int, course: Course | None = None) -> list[Assignment]:
        course = course or self.get_course(course_id)
        data = self._get_paginated(f"/api/v1/courses/{course_id}/assignments", {"include[]": "submission", "override_assignment_dates": "true", "per_page": 100})
        return [self._assignment(item, course) for item in data if item.get("id") is not None]

    def get_upcoming_assignments(self) -> list[Assignment]:
        """Include graded, practice and survey quizzes without starting attempts.

        Course jobs are bounded to four concurrent connections; each endpoint
        is paginated, with no per-quiz detail requests during a refresh.
        httpx.Client can be shared between threads.
        """
        self.quiz_coverage_warnings = []
        courses = self.get_courses()
        if not courses:
            return []
        assignments: list[Assignment] = []
        with ThreadPoolExecutor(max_workers=min(4, len(courses))) as executor:
            for course_assignments, warnings in executor.map(self._course_deadline_items, courses):
                assignments.extend(course_assignments)
                self.quiz_coverage_warnings.extend(warnings)
        return assignments

    @staticmethod
    def _positive_id(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.isascii() and value.isdigit():
            parsed = int(value)
            return parsed if parsed > 0 else None
        return None

    @staticmethod
    def _quiz_time_limit(item: dict, engine: str) -> float | None:
        if engine == "new":
            settings = item.get("quiz_settings")
            if not isinstance(settings, dict) or settings.get("has_time_limit") is not True:
                return None
            raw = settings.get("session_time_limit_in_seconds")
        else:
            raw = item.get("time_limit")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        minutes = raw / 60 if engine == "new" else raw
        return float(minutes) if math.isfinite(minutes) and minutes > 0 else None

    @classmethod
    def _quiz_assignment(cls, item: dict, course: Course, engine: str) -> Assignment:
        quiz_id = cls._positive_id(item.get("id"))
        if quiz_id is None:
            raise ValueError("Quiz identifier unavailable.")
        # New Quiz API routes are keyed by the associated assignment ID.
        linked_id = cls._positive_id(item.get("assignment_id"))
        if engine == "new":
            linked_id = linked_id or quiz_id
        # A stable, collision-resistant negative namespace for course + engine
        # + quiz. Hashing keeps even Canvas's large global IDs inside SQLite's
        # signed 64-bit range; real assignment IDs remain positive.
        identity = f"canvas-quiz:{course.id}:{engine}:{quiz_id}".encode("ascii")
        digest = hashlib.blake2b(identity, digest_size=8).digest()
        local_id = linked_id or -1 - (int.from_bytes(digest, "big") & ((1 << 63) - 1))
        return Assignment(
            assignment_id=local_id, course_id=course.id,
            course_name=course.name, course_code=course.course_code,
            title=item.get("title") or "Untitled quiz",
            description=item.get("instructions" if engine == "new" else "description"),
            due_at=item.get("due_at"), unlock_at=item.get("unlock_at"),
            lock_at=item.get("lock_at"),
            published=item.get("published", True),
            quiz_id=quiz_id, quiz_engine=engine,
            quiz_type=item.get("quiz_type") if engine == "classic" else "assignment",
            quiz_time_limit=cls._quiz_time_limit(item, engine),
            quiz_locked=item.get("locked_for_user") is True,
            quiz_assignment_id=linked_id,
            # The list API does not establish the current user's attempt state.
            # Do not infer completion from publication, a course-wide flag or score.
            submission_status=None,
        )

    @staticmethod
    def _merge_quiz_metadata(assignment: Assignment, quiz: Assignment) -> Assignment:
        updates = {
            "quiz_id": quiz.quiz_id,
            "quiz_engine": quiz.quiz_engine,
            "quiz_type": quiz.quiz_type,
            "quiz_time_limit": quiz.quiz_time_limit,
            "quiz_locked": assignment.quiz_locked or quiz.quiz_locked,
            "quiz_assignment_id": quiz.quiz_assignment_id or assignment.quiz_assignment_id,
        }
        if not assignment.description and quiz.description:
            updates["description"] = quiz.description
        # Assignment dates already include the requesting student's overrides,
        # including an explicit None. Never replace them with quiz base dates.
        return assignment.model_copy(update=updates)

    def _course_deadline_items(self, course: Course) -> tuple[list[Assignment], list[str]]:
        items: list[Assignment] = []
        by_assignment_id: dict[int, int] = {}
        by_quiz: dict[tuple[str, int], int] = {}
        warnings: list[str] = []
        course_label = f"{course.course_code or course.name} (Course {course.id})"
        try:
            course_assignments = self.get_assignments(course.id, course)
        except CanvasClientError:
            # A course may expose practice quizzes but deny its assignment
            # list. Keep discovering accessible content, including other
            # courses, instead of failing the entire deadline refresh.
            course_assignments = []
            warnings.append(
                f"{course_label}: assignment list unavailable; assignment deadlines "
                "and quiz submission status may be incomplete."
            )
        for assignment in course_assignments:
            if assignment.assignment_id in by_assignment_id:
                continue
            by_assignment_id[assignment.assignment_id] = len(items)
            if assignment.quiz_engine and assignment.quiz_id:
                by_quiz[(assignment.quiz_engine, assignment.quiz_id)] = len(items)
            items.append(assignment)
        for engine, path in (
            ("classic", f"/api/v1/courses/{course.id}/quizzes"),
            ("new", f"/api/quiz/v1/courses/{course.id}/quizzes"),
        ):
            warning = f"{course_label}: {engine.title()} quiz list unavailable; quiz deadlines may be incomplete."
            try:
                raw_quizzes = self._get_paginated(path, {"per_page": 100}, scoped_path=True)
            except CanvasClientError:
                warnings.append(warning)
                continue
            for raw_quiz in raw_quizzes:
                try:
                    quiz = self._quiz_assignment(raw_quiz, course, engine)
                except (ValueError, TypeError):
                    if warning not in warnings:
                        warnings.append(warning)
                    continue
                key = (engine, quiz.quiz_id)
                index = by_assignment_id.get(quiz.quiz_assignment_id)
                if index is None:
                    index = by_quiz.get(key)
                if index is None:
                    index = len(items)
                    items.append(quiz)
                    by_assignment_id[quiz.assignment_id] = index
                else:
                    items[index] = self._merge_quiz_metadata(items[index], quiz)
                by_quiz[key] = index
        return items, warnings

    def get_quiz_assignment(self, course_id: int, quiz_id: int, *, engine: str = "classic") -> Assignment:
        """Fetch instructions/timing only, never questions, answers or attempts."""
        course_id = self._positive_id(course_id)
        quiz_id = self._positive_id(quiz_id)
        if engine not in {"classic", "new"} or quiz_id is None or course_id is None:
            raise CanvasClientError("Could not load this Canvas quiz.")
        course = self.get_course(course_id)
        prefix = "/api/quiz/v1" if engine == "new" else "/api/v1"
        try:
            data = self._request(f"{prefix}/courses/{course_id}/quizzes/{quiz_id}").json()
            if not isinstance(data, dict):
                raise ValueError("Invalid quiz response.")
            return self._quiz_assignment(data, course, engine)
        except (ValueError, TypeError) as exc:
            raise CanvasClientError("Could not load this Canvas quiz.") from exc

    def get_assignment(self, course_id: int, assignment_id: int) -> Assignment:
        course = self.get_course(course_id)
        data = self._request(f"/api/v1/courses/{course_id}/assignments/{assignment_id}", {"include[]": "submission", "override_assignment_dates": "true"}).json()
        if not isinstance(data, dict): raise CanvasClientError("Could not connect to Canvas.")
        return self._assignment(data, course)

    def _study_list(self, path: str, params: dict | None = None, *, max_pages: int = 2) -> list[dict]:
        """Bounded, same-endpoint index reads for on-demand study retrieval."""
        rows: list[dict] = []
        next_url = path
        initial_path = urlparse(self._safe_url(path)).path.rstrip("/")
        seen: set[str] = set()
        for _ in range(max_pages):
            if not next_url:
                break
            absolute = self._safe_url(next_url)
            if absolute in seen or urlparse(absolute).path.rstrip("/") != initial_path:
                raise CanvasClientError("Could not load Canvas study materials.")
            seen.add(absolute)
            response = self._request(absolute, params)
            params = None
            if len(response.content) > 2_000_000:
                raise CanvasClientError("Canvas study index is too large.")
            try:
                data = response.json()
            except ValueError:
                raise CanvasClientError("Could not load Canvas study materials.") from None
            if not isinstance(data, list):
                raise CanvasClientError("Could not load Canvas study materials.")
            if len(data) > 100:
                self.study_index_truncated = True
            rows.extend(row for row in data[:100] if isinstance(row, dict))
            next_url = response.links.get("next", {}).get("url")
            if next_url:
                next_url = urljoin(str(response.url), next_url)
        if next_url:
            self.study_index_truncated = True
        return rows

    def get_study_courses(self) -> list[Course]:
        rows = self._study_list("/api/v1/courses", {
            "enrollment_state": "active", "state[]": "available", "per_page": 100,
        })
        courses = []
        for row in rows:
            if row.get("access_restricted_by_date"):
                continue
            try:
                courses.append(Course.model_validate(row))
            except ValueError:
                continue
        return courses

    def get_study_assignments(self, course_id: int) -> list[dict]:
        course_id = self._positive_id(course_id)
        if course_id is None:
            raise CanvasClientError("Could not load Canvas study materials.")
        rows = self._study_list(f"/api/v1/courses/{course_id}/assignments", {
            "override_assignment_dates": "true", "per_page": 100,
        })
        return [{key: row.get(key) for key in ("id", "name", "due_at", "published")} for row in rows]

    def get_assignment_study_details(self, course_id: int, assignment_id: int) -> dict:
        """Read instructions/rubric without student submissions or identities.

        Canvas includes rubric criteria on an associated assignment. Explicit
        overrides preserve the requesting student's authoritative due_at.
        """
        course_id, assignment_id = self._positive_id(course_id), self._positive_id(assignment_id)
        if course_id is None or assignment_id is None:
            raise CanvasClientError("Could not load Canvas study materials.")
        try:
            response = self._request(f"/api/v1/courses/{course_id}/assignments/{assignment_id}", {
                "override_assignment_dates": "true",
            })
            if len(response.content) > 2_000_000:
                raise CanvasClientError("Canvas study instructions are too large.")
            data = response.json()
        except ValueError:
            raise CanvasClientError("Could not load Canvas study materials.") from None
        if not isinstance(data, dict):
            raise CanvasClientError("Could not load Canvas study materials.")
        result = {key: data.get(key) for key in (
            "id", "name", "description", "due_at", "points_possible", "submission_types",
            "allowed_extensions", "locked_for_user", "use_rubric_for_grading",
        )}
        rubric = data.get("rubric")
        result["rubric"] = [{
            key: criterion.get(key) for key in ("id", "description", "long_description", "points")
        } | {"ratings": [{key: rating.get(key) for key in ("id", "description", "long_description", "points")}
                        for rating in (criterion.get("ratings") if isinstance(criterion.get("ratings"), list) else [])[:20] if isinstance(rating, dict)]}
            for criterion in rubric[:100] if isinstance(criterion, dict)] if isinstance(rubric, list) else []
        return result

    def get_study_modules(self, course_id: int) -> list[dict]:
        course_id = self._positive_id(course_id)
        if course_id is None:
            raise CanvasClientError("Could not load Canvas study materials.")
        return self._study_list(f"/api/v1/courses/{course_id}/modules", {"per_page": 100}, max_pages=1)

    def get_study_module_items(self, course_id: int, module_id: int) -> list[dict]:
        course_id, module_id = self._positive_id(course_id), self._positive_id(module_id)
        if course_id is None or module_id is None:
            raise CanvasClientError("Could not load Canvas study materials.")
        return self._study_list(f"/api/v1/courses/{course_id}/modules/{module_id}/items", {"per_page": 100}, max_pages=1)

    def get_study_pages(self, course_id: int) -> list[dict]:
        course_id = self._positive_id(course_id)
        if course_id is None:
            raise CanvasClientError("Could not load Canvas study materials.")
        return self._study_list(f"/api/v1/courses/{course_id}/pages", {"per_page": 100}, max_pages=1)

    def get_study_file_content(self, file_id: int, max_bytes: int = 8_000_000) -> tuple[dict, bytes]:
        """Stream at most the requested bytes; never forward PAT on redirects."""
        file_id = self._positive_id(file_id)
        if file_id is None or not isinstance(max_bytes, int) or not 0 < max_bytes <= 8_000_000:
            raise CanvasClientError("Could not load Canvas study materials.")
        metadata = self.get_file(file_id)
        size = metadata.get("size")
        if isinstance(size, int) and size > max_bytes:
            raise CanvasClientError("Canvas study file exceeds the 8 MB limit.")
        target = metadata.get("url")
        if not isinstance(target, str):
            raise CanvasClientError("Canvas study file is unavailable.")
        use_authorization = True
        for _ in range(4):
            parsed = urlparse(target)
            if parsed.username is not None or parsed.password is not None:
                raise CanvasClientError("Canvas study file redirect was blocked.")
            same_origin = (parsed.scheme, parsed.netloc) == self._origin
            if not same_origin:
                self._validate_public_image_url(target)
                use_authorization = False
            client = self._client if same_origin and use_authorization else self._public_client
            try:
                with client.stream("GET", target) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if not location:
                            break
                        target = urljoin(str(response.url), location)
                        use_authorization = False
                        continue
                    if response.is_error:
                        break
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise CanvasClientError("Canvas study file exceeds the 8 MB limit.")
                    chunks: list[bytes] = []
                    actual = 0
                    for chunk in response.iter_bytes(chunk_size=64_000):
                        actual += len(chunk)
                        if actual > max_bytes:
                            raise CanvasClientError("Canvas study file exceeds the 8 MB limit.")
                        chunks.append(chunk)
                    safe_metadata = {key: metadata.get(key) for key in ("id", "display_name", "filename", "content-type", "size")}
                    return safe_metadata, b"".join(chunks)
            except httpx.HTTPError:
                raise CanvasClientError("Canvas study file is unavailable.") from None
        raise CanvasClientError("Canvas study file is unavailable.")

    def get_calendar_events(self) -> list[dict]:
        return self._get_paginated("/api/v1/calendar_events", {"type": "event", "per_page": 100})

    def get_announcements(self, course_id: int) -> list[dict]:
        # Canvas otherwise defaults to a short recent window. Request the
        # full published history explicitly, rather than silently omitting
        # older lecturer announcements.
        return self._get_paginated("/api/v1/announcements", {
            "context_codes[]": f"course_{course_id}",
            "start_date": "1970-01-01T00:00:00Z",
            "end_date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "active_only": "true", "latest_only": "false", "per_page": 100,
        })

    def get_modules(self, course_id: int) -> list[dict]:
        return self._get_paginated(f"/api/v1/courses/{course_id}/modules", {"per_page": 100})

    def get_module_items(self, course_id: int, module_id: int) -> list[dict]:
        return self._get_paginated(f"/api/v1/courses/{course_id}/modules/{module_id}/items", {"per_page": 100})

    def get_pages(self, course_id: int) -> list[dict]:
        return self._get_paginated(f"/api/v1/courses/{course_id}/pages", {"per_page": 100})

    def get_page(self, course_id: int, page_url: str) -> dict:
        data = self._request(f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe='')}").json()
        if not isinstance(data, dict): raise CanvasClientError("Could not load this Canvas page.")
        return data

    def get_file(self, file_id: int) -> dict:
        data = self._request(f"/api/v1/files/{file_id}").json()
        if not isinstance(data, dict): raise CanvasClientError("Could not load this Canvas file.")
        return data

    def get_quiz(self, course_id: int, quiz_id: int) -> dict:
        data = self._request(f"/api/v1/courses/{course_id}/quizzes/{quiz_id}").json()
        if not isinstance(data, dict): raise CanvasClientError("Could not load this Canvas quiz.")
        return data

    def get_discussion_topic(self, course_id: int, topic_id: int) -> dict:
        data = self._request(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}").json()
        if not isinstance(data, dict): raise CanvasClientError("Could not load this Canvas discussion.")
        return data

    def get_canvas_image(self, url: str, max_bytes: int = 8_000_000) -> bytes:
        response = self._request(url)
        redirects = 0
        while response.is_redirect and redirects < 3:
            location = response.headers.get("Location")
            if not location: raise CanvasClientError("Canvas image is unavailable.")
            target = urljoin(str(response.url), location)
            self._validate_public_image_url(target)
            try:
                response = self._public_client.get(target)
            except httpx.HTTPError as exc:
                raise CanvasClientError("Canvas image is unavailable.") from exc
            redirects += 1
        if response.is_redirect or response.is_error:
            raise CanvasClientError("Canvas image is unavailable.")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise CanvasClientError("Canvas image is unavailable.")
        if len(response.content) > max_bytes:
            raise CanvasClientError("Canvas image is too large to display safely.")
        return response.content

    def get_canvas_file_content(self, file_id: int, max_bytes: int = 25_000_000) -> tuple[dict, bytes]:
        metadata = self.get_file(file_id)
        declared_size = metadata.get("size")
        if isinstance(declared_size, int) and declared_size > max_bytes:
            raise CanvasClientError("Canvas file is too large to preview safely.")
        url = metadata.get("url")
        if not isinstance(url, str) or not url:
            raise CanvasClientError("Canvas file content is unavailable.")
        parsed = urlparse(url)
        if (parsed.scheme, parsed.netloc) == self._origin:
            response = self._request(url)
        else:
            self._validate_public_image_url(url)
            try: response = self._public_client.get(url)
            except httpx.HTTPError as exc: raise CanvasClientError("Canvas file content is unavailable.") from exc
        redirects = 0
        while response.is_redirect and redirects < 3:
            location = response.headers.get("Location")
            if not location: raise CanvasClientError("Canvas file content is unavailable.")
            target = urljoin(str(response.url), location); self._validate_public_image_url(target)
            try: response = self._public_client.get(target)
            except httpx.HTTPError as exc: raise CanvasClientError("Canvas file content is unavailable.") from exc
            redirects += 1
        if response.is_redirect or response.is_error: raise CanvasClientError("Canvas file content is unavailable.")
        if len(response.content) > max_bytes: raise CanvasClientError("Canvas file is too large to preview safely.")
        return metadata, response.content

    @staticmethod
    def _validate_public_image_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CanvasClientError("Canvas image redirect was blocked.")
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith(".local"):
            raise CanvasClientError("Canvas image redirect was blocked.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise CanvasClientError("Canvas image redirect was blocked.")
