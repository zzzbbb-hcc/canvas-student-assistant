"""Explicit, user-operated ChatGPT login and course-data sharing controls."""

from PySide6.QtCore import QSignalBlocker, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

from app.ui.canvas_worker import CanvasWorker


class ChatGPTConnectionDialog(QDialog):
    def __init__(self, connection, database, parent=None):
        super().__init__(parent)
        self._connection, self._database = connection, database
        self._thread = self._worker = self._attempt = None
        self._job = ""
        self._close_pending = False
        self._load_models_next = False
        self._active_profile_id = None
        self.setWindowTitle("连接 ChatGPT")
        self.resize(610, 380)
        layout = QVBoxLayout(self)
        intro = QLabel("使用你自己的 ChatGPT 账户授权。不会读取 Codex 的登录令牌，也不会继承 ChatGPT 的其他聊天记录。符合条件的套餐和额度以授权结果为准。")
        intro.setWordWrap(True); layout.addWidget(intro)
        self.account_combo = QComboBox(); self.account_combo.setObjectName("chatgptAccountCombo")
        self.account_combo.currentIndexChanged.connect(self._select_account)
        layout.addWidget(self.account_combo)
        actions = QHBoxLayout()
        self.login_button = QPushButton("Continue with ChatGPT")
        self.login_button.setObjectName("chatgptSignInButton")
        self.login_button.clicked.connect(self._login)
        self.sign_out_button = QPushButton("退出当前账户")
        self.sign_out_button.clicked.connect(self._sign_out)
        self.cancel_button = QPushButton("取消本次连接")
        self.cancel_button.clicked.connect(self._cancel)
        for button in (self.login_button, self.sign_out_button, self.cancel_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.model_combo = QComboBox(); self.model_combo.setObjectName("chatgptModelCombo")
        self.model_combo.currentIndexChanged.connect(self._select_model)
        layout.addWidget(self.model_combo)
        self.models_button = QPushButton("读取当前账户可用模型")
        self.models_button.clicked.connect(self._models)
        layout.addWidget(self.models_button)
        self.share_checkbox = QCheckBox("允许将当前问题、当前对话的近期记录及相关课程资料发送给 OpenAI")
        self.share_checkbox.setObjectName("chatgptStudyDataConsent")
        self.share_checkbox.setChecked(database.get_preference("chatgpt.allow_study_data", False) is True)
        self.share_checkbox.toggled.connect(self._share_changed)
        layout.addWidget(self.share_checkbox)
        notice = QLabel("不勾选时只在本机检索。Canvas PAT、密码和 Cookie 不提供给模型。联网使用可能消耗你的 ChatGPT 套餐额度；此处不会自动切换为付费 API。可随时取消资料发送。")
        notice.setWordWrap(True); layout.addWidget(notice)
        self.status_label = QLabel(); self.status_label.setWordWrap(True)
        self.status_label.setObjectName("chatgptConnectionStatus"); layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self._reload_accounts()
        self._controls()

    def _reload_accounts(self):
        try:
            profiles = self._connection.profiles()
            active = self._connection.active_profile()
            self._active_profile_id = active["id"] if active else None
            with QSignalBlocker(self.account_combo):
                self.account_combo.clear()
                for profile in profiles:
                    label = profile["label"] + " · " + profile["id"][:6]
                    self.account_combo.addItem(label, profile["id"])
                self.account_combo.addItem("添加另一个账户／工作区", None)
                self.account_combo.setCurrentIndex(self.account_combo.findData(active["id"] if active else None))
            self.status_label.setText("已登录；模型访问以首次提问成功为准。" if active and active["sharing"] else
                                      "已登录，但尚未获得 ChatGPT 套餐使用授权。" if active and active["connected"] else
                                      "尚未连接。点击 Continue with ChatGPT，在浏览器中亲自完成登录和授权。")
        except Exception:
            self.status_label.setText("暂时无法读取受保护的账户信息，请稍后重试。")

    def _controls(self):
        busy = self._thread is not None
        for widget in (self.account_combo, self.login_button, self.sign_out_button,
                       self.models_button, self.model_combo):
            widget.setEnabled(not busy)
        self.sign_out_button.setEnabled(not busy and self.account_combo.currentData() is not None)
        self.models_button.setEnabled(not busy and self.account_combo.currentData() is not None)
        self.cancel_button.setEnabled(busy and self._attempt is not None)

    def _select_account(self, _index):
        if self._thread is not None:
            return
        identity = self.account_combo.currentData()
        self.model_combo.clear()
        if identity is not None:
            try:
                self._connection.set_active(identity)
                self._active_profile_id = identity
                self.status_label.setText("已切换账户，请读取该账户的可用模型。")
            except Exception:
                with QSignalBlocker(self.account_combo):
                    self.account_combo.setCurrentIndex(self.account_combo.findData(self._active_profile_id))
                self.status_label.setText("账户切换失败，原连接保持不变。")
        self._controls()

    def _share_changed(self, checked):
        try:
            self._database.set_preference("chatgpt.allow_study_data", bool(checked))
            self.status_label.setText("已允许联网处理所选课程资料，可随时取消。" if checked else "已停止向 OpenAI 发送课程资料，后续提问仅在本机检索。")
        except Exception:
            with QSignalBlocker(self.share_checkbox):
                self.share_checkbox.setChecked(not checked)
            self.status_label.setText("设置未能保存，原授权选择保持不变。")

    def _select_model(self, _index):
        model = self.model_combo.currentData()
        if model is not None:
            try:
                self._database.set_preference("chatgpt.model", model)
            except Exception:
                self.status_label.setText("暂时无法保存模型选择。")

    def _login(self):
        if self._thread is not None:
            return
        try:
            self._attempt = self._connection.start_sign_in(self.account_combo.currentData())
            attempt = self._attempt
            self._run("login", lambda: self._connection.finish_sign_in(attempt))
            if not QDesktopServices.openUrl(QUrl(attempt.authorization_url)):
                attempt.cancel()
                self.status_label.setText("无法打开默认浏览器，登录已取消；请检查后重试。")
        except Exception:
            if self._attempt is not None:
                self._attempt.cancel()
                if self._thread is None:
                    self._attempt.server.server_close()
                self._attempt = None
            self.status_label.setText("未能启动登录，请检查 Windows 凭据管理器和网络后重试。")

    def _models(self):
        if self._thread is None and self.account_combo.currentData() is not None:
            self._run("models", self._connection.list_models)

    def _sign_out(self):
        if self._thread is None and self.account_combo.currentData() is not None:
            self._run("signout", self._connection.sign_out)

    def _run(self, job, operation):
        self._job = job
        self._thread = QThread(self)
        self._worker = CanvasWorker(operation)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._succeeded)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._finished)
        if self.parent() is not None:
            self.parent()._connection_thread = self._thread
        self.status_label.setText("请在浏览器中完成登录和授权；此处不会替你操作登录。" if job == "login" else "正在连接 ChatGPT…")
        self._controls()
        self._thread.start()

    @Slot(object)
    def _succeeded(self, result):
        try:
            self._apply_result(result)
        except Exception:
            self._load_models_next = False
            self.status_label.setText("连接结果暂时无法显示或保存，请重试；已有问答不受影响。")

    def _apply_result(self, result):
        if self._job == "models":
            selected = self._database.get_preference("chatgpt.model")
            with QSignalBlocker(self.model_combo):
                self.model_combo.clear()
                for model in result:
                    self.model_combo.addItem(model["display_name"], model["slug"])
                self.model_combo.setCurrentIndex(max(0, self.model_combo.findData(selected)))
            self._select_model(0)
            self.status_label.setText("已读取账户模型。实际可用性与额度以首次提问成功为准。")
        else:
            self._reload_accounts()
            if self._job == "login":
                self._load_models_next = bool(result.get("sharing"))
            elif self._job == "signout":
                self.model_combo.clear()
                self.status_label.setText("已退出当前账户，本机问答不删除。" if result else
                    "已清除本机登录凭证；远端撤销未确认，可到 ChatGPT 设置断开此 App。本机问答不删除。")

    @Slot(str)
    def _failed(self, _message):
        self.status_label.setText("连接未完成或授权不可用。请检查网络、套餐权限后重试；已有账户和问答保留。")

    @Slot()
    def _finished(self):
        thread = self._thread
        if thread is not None:
            thread.wait(); thread.deleteLater()
        self._thread = self._worker = self._attempt = None
        if self.parent() is not None:
            self.parent()._connection_thread = None
        self._controls()
        if self._close_pending:
            super().done(QDialog.DialogCode.Rejected)
        elif self._load_models_next:
            self._load_models_next = False
            self._models()
        parent = self.parent()
        if parent is not None and getattr(parent, "_close_pending", False) and getattr(parent, "_thread", None) is None:
            parent._close_pending = False
            QTimer.singleShot(0, parent.close)

    def _cancel(self):
        if self._attempt is not None:
            self._attempt.cancel()

    @Slot()
    def reject(self):
        if self._thread is not None:
            self._close_pending = True
            self._cancel()
            self.status_label.setText("正在结束当前连接，完成后关闭…")
            return
        super().reject()

    def closeEvent(self, event):
        if self._thread is not None:
            self._close_pending = True
            self._cancel(); event.ignore()
            return
        super().closeEvent(event)
