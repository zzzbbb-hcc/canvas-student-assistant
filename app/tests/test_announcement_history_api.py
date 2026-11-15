"""Announcement refresh explicitly reads published history, not a short window."""

from datetime import datetime, timezone

import httpx

from app.services.canvas_client import CanvasClient


def test_announcements_request_full_published_history_with_read_only_pagination():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/announcements"
        assert request.url.params.get("access_token") is None
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2, "title": "Older notice"}], request=request)
        params = request.url.params
        assert params["context_codes[]"] == "course_7"
        assert params["start_date"] == "1970-01-01T00:00:00Z"
        assert params["active_only"] == "true"
        assert params["latest_only"] == "false"
        assert params["per_page"] == "100"
        end = datetime.fromisoformat(params["end_date"].replace("Z", "+00:00"))
        assert 0 <= (datetime.now(timezone.utc) - end).total_seconds() < 5
        return httpx.Response(
            200, json=[{"id": 1, "title": "Recent notice"}],
            headers={"Link": '<https://canvas.sydney.edu.au/api/v1/announcements?page=2>; rel="next"'},
            request=request,
        )

    with CanvasClient("fixture-value", transport=httpx.MockTransport(handler)) as client:
        result = client.get_announcements(7)

    assert [item["id"] for item in result] == [1, 2]
    assert len(calls) == 2


def test_announcements_empty_response_does_not_request_unrelated_content():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=[], request=request)

    with CanvasClient("fixture-value", transport=httpx.MockTransport(handler)) as client:
        assert client.get_announcements(7) == []
    assert calls == [("GET", "/api/v1/announcements")]
