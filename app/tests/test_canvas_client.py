import httpx
import pytest

from app.services.canvas_client import CanvasClient, CanvasClientError


def client(handler):
    return CanvasClient("fake-value", transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_rejects_non_https_base_url():
    with pytest.raises(ValueError, match="HTTPS"):
        CanvasClient("fake-value", base_url="http://canvas.example.edu")


def test_no_token():
    with pytest.raises(CanvasClientError, match="No Canvas token"):
        CanvasClient("")


@pytest.mark.parametrize("status,message", [(401, "invalid or expired"), (403, "access denied"), (500, "Could not connect")])
def test_safe_status_errors(status, message):
    with client(lambda request: httpx.Response(status, request=request)) as canvas:
        with pytest.raises(CanvasClientError, match=message):
            canvas.get_current_user()


def test_timeout_is_safe():
    def handler(request):
        raise httpx.ReadTimeout("private transport detail", request=request)
    with client(handler) as canvas:
        with pytest.raises(CanvasClientError, match="timed out") as error:
            canvas.get_current_user()
    assert "private transport detail" not in str(error.value)


def test_current_user_authorization_header_not_in_url():
    def handler(request):
        assert request.url == "https://canvas.sydney.edu.au/api/v1/users/self"
        assert request.headers["Authorization"] == "Bearer fake-value"
        return httpx.Response(200, json={"id": 1, "name": "Student"}, request=request)
    with client(handler) as canvas:
        assert canvas.get_current_user()["name"] == "Student"


def test_courses_pagination_and_filtering():
    def handler(request):
        assert request.url.params.get("access_token") is None
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2, "name": "Course Two", "course_code": "C2"}], request=request)
        headers = {"Link": '<https://canvas.sydney.edu.au/api/v1/courses?page=2>; rel="next"'}
        data = [
            {"id": 1, "name": "Course One", "course_code": "C1"},
            {"id": 99, "name": "Old", "access_restricted_by_date": True},
        ]
        return httpx.Response(200, json=data, headers=headers, request=request)
    with client(handler) as canvas:
        courses = canvas.get_courses()
    assert [(course.id, course.course_code) for course in courses] == [(1, "C1"), (2, "C2")]


def test_pagination_cannot_send_token_to_another_origin():
    def handler(request):
        headers = {"Link": '<https://attacker.invalid/steal>; rel="next"'}
        return httpx.Response(200, json=[], headers=headers, request=request)
    with client(handler) as canvas:
        with pytest.raises(CanvasClientError, match="Could not connect"):
            canvas.get_courses()


def test_assignment_pagination_and_submission_mapping():
    def handler(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 12, "name": "No Date", "due_at": None}], request=request)
        headers = {"Link": '<https://canvas.sydney.edu.au/api/v1/courses/7/assignments?page=2>; rel="next"'}
        data = [{"id": 11, "name": "Essay", "due_at": "2026-09-20T13:59:00Z", "submission": {"workflow_state": "submitted", "submitted_at": "2026-09-19T10:00:00Z", "attempt": 1, "missing": False}}]
        return httpx.Response(200, json=data, headers=headers, request=request)
    from app.models.course import Course
    with client(handler) as canvas:
        result = canvas.get_assignments(7, Course(id=7, name="Testing", course_code="TEST1000"))
    assert [item.assignment_id for item in result] == [11, 12]
    assert result[0].is_submitted
    assert result[0].submission_attempt == 1
    assert result[1].due_at is None


def test_429_retries_are_limited():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, request=request)
    with client(handler) as canvas:
        with pytest.raises(CanvasClientError, match="Could not connect"):
            canvas.get_current_user()
    assert calls == 3


def test_5xx_recovers_after_finite_retry():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls < 3 else 200, json={"id": 1}, request=request)
    with client(handler) as canvas:
        assert canvas.get_current_user()["id"] == 1
    assert calls == 3


def test_timeout_retries_are_limited():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)
    with client(handler) as canvas:
        with pytest.raises(CanvasClientError, match="timed out"):
            canvas.get_current_user()
    assert calls == 3


def test_pages_uses_official_course_endpoint():
    def handler(request):
        assert request.url.path == "/api/v1/courses/7/pages"
        return httpx.Response(200, json=[{"url": "week-1", "title": "Week 1"}], request=request)
    with client(handler) as canvas:
        assert canvas.get_pages(7)[0]["title"] == "Week 1"


def test_get_page_uses_read_only_api_and_encodes_url():
    def handler(request):
        assert request.method == "GET"
        assert request.url.raw_path == b"/api/v1/courses/7/pages/week%20one"
        return httpx.Response(200, json={"title": "Week One", "body": "<p>Content</p>"}, request=request)
    with client(handler) as canvas:
        assert canvas.get_page(7, "week one")["title"] == "Week One"


def test_canvas_image_redirect_does_not_forward_authorization():
    png = b"\x89PNG\r\n\x1a\n" + b"image-data"
    def handler(request):
        if request.url.host == "canvas.sydney.edu.au":
            assert request.headers.get("Authorization") == "Bearer fake-value"
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/image.png"}, request=request)
        assert request.url.host == "cdn.example.com"
        assert "Authorization" not in request.headers
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=png, request=request)
    with client(handler) as canvas:
        assert canvas.get_canvas_image("https://canvas.sydney.edu.au/files/1/download") == png


def test_canvas_image_blocks_unsafe_redirect():
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}, request=request)
    with client(handler) as canvas:
        with pytest.raises(CanvasClientError, match="blocked"):
            canvas.get_canvas_image("https://canvas.sydney.edu.au/files/1/download")


def test_canvas_file_download_uses_public_client_without_token_after_redirect():
    content = b"%PDF-1.4\ncourseware"
    def handler(request):
        if request.url.path == "/api/v1/files/42":
            return httpx.Response(200, json={"id": 42, "display_name": "notes.pdf", "content-type": "application/pdf", "size": len(content), "url": "https://canvas.sydney.edu.au/files/42/download"}, request=request)
        if request.url.host == "canvas.sydney.edu.au":
            assert request.headers.get("Authorization") == "Bearer fake-value"
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/notes.pdf"}, request=request)
        assert "Authorization" not in request.headers
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=content, request=request)
    with client(handler) as canvas:
        metadata, downloaded = canvas.get_canvas_file_content(42)
    assert metadata["display_name"] == "notes.pdf" and downloaded == content
