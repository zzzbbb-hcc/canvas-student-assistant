"""Dashboard summary cards act as accessible links to unified deadlines."""

from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.storage.cache import Cache
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.dashboard import DashboardPage


@pytest.fixture
def dashboard(qtbot):
    database = SQLiteDatabase(":memory:")
    session_cache.clear()
    page = DashboardPage(credential_store=object(), cache=Cache(database))
    qtbot.addWidget(page)
    page.resize(900, 680)
    page.show()
    qtbot.wait(10)
    yield page
    database.close()
    session_cache.clear()


@pytest.mark.parametrize("name,period", [
    ("Due Today", "today"),
    ("Due Tomorrow", "tomorrow"),
    ("Due This Week", "week"),
    ("Overdue", "overdue"),
])
def test_stat_cards_support_mouse_and_keyboard(dashboard, qtbot, name, period):
    button = dashboard.stat_buttons[name]
    assert isinstance(button, QPushButton)
    assert button.objectName() == "statCard"
    assert button.accessibleName() == name
    assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert "assignments" in button.toolTip()
    assert "quizzes" in button.toolTip()
    label = button.findChild(QLabel, "statLabel")
    number = button.findChild(QLabel, "statNumber")
    assert number is dashboard.stats[name]
    assert label.text() == name
    for child in (number, label):
        assert child.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    with qtbot.waitSignal(dashboard.deadline_category_requested) as emitted:
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton,
                        pos=label.mapTo(button, label.rect().center()))
    assert emitted.args == [period]

    button.setFocus()
    with qtbot.waitSignal(dashboard.deadline_category_requested) as emitted:
        qtbot.keyClick(button, Qt.Key.Key_Space)
    assert emitted.args == [period]


@pytest.mark.parametrize("width", [600, 900])
def test_stat_labels_and_numbers_remain_fully_visible(dashboard, qtbot, width):
    dashboard.resize(width, 680)
    for label in dashboard.stats.values():
        label.setText("123")
    qtbot.wait(20)
    for button in dashboard.stat_buttons.values():
        assert dashboard.rect().contains(button.geometry())
        for child in button.findChildren(QLabel):
            assert button.rect().contains(child.geometry())
            if child.hasHeightForWidth():
                assert child.height() >= child.heightForWidth(child.width())
            else:
                assert child.height() >= child.minimumSizeHint().height()


def test_counts_include_assignment_and_quiz_with_same_period_rules(dashboard, monkeypatch):
    # Friday: today/tomorrow overlap this week, while Monday does not.
    now = datetime(2026, 10, 16, 12, 0).astimezone()
    service = DeadlineService(now)
    monkeypatch.setattr("app.ui.dashboard.DeadlineService", lambda: service)

    def task(identifier, due, *, quiz=False, submitted=False):
        return Assignment(
            assignment_id=identifier, course_id=1, course_name="Course",
            title=f"Item {identifier}", due_at=due,
            quiz_id=identifier if quiz else None,
            quiz_engine="classic" if quiz else None,
            submission_status="submitted" if submitted else "unsubmitted",
        )

    assignments = [
        task(1, now.replace(hour=18)),
        task(2, now.replace(hour=20), quiz=True),
        task(3, now + timedelta(days=1)),
        task(4, now + timedelta(days=1, hours=1), quiz=True),
        task(5, now + timedelta(days=2), quiz=True),
        task(6, now + timedelta(days=3)),
        task(7, now - timedelta(days=1)),
        task(8, now - timedelta(hours=2), quiz=True),
        task(9, None, quiz=True),
        task(10, now.replace(hour=18), submitted=True),
        task(11, now - timedelta(days=2), submitted=True),
    ]
    dashboard._show(assignments)
    assert {name: label.text() for name, label in dashboard.stats.items()} == {
        "Due Today": "2", "Due Tomorrow": "2", "Due This Week": "5", "Overdue": "2",
    }
    deadlines = service.deadlines(assignments)
    for name, period in dashboard._stat_periods.items():
        matches = [deadline for deadline in deadlines if service.matches_period(deadline, period)]
        assert len(matches) == int(dashboard.stats[name].text())
        assert any(deadline.assignment.is_quiz for deadline in matches)
        assert any(not deadline.assignment.is_quiz for deadline in matches)
        assert all(not deadline.assignment.is_submitted for deadline in matches)


def test_stat_labels_fit_under_application_styles(qtbot, monkeypatch):
    from app.ui.main_window import MainWindow

    class Store:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

    session_cache.clear()
    monkeypatch.setattr("app.ui.main_window.CredentialStore", Store)
    monkeypatch.setattr("app.ui.main_window.SQLiteDatabase", lambda: SQLiteDatabase(":memory:"))
    monkeypatch.setattr(MainWindow, "_refresh_current_page", lambda *_args: None)
    window = MainWindow()
    window._reminder_timer.stop()
    qtbot.addWidget(window)
    window.resize(1100, 720)
    window.show()
    qtbot.wait(20)
    try:
        page = next(window.pages.widget(index) for index in range(window.pages.count())
                    if isinstance(window.pages.widget(index), DashboardPage))
        for button in page.stat_buttons.values():
            for child in button.findChildren(QLabel):
                assert button.rect().contains(child.geometry())
                assert child.height() >= child.minimumSizeHint().height()
    finally:
        window._database.close()
        session_cache.clear()
