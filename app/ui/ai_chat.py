from __future__ import annotations

from collections.abc import Mapping
from html import escape
import re

from PySide6.QtCore import QThread, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.services.ai_service import AIService
from app.services.ai_tool_gateway import AIReadOnlyToolGateway
from app.services.canvas_study_context import CanvasStudyContextRetriever, read_limits_for_profile
from app.services.chatgpt_connection import ChatGPTConnection
from app.services.chat_history import (
    ChatGroup,
    ChatHistoryStore,
    ChatMessage,
    redact_chat_text,
)
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.canvas_worker import CanvasWorker
from app.ui.chatgpt_connection_dialog import ChatGPTConnectionDialog
from app.ui.doodle_icons import navigation_icon
from app.ui.theme import PALETTE as P, theme_font


class CachedCanvasReadOnlySource:
    """Expose only non-sensitive Canvas data already loaded by the UI."""

    @staticmethod
    def get_courses():
        # Dashboard can restore assignments before the Courses page is opened.
        # Build the same local course index without adding GUI-thread HTTP.
        courses = {}
        for key, from_assignment in (("courses", False), ("assignments", True)):
            for item in session_cache.get(key, []):
                def value(field, default=None):
                    return item.get(field, default) if isinstance(item, Mapping) else getattr(item, field, default)

                course_id = value("course_id" if from_assignment else "id")
                name = value("course_name" if from_assignment else "name")
                if (isinstance(course_id, bool) or not isinstance(course_id, int)
                        or not 0 < course_id < 2**63 or not isinstance(name, str) or not name.strip()):
                    continue
                code = value("course_code", "")
                courses.setdefault(course_id, {
                    "id": course_id, "name": name.strip()[:200],
                    "course_code": code[:200] if isinstance(code, str) else "",
                })
        return list(courses.values())

    @staticmethod
    def get_upcoming_assignments():
        return list(session_cache.get("assignments", []))

    @staticmethod
    def get_assignments(course_id):
        return [
            item
            for item in session_cache.get("assignments", [])
            if item.course_id == int(course_id)
        ]

    @staticmethod
    def get_announcements(course_id):
        return list(session_cache.get(f"announcements:{int(course_id)}", []))

    @staticmethod
    def get_modules(course_id):
        return list(session_cache.get(f"modules:{int(course_id)}", []))

    @staticmethod
    def get_module_items(course_id, module_id):
        return list(session_cache.get(f"module_items:{int(course_id)}:{int(module_id)}", []))


class AIChatPage(QWidget):
    """Read-only study assistance with durable, locally grouped conversations."""

    def __init__(
        self,
        service: AIService | None = None,
        *,
        database: SQLiteDatabase | None = None,
        history_store: ChatHistoryStore | None = None,
        credential_store=None,
        connection=None,
    ) -> None:
        super().__init__()
        self._database = database or getattr(history_store, "database", None) or SQLiteDatabase()
        try:
            profile = self._database.get_preference("ai.study_read_profile", "expanded")
        except Exception:
            profile = "expanded"
        self._study_read_profile = profile if profile in ("standard", "expanded") else "expanded"
        self._connection = connection
        self._credential_store = credential_store
        self._connection_thread = None
        if service is None:
            self._connection = connection or ChatGPTConnection(self._database, credential_store)
        self._service = service or AIService(
            AIReadOnlyToolGateway(canvas=CachedCanvasReadOnlySource()), provider=self._connection,
            context_retriever=CanvasStudyContextRetriever(
                credential_store,
                limits_provider=lambda: read_limits_for_profile(self._study_read_profile),
            ) if credential_store is not None else None,
        )
        self._history_store = history_store or ChatHistoryStore(self._database)
        self._groups: dict[str, ChatGroup] = {}
        self._messages: list[ChatMessage] = []
        self._current_group_id: str | None = None
        self._current_conversation_id: str | None = None
        self._group_selections: dict[str, str | None] = {}
        self._populating = False
        self._busy = False
        self._thread = None
        self._worker = None
        self._request_conversation_id = None
        self._close_pending = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        heading.setSpacing(14)
        title = QLabel("学习助手")
        title.setObjectName("pageTitle")
        subtitle = QLabel("问答保存在本机 · Canvas 只读检索 · 在线回答需授权")
        self.mode_label = subtitle
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(subtitle, 1)
        self.reading_range_combo = QComboBox()
        self.reading_range_combo.setObjectName("studyReadRangeCombo")
        self.reading_range_combo.setAccessibleName("资料读取范围")
        self.reading_range_combo.addItem("标准 · 50 页", "standard")
        self.reading_range_combo.addItem("扩展 · 200 页", "expanded")
        self.reading_range_combo.setCurrentIndex(self.reading_range_combo.findData(self._study_read_profile))
        self.reading_range_combo.setToolTip(
            "资料读取范围（PDF 页数／PPT 张数）：\n"
            "标准：50 页，单份 25,000 字符，合计 100,000 字符。\n"
            "扩展：200 页，单份 100,000 字符，合计 400,000 字符。\n"
            "每份文件仍限 8 MB。扩大范围可能增加耗时及在线模型额度消耗；\n"
            "不改变发送资料的授权开关，不会自动请求回答。仅对下一次检索生效。"
        )
        self.reading_range_combo.currentIndexChanged.connect(self._reading_range_changed)
        heading.addWidget(self.reading_range_combo)
        self.connection_button = QPushButton("连接 ChatGPT")
        self.connection_button.setObjectName("chatgptConnectionButton")
        self.connection_button.clicked.connect(self._configure_connection)
        heading.addWidget(self.connection_button)
        layout.addLayout(heading)

        groups = QHBoxLayout()
        groups.setSpacing(6)
        groups.addWidget(QLabel("分组"))
        self.group_combo = QComboBox()
        self.group_combo.setObjectName("chatGroupCombo")
        self.group_combo.setMinimumContentsLength(12)
        self.group_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.group_combo.currentIndexChanged.connect(self._group_changed)
        groups.addWidget(self.group_combo, 1)
        self.new_group_button = self._secondary_button("新建分组", self._new_group)
        self.rename_group_button = self._secondary_button("重命名分组", self._rename_group)
        groups.addWidget(self.new_group_button)
        groups.addWidget(self.rename_group_button)
        layout.addLayout(groups)

        conversations = QHBoxLayout()
        conversations.setSpacing(6)
        conversations.addWidget(QLabel("对话"))
        self.conversation_combo = QComboBox()
        self.conversation_combo.setObjectName("chatConversationCombo")
        self.conversation_combo.setMinimumContentsLength(12)
        self.conversation_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.conversation_combo.currentIndexChanged.connect(self._conversation_changed)
        conversations.addWidget(self.conversation_combo, 1)
        self.new_chat_button = self._secondary_button("新对话", self._new_chat)
        self.rename_chat_button = self._secondary_button("重命名", self._rename_chat)
        self.move_chat_button = self._secondary_button("移动到…", self._move_chat)
        for button in (self.new_chat_button, self.rename_chat_button, self.move_chat_button):
            conversations.addWidget(button)
        self.suggestions_button = QPushButton("快捷提问")
        self.suggestions_button.setObjectName("secondaryButton")
        suggestions = QMenu(self.suggestions_button)
        for label, question in (
            ("今天截止", "今天有哪些作业截止？"),
            ("明天截止", "明天有哪些作业截止？"),
            ("本周截止", "本周有哪些作业截止？"),
            ("优先做什么", "按截止时间，我应该优先做哪项作业？"),
        ):
            action = suggestions.addAction(label)
            action.triggered.connect(lambda _checked=False, text=question: self._ask_text(text))
        self.suggestions_button.setMenu(suggestions)
        conversations.addWidget(self.suggestions_button)
        layout.addLayout(conversations)

        self.chat_view = QTextBrowser()
        self.chat_view.setObjectName("chatView")
        self.chat_view.document().setDefaultFont(theme_font())
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setOpenLinks(False)
        layout.addWidget(self.chat_view, 1)

        composer = QFrame()
        composer.setObjectName("contentCard")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(8, 6, 8, 6)
        composer_layout.setSpacing(8)
        self.question_input = QLineEdit()
        self.question_input.setPlaceholderText("输入问题… 问答会自动保存，请勿粘贴密码或令牌")
        self.question_input.returnPressed.connect(self.ask)
        self.ask_button = QPushButton("发送")
        self.ask_button.setIcon(navigation_icon("assistant"))
        self.ask_button.clicked.connect(self.ask)
        composer_layout.addWidget(self.question_input, 1)
        composer_layout.addWidget(self.ask_button)
        layout.addWidget(composer)
        self.status_label = QLabel()
        self.status_label.setObjectName("statusText")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        selected_group = selected_conversation = None
        have_selection = False
        try:
            selection = self._database.get_preference("ai_chat_selection", None)
            if isinstance(selection, dict):
                selected_group = selection.get("group_id")
                selected_conversation = selection.get("conversation_id")
                have_selection = True
        except Exception:
            pass
        try:
            self._reload_history(
                selected_group,
                selected_conversation,
                choose_latest=not have_selection,
                sync_courses=True,
            )
        except Exception:
            self._render_history()
            self._update_actions()
            self.status_label.setText("暂时无法读取本机对话记录；请稍后重新进入此页面。")

    def _secondary_button(self, text: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("secondaryButton")
        button.clicked.connect(callback)
        return button

    def _reading_range_changed(self, _index: int) -> None:
        profile = self.reading_range_combo.currentData()
        if self._busy or profile not in ("standard", "expanded"):
            self._restore_reading_range()
            return
        try:
            self._database.set_preference("ai.study_read_profile", profile)
        except Exception:
            self._restore_reading_range()
            self.status_label.setText("未能保存读取范围，仍使用原设置；已有问答没有改动。")
            return
        self._study_read_profile = profile
        self._set_local_status()

    def _restore_reading_range(self) -> None:
        blocked = self.reading_range_combo.blockSignals(True)
        try:
            self.reading_range_combo.setCurrentIndex(
                self.reading_range_combo.findData(self._study_read_profile)
            )
        finally:
            self.reading_range_combo.blockSignals(blocked)

    def refresh(self) -> None:
        if self._busy:
            return
        try:
            self._reload_history(
                self._current_group_id,
                self._current_conversation_id,
                sync_courses=True,
            )
        except Exception:
            # Do not replace a readable transcript with an empty page on a
            # transient storage failure. Sending will still require a commit.
            self.status_label.setText("暂时无法刷新本机记录，当前对话仍保留在页面中。")

    def _reload_history(
        self,
        group_id: str | None,
        conversation_id: str | None,
        *,
        choose_latest: bool = False,
        sync_courses: bool = False,
    ) -> None:
        groups = (
            self._history_store.ensure_course_groups(CachedCanvasReadOnlySource.get_courses())
            if sync_courses else self._history_store.list_groups()
        )
        group_lookup = {group.id: group for group in groups}
        if group_id not in group_lookup:
            group_id = groups[0].id if groups else None
        conversations = self._history_store.list_conversations(group_id) if group_id else []
        conversation_ids = {conversation.id for conversation in conversations}
        if conversation_id not in conversation_ids:
            conversation_id = conversations[0].id if choose_latest and conversations else None
        messages = self._history_store.list_messages(conversation_id) if conversation_id else []

        # All reads succeed before committing the new visible selection.
        self._groups = group_lookup
        self._current_group_id = group_id
        self._current_conversation_id = conversation_id
        self._messages = messages
        self._populating = True
        try:
            self.group_combo.clear()
            for group in groups:
                self.group_combo.addItem(group.name, group.id)
            self.group_combo.setCurrentIndex(self.group_combo.findData(group_id))
            self.conversation_combo.clear()
            self.conversation_combo.addItem("新对话（发送后自动保存）", None)
            for conversation in conversations:
                self.conversation_combo.addItem(conversation.title, conversation.id)
            self.conversation_combo.setCurrentIndex(self.conversation_combo.findData(conversation_id))
        finally:
            self._populating = False
        if group_id:
            self._group_selections[group_id] = conversation_id
        self._render_history()
        self._update_actions()
        self._set_local_status()
        self._remember_selection()

    def _remember_selection(self) -> None:
        try:
            self._database.set_preference("ai_chat_selection", {
                "group_id": self._current_group_id,
                "conversation_id": self._current_conversation_id,
            })
        except Exception:
            self.status_label.setText("对话记录已保留，但暂时无法记住当前选择。")

    def _set_local_status(self) -> None:
        group = self._groups.get(self._current_group_id)
        scope = "仅查询此课程" if group and group.course_id is not None else "查询已缓存的全部课程"
        count = len(session_cache.get("assignments", []))
        ready = bool(getattr(self._service, "provider_configured", False))
        self.mode_label.setText("使用 ChatGPT 套餐 · 当前对话和相关资料将联网处理" if ready else
                                "本机检索模式 · 问答自动保存 · 在线回答需连接并授权")
        self.connection_button.setText("ChatGPT 连接设置" if ready else "连接 ChatGPT")
        self.status_label.setText(
            f"{'ChatGPT' if ready else '本机检索'} · {len(self._messages)} 条已保存消息 · {scope} · "
            f"{count} 项作业缓存 · 读取上限 {read_limits_for_profile(self._study_read_profile).pages} 页／张 · "
            "截止时间以 Canvas due_at 为准"
        )

    def _update_actions(self) -> None:
        available = bool(self._current_group_id) and not self._busy
        self.group_combo.setEnabled(not self._busy)
        self.conversation_combo.setEnabled(available)
        self.new_group_button.setEnabled(not self._busy)
        self.rename_group_button.setEnabled(available)
        self.new_chat_button.setEnabled(available)
        self.rename_chat_button.setEnabled(available and self._current_conversation_id is not None)
        self.move_chat_button.setEnabled(available and self._current_conversation_id is not None)
        self.suggestions_button.setEnabled(available)
        self.ask_button.setEnabled(available)
        self.question_input.setEnabled(not self._busy)
        self.connection_button.setEnabled(not self._busy)
        self.reading_range_combo.setEnabled(not self._busy)

    def _configure_connection(self):
        if self._busy:
            return
        try:
            if self._connection is None:
                self._connection = ChatGPTConnection(self._database, self._credential_store)
            dialog = ChatGPTConnectionDialog(self._connection, self._database, self)
            dialog.exec()
            self._set_local_status()
        except Exception:
            self.status_label.setText("ChatGPT 连接设置暂不可用，本机历史问答不受影响。")

    def _group_changed(self, _index: int) -> None:
        if self._populating or self._busy:
            return
        group_id = self.group_combo.currentData()
        try:
            self._reload_history(
                group_id,
                self._group_selections.get(group_id),
                choose_latest=group_id not in self._group_selections,
            )
        except Exception:
            self._restore_combo_selection()
            self.status_label.setText("暂时无法读取此分组，原对话没有改动。")

    def _conversation_changed(self, _index: int) -> None:
        if self._populating or self._busy:
            return
        try:
            self._reload_history(self._current_group_id, self.conversation_combo.currentData())
        except Exception:
            self._restore_combo_selection()
            self.status_label.setText("暂时无法读取此对话，原记录没有改动。")

    def _restore_combo_selection(self) -> None:
        self._populating = True
        try:
            self.group_combo.setCurrentIndex(self.group_combo.findData(self._current_group_id))
            self.conversation_combo.setCurrentIndex(self.conversation_combo.findData(self._current_conversation_id))
        finally:
            self._populating = False

    def _new_group(self) -> None:
        if self._busy:
            return
        name, accepted = QInputDialog.getText(self, "新建分组", "分组名称：")
        if not accepted:
            return
        try:
            group = self._history_store.create_group(name)
            self._reload_history(group.id, None)
        except Exception:
            self.status_label.setText("未能保存分组，请使用 1–200 字的名称后重试。")

    def _rename_group(self) -> None:
        if self._busy:
            return
        group = self._groups.get(self._current_group_id)
        if group is None:
            return
        name, accepted = QInputDialog.getText(self, "重命名分组", "分组名称：", text=group.name)
        if not accepted:
            return
        try:
            self._history_store.rename_group(group.id, name)
            self._reload_history(group.id, self._current_conversation_id)
        except Exception:
            self.status_label.setText("未能重命名分组，原记录保留；名称须为 1–200 字。")

    def _new_chat(self) -> None:
        if self._busy:
            return
        try:
            self._reload_history(self._current_group_id, None)
            self.question_input.setFocus()
        except Exception:
            self.status_label.setText("暂时无法打开新对话，已有记录没有改动。")

    def _rename_chat(self) -> None:
        if self._busy or self._current_conversation_id is None:
            return
        title, accepted = QInputDialog.getText(
            self, "重命名对话", "对话名称：", text=self.conversation_combo.currentText(),
        )
        if not accepted:
            return
        try:
            self._history_store.rename_conversation(self._current_conversation_id, title)
            self._reload_history(self._current_group_id, self._current_conversation_id)
        except Exception:
            self.status_label.setText("未能重命名对话，问答记录保留；名称须为 1–300 字。")

    def _move_chat(self) -> None:
        if self._busy or self._current_conversation_id is None:
            return
        groups = list(self._groups.values())
        # Duplicate user-chosen names remain distinguishable without exposing
        # or changing the underlying Canvas course identity.
        choices = [f"{index + 1}. {group.name}" for index, group in enumerate(groups)]
        current = next((index for index, group in enumerate(groups)
                        if group.id == self._current_group_id), 0)
        choice, accepted = QInputDialog.getItem(
            self, "移动对话", "选择目标分组（问答内容全部保留）：", choices, current, False,
        )
        if not accepted or choice not in choices:
            return
        group_id = groups[choices.index(choice)].id
        try:
            moved = self._history_store.move_conversation(self._current_conversation_id, group_id)
            self._reload_history(group_id, moved.id)
            self.status_label.setText("对话已移动，历史问答全部保留；后续提问使用目标分组的课程范围。")
        except Exception:
            self.status_label.setText("暂时无法移动对话，已有问答不会删除。")

    def _render_history(self) -> None:
        if not self._messages:
            self.chat_view.setHtml(
                "<div style='padding:12px'><h3>这里会保留你问过的内容</h3>"
                "<p>每次提问和回答会自动保存在本机；重新进入或重启后，仍可选择原对话继续。"
                "可按课程、自建分组整理，也可重命名或移动对话。</p>"
                "<p>可以问“5891 group report 怎么写”，本机会定位作业说明、评分标准及相关课件。"
                "连接 ChatGPT 并允许资料发送后，可据此生成分析；未授权时仅展示检索资料。"
                "请不要在问题中粘贴密码、Cookie 或令牌。</p></div>"
            )
            return
        blocks = []
        for message in self._messages:
            label = {"user": "我", "assistant": "学习助手", "error": "回答未完成"}.get(message.role, "消息")
            background = P.peach if message.role == "user" else P.warning_bg if message.role == "error" else P.surface
            timestamp = message.created_at.astimezone().strftime("%m-%d %H:%M")
            content = escape(message.content).replace("\n", "<br>")
            sources = ""
            if message.sources:
                sources = f"<div style='color:{P.text_muted};font-size:12px;margin-top:8px'>来源：" + \
                    escape(" · ".join(message.sources)) + "</div>"
            blocks.append(
                f"<div style='background:{background};color:{P.text};border:1px solid {P.border};border-radius:12px;"
                f"padding:12px;margin:8px 0'><b>{label}</b> "
                f"<span style='color:{P.text_muted};font-size:12px'>{timestamp}</span><br>{content}{sources}</div>"
            )
        self.chat_view.setHtml("<div style='padding:8px'>" + "".join(blocks) + "</div>")
        self.chat_view.verticalScrollBar().setValue(self.chat_view.verticalScrollBar().maximum())

    def ask(self) -> None:
        self._ask_text(self.question_input.text())

    def _ask_text(self, question: str) -> None:
        if self._busy:
            return
        question = question.strip()
        if not question:
            self.status_label.setText("请输入学习问题。")
            return
        if len(question) > 8_000:
            self.status_label.setText("问题太长，请缩短至 8,000 字以内。")
            return
        group = self._groups.get(self._current_group_id)
        if group is None:
            self.status_label.setText("请先选择可用分组；当前问题尚未保存。")
            return
        prior_messages = [message for message in self._messages if message.role in {"user", "assistant"}]
        try:
            if self._current_conversation_id is None:
                title = re.sub(r"\s+", " ", redact_chat_text(question)).strip()[:60] or "新对话"
                conversation = self._history_store.create_conversation(group.id, title)
                self._current_conversation_id = conversation.id
                self._populating = True
                try:
                    self.conversation_combo.addItem(conversation.title, conversation.id)
                    self.conversation_combo.setCurrentIndex(self.conversation_combo.findData(conversation.id))
                finally:
                    self._populating = False
            saved_question = self._history_store.append_message(
                self._current_conversation_id, "user", question,
            )
        except Exception:
            self._update_actions()
            self.status_label.setText("未能保存问题，因此没有请求回答；输入内容仍保留，请稍后重试。")
            return
        # Never clear an unsaved question. Shortcut actions also leave an
        # unrelated draft in the composer alone.
        if self.question_input.text().strip() == question:
            self.question_input.clear()
        self._messages.append(saved_question)
        self._group_selections[group.id] = self._current_conversation_id
        self._render_history()
        self._remember_selection()
        self._busy = True
        self._update_actions()
        self._request_conversation_id = self._current_conversation_id
        self.status_label.setText("问题已保存在本机，正在读取课程资料并回答…")
        self._thread = QThread(self)
        self._worker = CanvasWorker(lambda: self._service.ask(
            saved_question.content,
            course_id=group.course_id,
            history=tuple(prior_messages),
        ))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._answer_ready)
        self._worker.failed.connect(self._answer_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._answer_finished)
        self._thread.start()

    @Slot(object)
    def _answer_ready(self, answer) -> None:
        try:
            saved_answer = self._history_store.append_message(
                self._request_conversation_id, "assistant", answer.text, answer.sources,
            )
        except Exception:
            self.status_label.setText("问题已保存，但回答未能保存；请稍后重试。")
            return
        if self._current_conversation_id == self._request_conversation_id:
            self._messages.append(saved_answer)
            self._render_history()
        self.status_label.setText(
            "问答已保存在本机 · 本机 Canvas 查询／检索结果 · 未使用在线模型"
            if not getattr(answer, "provider_used", False)
            else "问答已保存在本机 · 由已配置的模型使用脱敏资料回答"
        )

    @Slot(str)
    def _answer_failed(self, _message: str) -> None:
        # Provider/SDK exceptions may carry headers or credentials; neither
        # display nor persist their raw diagnostics.
        failure = "本次回答未完成，你的问题已保存，可稍后继续提问。"
        try:
            saved_error = self._history_store.append_message(
                self._request_conversation_id, "error", failure,
            )
            if self._current_conversation_id == self._request_conversation_id:
                self._messages.append(saved_error)
        except Exception:
            pass
        self._render_history()
        self.status_label.setText(failure)

    @Slot()
    def _answer_finished(self) -> None:
        # Qt's finished signal can precede native TLS destructor completion.
        # Join that tail before releasing wrappers or starting another worker.
        thread = self._thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
        self._thread = None
        self._worker = None
        self._request_conversation_id = None
        self._busy = False
        self._update_actions()
        if self._close_pending:
            self._close_pending = False
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event) -> None:
        if self._thread is not None or self._connection_thread is not None:
            self._close_pending = True
            for dialog in self.findChildren(ChatGPTConnectionDialog):
                dialog.reject()
            event.ignore()
            return
        super().closeEvent(event)
