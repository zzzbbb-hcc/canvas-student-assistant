from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.services.ed_lessons_service import EdLessonsService, UNSUPPORTED_API


class EdLessonsPage(QWidget):
    """Read-only lesson browser with an explicit unsupported API state."""

    def __init__(self, service: EdLessonsService | None = None) -> None:
        super().__init__()
        self._service = service or EdLessonsService()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        heading = QLabel("Ed Lessons")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("Course → module → lesson → content, always read-only")
        subtitle.setObjectName("pageSubtitle")
        layout.addWidget(heading)
        layout.addWidget(subtitle)

        notice = QFrame()
        notice.setObjectName("noticeCard")
        notice_box = QVBoxLayout(notice)
        self.status_label = QLabel(UNSUPPORTED_API)
        self.status_label.setObjectName("sectionTitle")
        detail = QLabel(self._service.reason)
        detail.setWordWrap(True)
        detail.setObjectName("statusText")
        notice_box.addWidget(self.status_label)
        notice_box.addWidget(detail)
        layout.addWidget(notice)

        controls = QHBoxLayout()
        self.course_selector = QComboBox()
        self.course_selector.addItem("No Ed Lesson courses available")
        self.course_selector.setEnabled(False)
        controls.addWidget(QLabel("Course"))
        controls.addWidget(self.course_selector, 1)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.module_list = QListWidget()
        self.module_list.addItem("Modules unavailable")
        self.module_list.setEnabled(False)
        self.lesson_list = QListWidget()
        self.lesson_list.addItem("Lessons unavailable")
        self.lesson_list.setEnabled(False)
        self.content_viewer = QTextBrowser()
        self.content_viewer.setHtml(
            "<h2>Lesson content</h2><p>No official lesson-content endpoint has "
            "been confirmed. The app will not scrape sessions, bypass MFA or "
            "guess private routes.</p><p>Answers and progress controls are disabled.</p>"
        )
        splitter.addWidget(self.module_list)
        splitter.addWidget(self.lesson_list)
        splitter.addWidget(self.content_viewer)
        splitter.setSizes([240, 280, 650])
        layout.addWidget(splitter, 1)

    def refresh(self) -> None:
        self.status_label.setText(self._service.status)
