from app.ui.main_window import MainWindow
from PySide6.QtGui import QCloseEvent

def test_main_window_contains_all_navigation_items(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    assert [window.navigation.item(i).text() for i in range(window.navigation.count())] == ["Dashboard", "Courses", "Assignments", "Quizzes", "Deadlines", "公告", "AI Assistant", "Settings"]


def test_close_waits_for_announcement_preview_cleanup_even_after_native_run_returns(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    page = window.pages.widget(5)

    class FinishingThread:
        def isRunning(self):
            return False

    page._preview_thread = FinishingThread()
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    page._preview_thread = None
    qtbot.wait(220)
    courses = window.pages.widget(1)
    assert courses.course_list.objectName() == "cardList"


def test_navigation_refreshes_data_pages(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    courses = window.pages.widget(1)
    calls = []
    monkeypatch.setattr(courses, "refresh_courses", lambda: calls.append("courses"))
    window.navigation.setCurrentRow(1)
    assert calls == ["courses"]
