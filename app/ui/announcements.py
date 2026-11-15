"""Compact announcements inbox with private offline bilingual reading."""

from collections import OrderedDict

from PySide6.QtCore import QSignalBlocker, QThread, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from app.models.announcement import announcement_text, sort_announcements
from app.security.html_sanitizer import safe_canvas_html
from app.services.announcement_loader import cached_announcements, load_announcements
from app.services.announcement_state import AnnouncementState, AnnouncementStateStore
from app.services.announcement_preview import announcement_preview_key, translate_announcement_preview
from app.services.credential_store import CredentialStore
from app.services.offline_translation import offline_translation
from app.storage.cache import Cache
from app.storage.session_cache import session_cache
from app.ui.canvas_worker import CanvasWorker
from app.ui.cards import CardList
from app.ui.theme import (
    PALETTE, application_stylesheet, reader_stylesheet, theme_font,
    warning_stylesheet,
)


def _posted_time(value):
    if value is None:
        return "发布时间未提供"
    local = value.astimezone()
    offset = local.strftime("%z")
    return f"{local:%Y年%m月%d日 %H:%M} · 本地时间 (UTC{offset[:3]}:{offset[3:]})"


class AnnouncementsPage(QWidget):
    def __init__(self, credential_store=None, *, cache=None, translator=None):
        super().__init__()
        self.setFont(theme_font())
        self._store = credential_store or CredentialStore()
        self._cache = cache or Cache()
        self._state_store = AnnouncementStateStore(self._cache.database)
        self._state_error = ""
        self._announcement_states = {}
        self._load_announcement_states()
        self._thread = None
        self._worker = None
        self._translator = translator if translator is not None else offline_translation
        self._preview_thread = None
        self._preview_worker = None
        self._preview_key = None
        self._preview_translations = OrderedDict()
        self._preview_failures = set()
        self._preview_close_pending = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._translate_visible_previews)
        self._announcements = []
        self._visible_announcements = []
        self._coverage_warnings = []
        self._announcement_cards = {}
        self._empty_item = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 14); layout.setSpacing(8)
        title = QLabel("公告"); title.setObjectName("pageTitle")
        actions = QHBoxLayout(); actions.addWidget(title); actions.addStretch()
        self.refresh_button = QPushButton("刷新公告")
        self.refresh_button.clicked.connect(self.refresh); actions.addWidget(self.refresh_button)
        layout.addLayout(actions)
        filters = QHBoxLayout(); filters.addWidget(QLabel("课程"))
        self.course_filter = QComboBox(); self.course_filter.setObjectName("announcementCourseFilter")
        self.course_filter.setMinimumContentsLength(12)
        self.course_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.course_filter.addItem("全部课程", None)
        self.course_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.course_filter, 1)
        self.read_filter = QComboBox(); self.read_filter.setObjectName("announcementReadFilter")
        for label, value in (("全部状态", "all"), ("仅未读", "unread"), ("仅已读", "read")):
            self.read_filter.addItem(label, value)
        self.read_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.read_filter)
        self.star_filter = QComboBox(); self.star_filter.setObjectName("announcementStarFilter")
        self.star_filter.addItem("全部公告", "all"); self.star_filter.addItem("★ 重点公告", "starred")
        self.star_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.star_filter)
        self.coverage_button = QPushButton("读取提示")
        self.coverage_button.setObjectName("secondaryButton"); self.coverage_button.hide()
        self.coverage_button.clicked.connect(self._show_coverage); filters.addWidget(self.coverage_button)
        layout.addLayout(filters)
        self.summary_label = QLabel("公告按发布时间从新到旧排列。重点为本机摘录，非完整原文。")
        self.summary_label.setObjectName("statusText"); self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.preview_status = QLabel("标题与重点将在本机翻译为中文，译文仅供参考；等待时保留原文。")
        self.preview_status.setObjectName("announcementPreviewStatus")
        self.preview_status.setWordWrap(True)
        self.preview_status.setStyleSheet(f"color: {PALETTE.text_muted}; font-size: 12px;")
        layout.addWidget(self.preview_status)
        self.coverage_label = QLabel(); self.coverage_label.setObjectName("announcementCoverageWarning")
        self.coverage_label.setWordWrap(True); self.coverage_label.setTextFormat(Qt.TextFormat.PlainText)
        self.coverage_label.setStyleSheet(warning_stylesheet())
        self.coverage_label.hide(); layout.addWidget(self.coverage_label)
        self.announcement_list = CardList(compact=True)
        self.announcement_list.itemClicked.connect(self._open_item)
        self.announcement_list.verticalScrollBar().valueChanged.connect(self._schedule_preview_translation)
        layout.addWidget(self.announcement_list, 1)
        self.status_label = QLabel()
        self.status_label.setObjectName("statusText"); self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._set_status("单击打开自动已读 · 点 ● / 已读切换 · ☆ 标重点 · 标记仅保存在本机。")

    def refresh(self, refresh=True):
        if self._thread is not None:
            return
        self._preview_failures.clear()
        known = cached_announcements(self._cache)
        if known:
            self._show(known)
        self.refresh_button.setEnabled(False)
        self._set_status("保留当前公告，正在后台检查更新…" if self._announcements else "正在读取课程公告…")
        self._thread = QThread(self)
        self._worker = CanvasWorker(lambda: load_announcements(self._store, refresh=True, cache=self._cache))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._show)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._finished)
        self._thread.start()

    @Slot(object)
    def _show(self, items):
        self._announcements = sort_announcements(items)
        self._load_announcement_states()
        selected = self.course_filter.currentData()
        courses = {item.course_id: item for item in self._announcements}
        with QSignalBlocker(self.course_filter):
            self.course_filter.clear(); self.course_filter.addItem("全部课程", None)
            for course_id, item in sorted(courses.items(), key=lambda pair: ((pair[1].course_code or pair[1].course_name).casefold(), pair[0])):
                label = f"{item.course_code} · {item.course_name}" if item.course_code else item.course_name
                self.course_filter.addItem(label, course_id)
            self.course_filter.setCurrentIndex(max(0, self.course_filter.findData(selected)))
        self._render()
        self._update_coverage()
        self._set_status("打开自动已读 · 点 ● / 已读切换 · ☆ 标重点 · 仅保存在本机，不修改 Canvas 状态。")

    @Slot()
    def _render(self):
        self._visible_announcements = [item for item in self._announcements if self._matches_filters(item)]
        self.announcement_list.clear()
        self._announcement_cards.clear()
        self._empty_item = None
        for item in self._visible_announcements:
            self._add_announcement_card(item)
        self._update_summary()
        self._schedule_preview_translation()

    @staticmethod
    def _announcement_key(item):
        return item.course_id, item.announcement_id

    def _state_for(self, item):
        return self._announcement_states.get(self._announcement_key(item), AnnouncementState())

    def _load_announcement_states(self):
        try:
            self._announcement_states = self._state_store.get_states()
            self._state_error = ""
        except Exception:
            # Keep known flags if a transient lock happens during a refresh;
            # unseen notices default to unread instead of guessing read status.
            self._state_error = "本机已读和重点标记暂不可读取，保留当前标记；未记录的公告按未读显示。刷新可重试。"

    def _matches_filters(self, item):
        selected = self.course_filter.currentData()
        if selected is not None and item.course_id != selected:
            return False
        state = self._state_for(item)
        selected_read = self.read_filter.currentData()
        if selected_read == "unread" and state.is_read or selected_read == "read" and not state.is_read:
            return False
        return self.star_filter.currentData() != "starred" or state.is_starred

    def _add_announcement_card(self, item):
        posted = _posted_time(item.posted_at)
        subtitle = f"{item.course_code or item.course_name} · {posted}"
        listing = self.announcement_list
        preview = self._preview_translations.get(announcement_preview_key(item))
        title = preview.title if preview else item.title
        meta = f"中文重点（非完整原文）：{preview.summary}" if preview else f"重点摘录（原文，待翻译；非完整原文）：{item.summary}"
        card_item = listing.add_card(title, subtitle, meta, accent=PALETTE.accent)
        card_item.setData(Qt.ItemDataRole.UserRole, item)
        self._announcement_cards[self._announcement_key(item)] = card_item
        card = listing.itemWidget(card_item)
        controls = QVBoxLayout(); controls.setSpacing(3)
        read_button = QPushButton(); read_button.setObjectName("announcementReadButton")
        read_button.setFixedWidth(72)
        read_button.clicked.connect(lambda checked=False, notice=item: self._set_announcement_read(notice, not self._state_for(notice).is_read))
        controls.addWidget(read_button)
        star_button = QPushButton(); star_button.setObjectName("announcementStarButton")
        star_button.setFixedWidth(72)
        star_button.clicked.connect(lambda checked=False, notice=item: self._set_announcement_starred(notice, not self._state_for(notice).is_starred))
        controls.addWidget(star_button); controls.addStretch()
        card.layout().addLayout(controls)
        self._style_announcement_card(item, card_item)

    def _style_announcement_card(self, item, card_item):
        card = self.announcement_list.itemWidget(card_item)
        if card is None:
            return
        state = self._state_for(item)
        card.setProperty("isRead", state.is_read); card.setProperty("isStarred", state.is_starred)
        background, border = ((PALETTE.surface, PALETTE.border) if state.is_read else
                              (PALETTE.surface_alt, PALETTE.accent))
        card.setStyleSheet(
            f"QFrame#contentCard {{ background: {background}; border: 1px solid {border}; border-radius: 12px; }}"
        )
        heading = card.findChild(QLabel, "cardTitle")
        heading.setStyleSheet(
            f"font-size: 15px; font-weight: {500 if state.is_read else 650}; "
            f"color: {PALETTE.text_secondary if state.is_read else PALETTE.text};"
        )
        read_button = card.findChild(QPushButton, "announcementReadButton")
        read_button.setText("已读" if state.is_read else "● 未读")
        read_button.setAccessibleName("标为未读" if state.is_read else "标为已读")
        read_button.setToolTip("标为未读（仅本机）" if state.is_read else "标为已读（仅本机）")
        read_color, read_bg = ((PALETTE.text_secondary, PALETTE.neutral_bg) if state.is_read else
                               (PALETTE.warning, PALETTE.selection))
        read_button.setStyleSheet(
            f"QPushButton {{ color: {read_color}; background: {read_bg}; border: 1px solid transparent; "
            "border-radius: 8px; padding: 3px 5px; font-size: 12px; } "
            f"QPushButton:hover {{ background: {PALETTE.primary_hover}; }} "
            f"QPushButton:focus {{ border-color: {PALETTE.focus}; }}"
        )
        star_button = card.findChild(QPushButton, "announcementStarButton")
        star_button.setText("★ 重点" if state.is_starred else "☆ 重点")
        star_button.setAccessibleName("取消重点标记" if state.is_starred else "标为重点")
        star_button.setToolTip("取消重点标记（仅本机）" if state.is_starred else "标为重点（仅本机）")
        star_color, star_bg = ((PALETTE.warning, PALETTE.warning_bg) if state.is_starred else
                               (PALETTE.text_secondary, PALETTE.surface))
        star_button.setStyleSheet(
            f"QPushButton {{ color: {star_color}; background: {star_bg}; border: 1px solid transparent; "
            "border-radius: 8px; padding: 3px 5px; font-size: 12px; } "
            f"QPushButton:hover {{ background: {PALETTE.selection}; }} "
            f"QPushButton:focus {{ border-color: {PALETTE.focus}; }}"
        )
        self.announcement_list._size_item(card_item, card)

    def _update_summary(self):
        if not self._visible_announcements:
            if self._empty_item is None:
                self._empty_item = self.announcement_list.add_section("当前没有符合筛选的公告", "可切换课程、已读状态或重点筛选；读取失败会单独提示。")
        selected_course = self.course_filter.currentData()
        course_items = [item for item in self._announcements if selected_course is None or item.course_id == selected_course]
        unread = sum(not self._state_for(item).is_read for item in course_items)
        starred = sum(self._state_for(item).is_starred for item in course_items)
        self.summary_label.setText(f"显示 {len(self._visible_announcements)} / {len(course_items)} 条 · 未读 {unread} · ★ 重点 {starred} · 最新优先 · 重点摘录非完整原文")

    def _set_status(self, message):
        self.status_label.setText(f"{message}\n{self._state_error}" if self._state_error else message)

    def _set_announcement_read(self, item, value):
        return self._change_announcement_state(item, "read", value)

    def _set_announcement_starred(self, item, value):
        return self._change_announcement_state(item, "starred", value)

    def _change_announcement_state(self, item, field, value):
        try:
            setter = self._state_store.set_read if field == "read" else self._state_store.set_starred
            state = setter(item.course_id, item.announcement_id, bool(value))
        except Exception:
            self._set_status("本机标记未能保存，原状态已保留；请稍后重试。")
            return False
        self._announcement_states[self._announcement_key(item)] = state
        # If the initial load failed, a successful isolated write does not
        # mean all other persisted rows have become available again.
        self._update_announcement_card(item)
        self._set_status("标记已保存在本机，不修改 Canvas 状态。")
        return True

    def _update_announcement_card(self, item):
        """Change only one row, retaining other widgets, selection and scroll."""
        key = self._announcement_key(item)
        card_item = self._announcement_cards.get(key)
        if card_item is None:
            return
        listing = self.announcement_list
        scroll = listing.verticalScrollBar().value()
        if self._matches_filters(item):
            self._style_announcement_card(item, card_item)
        else:
            card = listing.itemWidget(card_item)
            listing.removeItemWidget(card_item)
            listing.takeItem(listing.row(card_item))
            if card is not None:
                card.deleteLater()
            del self._announcement_cards[key]
            self._visible_announcements = [notice for notice in self._visible_announcements if self._announcement_key(notice) != key]
        self._update_summary()
        listing.doItemsLayout()
        listing.verticalScrollBar().setValue(scroll)
        self._schedule_preview_translation()

    @Slot()
    def _schedule_preview_translation(self, *_args):
        if self.isVisible() and not self._preview_close_pending:
            self._preview_timer.start()

    @Slot()
    def _translate_visible_previews(self):
        """Translate one visible card at a time, never the whole history."""
        if (self._preview_thread is not None or not self.isVisible() or self._preview_close_pending
                or QApplication.activeModalWidget() is not None):
            return
        available = getattr(self._translator, "is_available", None)
        if callable(available) and not available():
            self.preview_status.setText("本机中文翻译模型暂不可用，先显示原文；安装完整版本后刷新重试。")
            return
        listing = self.announcement_list
        viewport = listing.viewport().rect()
        for row in range(listing.count()):
            card_item = listing.item(row)
            rect = listing.visualItemRect(card_item)
            if rect.top() > viewport.bottom():
                break
            if not rect.intersects(viewport):
                continue
            item = card_item.data(Qt.ItemDataRole.UserRole)
            if item is None:
                continue
            key = announcement_preview_key(item)
            if key in self._preview_translations or key in self._preview_failures:
                continue
            self._preview_key = key
            self.preview_status.setText("正在本机翻译当前可见公告的标题与重点… 不发送给第三方。")
            self._preview_thread = QThread(self)
            self._preview_worker = CanvasWorker(lambda notice=item, identity=key: (
                identity, translate_announcement_preview(notice, self._translator)))
            self._preview_worker.moveToThread(self._preview_thread)
            self._preview_thread.started.connect(self._preview_worker.run)
            self._preview_worker.succeeded.connect(self._preview_translated)
            self._preview_worker.failed.connect(self._preview_failed)
            self._preview_worker.finished.connect(self._preview_thread.quit)
            self._preview_worker.finished.connect(self._preview_worker.deleteLater)
            self._preview_thread.finished.connect(self._preview_finished)
            self._preview_thread.start()
            return
        self.preview_status.setText("部分中文翻译暂不可用，已保留原文；点击“刷新公告”重试。" if self._preview_failures else
                                    "中文标题与重点 · 本机离线翻译，仅供参考；点开可查看完整原文和中文。")

    @Slot(object)
    def _preview_translated(self, result):
        key, preview = result
        self._preview_translations[key] = preview
        self._preview_translations.move_to_end(key)
        while len(self._preview_translations) > 512:
            self._preview_translations.popitem(last=False)
        listing = self.announcement_list
        scroll = listing.verticalScrollBar().value()
        for item in self._visible_announcements:
            if announcement_preview_key(item) != key:
                continue
            card_item = self._announcement_cards.get(self._announcement_key(item))
            card = listing.itemWidget(card_item) if card_item is not None else None
            if card is not None:
                card.findChild(QLabel, "cardTitle").setText(preview.title)
                card.findChild(QLabel, "cardMeta").setText(f"中文重点（非完整原文）：{preview.summary}")
                card_item.setToolTip(f"{preview.title}\n{preview.summary}\n原文标题：{item.title}")
                listing._size_item(card_item, card)
        listing.doItemsLayout()
        listing.verticalScrollBar().setValue(scroll)

    @Slot(str)
    def _preview_failed(self, _message):
        self._preview_failures.add(self._preview_key)
        self.preview_status.setText("中文翻译暂不可用，已保留原文；点击“刷新公告”重试。")

    @Slot()
    def _preview_finished(self):
        thread = self._preview_thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
        self._preview_thread = None
        self._preview_worker = None
        self._preview_key = None
        if self._preview_close_pending:
            self._preview_close_pending = False
            self.close()
        else:
            self._schedule_preview_translation()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_preview_translation()

    def hideEvent(self, event):
        self._preview_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_preview_translation()

    def closeEvent(self, event):
        self._preview_timer.stop()
        if self._preview_thread is not None:
            self._preview_close_pending = True
            event.ignore()
            return
        super().closeEvent(event)

    def _update_coverage(self):
        self._coverage_warnings = list(session_cache.get("announcement_coverage_warnings", []) or [])
        count = len(self._coverage_warnings)
        self.coverage_label.setText(f"{count} 个课程的公告暂不可完整读取，已保留可读内容；点“读取提示”查看。" if count else "")
        self.coverage_label.setVisible(bool(count)); self.coverage_button.setVisible(bool(count))
        self.coverage_button.setText(f"读取提示 ({count})")

    @Slot(str)
    def _failed(self, message):
        self._set_status(message)
        self._update_coverage()

    @Slot()
    def _finished(self):
        # finished can reach the GUI before native thread-local destructors
        # have returned. Synchronize that tail before releasing Qt wrappers
        # or allowing a new refresh to reuse their native allocations.
        thread = self._thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
        self._thread = None; self._worker = None
        self.refresh_button.setEnabled(True)

    def _show_coverage(self):
        message = QMessageBox(self)
        message.setWindowTitle("公告读取提示"); message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText("部分课程公告暂不可读取，当前公告列表可能不完整。")
        message.setDetailedText("\n\n".join(self._coverage_warnings))
        message.setStandardButtons(QMessageBox.StandardButton.Close)
        message.button(QMessageBox.StandardButton.Close).setText("关闭")
        message.exec()

    def _open_item(self, item):
        announcement = item.data(Qt.ItemDataRole.UserRole)
        if announcement is not None:
            # Capture identity before removing a newly read row from an unread
            # filter. Buttons consume their own clicks and do not open details.
            dialog = AnnouncementDetailDialog(announcement, self)
            if not self._state_for(announcement).is_read:
                self._set_announcement_read(announcement, True)
            dialog.exec()
            self._schedule_preview_translation()


class AnnouncementDetailDialog(QDialog):
    def __init__(self, item, parent=None, *, translator=None):
        super().__init__(parent)
        self.setFont(theme_font())
        self.setStyleSheet(application_stylesheet())
        self.announcement = item
        self._translator = translator if translator is not None else offline_translation
        self._thread = None; self._worker = None; self._pending_result = None
        self._source = announcement_text(item.message)
        previews = getattr(parent, "_preview_translations", {})
        preview = previews.get(announcement_preview_key(item))
        self.setWindowTitle(preview.title if preview else item.title); self.setMinimumSize(850, 570)
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        self.resize(min(1280, int(area.width() * .92)) if area else 1100,
                    min(880, int(area.height() * .9)) if area else 760)
        layout = QVBoxLayout(self); layout.setContentsMargins(16, 14, 16, 14); layout.setSpacing(6)
        title = QLabel(preview.title if preview else item.title); title.setObjectName("announcementTitle")
        title.setTextFormat(Qt.TextFormat.PlainText); title.setWordWrap(True)
        title.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {PALETTE.text};")
        layout.addWidget(title)
        if preview and preview.title != item.title:
            original_title = QLabel(f"原文标题：{item.title}")
            original_title.setObjectName("announcementOriginalTitle")
            original_title.setTextFormat(Qt.TextFormat.PlainText); original_title.setWordWrap(True)
            layout.addWidget(original_title)
        posted = _posted_time(item.posted_at)
        identity = QLabel(" · ".join(value for value in (item.course_code or item.course_name, item.author_name, posted) if value))
        identity.setTextFormat(Qt.TextFormat.PlainText); identity.setWordWrap(True); layout.addWidget(identity)
        actions = QHBoxLayout()
        self.translate_button = QPushButton("重新翻译")
        self.translate_button.clicked.connect(self.translate)
        actions.addWidget(self.translate_button)
        self.translation_status = QLabel("正在准备本机离线翻译…" if self._source else "此公告没有可翻译的文字。")
        self.translation_status.setObjectName("announcementTranslationStatus"); self.translation_status.setWordWrap(True)
        actions.addWidget(self.translation_status, 1); layout.addLayout(actions)
        splitter = QSplitter(Qt.Orientation.Vertical); splitter.setChildrenCollapsible(False)
        self.original = QTextBrowser(); self.original.setObjectName("announcementOriginal")
        self.translation = QTextBrowser(); self.translation.setObjectName("announcementChinese")
        for caption, viewer, background in (("简体中文 · 本机离线译文", self.translation, PALETTE.peach), ("Canvas 完整原文", self.original, PALETTE.surface)):
            section = QWidget(); box = QVBoxLayout(section); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(4)
            box.addWidget(QLabel(caption)); box.addWidget(viewer, 1); splitter.addWidget(section)
            viewer.setOpenLinks(False); viewer.setOpenExternalLinks(False)
            viewer.setStyleSheet(reader_stylesheet(background=background))
        self.original.setHtml(safe_canvas_html(item.message, "Canvas 未提供公告正文。"))
        splitter.setSizes([340, 340]); layout.addWidget(splitter, 1)
        notice = QLabel("中文仅供参考，请以老师的 Canvas 原文为准。正文仅在本机翻译，不发送给第三方。链接不自动打开浏览器。")
        notice.setWordWrap(True); notice.setObjectName("statusText"); layout.addWidget(notice)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.translate()

    @Slot()
    def translate(self):
        if self._thread is not None or self._pending_result is not None or not self._source:
            self.translate_button.setEnabled(bool(self._source) and self._thread is None)
            return
        source = self._source
        self.translate_button.setEnabled(False)
        self.translation_status.setText("正在本机翻译，首次加载模型可能稍慢…")
        self._thread = QThread(self)
        self._worker = CanvasWorker(lambda: self._translator.translate(source)); self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._translated)
        self._worker.failed.connect(self._translation_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._finished)
        self._thread.start()

    @Slot(object)
    def _translated(self, text):
        if self._pending_result is None:
            self.translation.setPlainText(str(text))
            self.translation_status.setText("离线翻译完成 · 正文未发送给第三方")

    @Slot(str)
    def _translation_failed(self, message):
        self.translation_status.setText("离线翻译暂不可用，可点重新翻译；原文保持可读。")

    @Slot()
    def _finished(self):
        thread = self._thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
        self._thread = None; self._worker = None
        if self._pending_result is not None:
            result, self._pending_result = self._pending_result, None
            super().done(result)
            return
        self.translate_button.setEnabled(bool(self._source))

    def done(self, result):
        if self._thread is not None:
            self._pending_result = result
            self.translation_status.setText("正在结束当前本机翻译，完成后关闭…")
            return
        super().done(result)

    @Slot()
    def accept(self):
        self.done(QDialog.DialogCode.Accepted)

    @Slot()
    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        if self._thread is not None:
            self._pending_result = QDialog.DialogCode.Rejected
            self.translation_status.setText("正在结束当前本机翻译，完成后关闭…")
            event.ignore()
            return
        super().closeEvent(event)
