import pytest

from app.services.ed_client import EdClient, EdUnsupportedAPI, UNSUPPORTED_API
from app.services.ed_lessons_service import EdLessonsService
from app.ui.ed_discussion import EdDiscussionPage
from app.ui.ed_lessons import EdLessonsPage


def test_ed_client_refuses_unconfirmed_endpoints():
    client = EdClient("dummy-token")
    with pytest.raises(EdUnsupportedAPI) as error:
        client.get_courses()
    assert error.value.code == UNSUPPORTED_API
    assert "dummy-token" not in str(error.value)


def test_ed_lessons_explicitly_returns_unsupported():
    service = EdLessonsService()
    assert service.get_lesson_courses() == UNSUPPORTED_API
    assert service.get_lesson_content("lesson") == UNSUPPORTED_API


def test_ed_pages_show_safe_degraded_state(qtbot):
    discussion = EdDiscussionPage()
    lessons = EdLessonsPage()
    qtbot.addWidget(discussion)
    qtbot.addWidget(lessons)
    assert "not included" in discussion.status_label.text()
    assert not discussion.search_button.isEnabled()
    assert lessons.status_label.text() == UNSUPPORTED_API


