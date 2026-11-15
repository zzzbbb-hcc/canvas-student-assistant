from datetime import datetime, timedelta, timezone

from app.models.assignment import Assignment
from app.storage.session_cache import session_cache
from app.ui.ai_chat import AIChatPage


def test_ai_chat_answers_from_cached_canvas_due_at(qtbot):
    session_cache.set(
        "assignments",
        [
            Assignment(
                assignment_id=500,
                course_id=10,
                course_name="Test Course",
                course_code="TEST1000",
                title="Safe assignment",
                due_at=datetime.now(timezone.utc) + timedelta(days=2),
                submission_status="unsubmitted",
            )
        ],
    )
    page = AIChatPage()
    qtbot.addWidget(page)
    page.question_input.setText("Which assignment should I work on first based on deadline?")
    page.ask_button.click()
    qtbot.waitUntil(lambda: getattr(page, "_thread", None) is None, timeout=10_000)
    assert "Safe assignment" in page.chat_view.toPlainText()
    assert "Canvas due_at" in page.chat_view.toPlainText()


def test_ai_chat_rejects_empty_question(qtbot):
    page = AIChatPage()
    qtbot.addWidget(page)
    page.ask_button.click()
    assert page.status_label.text() == "请输入学习问题。"
