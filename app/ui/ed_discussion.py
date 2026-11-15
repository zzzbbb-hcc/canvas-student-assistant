from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.services.ed_client import EdClient


class EdDiscussionPage(QWidget):
    """Read-only Ed workspace that remains closed while the API is unsupported."""

    def __init__(self) -> None:
        super().__init__()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        heading = QLabel("Ed Discussion")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("Read-only course discussions, ready for an official Ed API connection")
        subtitle.setObjectName("pageSubtitle")
        layout.addWidget(heading)
        layout.addWidget(subtitle)

        notice = QFrame()
        notice.setObjectName("noticeCard")
        notice_layout = QVBoxLayout(notice)
        self.status_label = QLabel()
        self.status_label.setObjectName("sectionTitle")
        self.status_detail = QLabel(EdClient.unsupported_reason)
        self.status_detail.setWordWrap(True)
        self.status_detail.setObjectName("statusText")
        notice_layout.addWidget(self.status_label)
        notice_layout.addWidget(self.status_detail)
        layout.addWidget(notice)

        toolbar = QHBoxLayout()
        self.course_selector = QComboBox()
        self.course_selector.addItem("No Ed courses available")
        self.course_selector.setEnabled(False)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search threads")
        self.search_input.setEnabled(False)
        self.recent_button = QPushButton("Recent")
        self.staff_button = QPushButton("Staff Answers")
        self.search_button = QPushButton("Search")
        for button in (self.recent_button, self.staff_button, self.search_button):
            button.setEnabled(False)
        self.open_ed_button = QPushButton("Open official Ed")
        self.open_ed_button.setObjectName("secondaryButton")
        self.open_ed_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://edstem.org/"))
        )
        toolbar.addWidget(self.course_selector, 2)
        toolbar.addWidget(self.search_input, 3)
        toolbar.addWidget(self.search_button)
        toolbar.addWidget(self.recent_button)
        toolbar.addWidget(self.staff_button)
        toolbar.addWidget(self.open_ed_button)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.thread_list = QListWidget()
        self.thread_list.setObjectName("threadList")
        self.thread_list.addItem("Ed API unavailable — no thread data requested")
        self.thread_list.setEnabled(False)
        self.thread_viewer = QTextBrowser()
        self.thread_viewer.setObjectName("threadViewer")
        self.thread_viewer.setHtml(
            "<h2>Thread viewer</h2>"
            "<p>When an official API connection becomes available, thread bodies, "
            "replies, comments, resolved state, pinned state and staff answers will "
            "appear here.</p><p><b>Read-only:</b> posting and reactions are disabled.</p>"
        )
        splitter.addWidget(self.thread_list)
        splitter.addWidget(self.thread_viewer)
        splitter.setSizes([340, 760])
        layout.addWidget(splitter, 1)
        self.refresh()

    def refresh(self) -> None:
        self.status_label.setText("Ed integration is not included in this version")
