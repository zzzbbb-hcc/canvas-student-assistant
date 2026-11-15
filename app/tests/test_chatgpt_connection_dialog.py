"""Connection controls use explicit consent and never automate real sign-in."""

from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QWidget

from app.services.ai_service import AIAnswer
from app.services.chat_history import ChatHistoryStore
from app.storage.database import SQLiteDatabase
from app.storage.session_cache import session_cache
from app.ui.ai_chat import AIChatPage
from app.ui.chatgpt_connection_dialog import ChatGPTConnectionDialog
from app.ui.main_window import MainWindow


SECRET = "_".join(("SUPER", "SECRET", "DIALOG", "DIAGNOSTIC"))


class FakeAttempt:
    authorization_url = "https://auth.example.invalid/authorize?state=unit-test-only"

    def __init__(self, events):
        self.events = events
        self.listener_ready = True
        self.release = Event()
        self.started = Event()
        self.cancelled = False
        self.closed = False
        self.server = SimpleNamespace(server_close=self.close_server)

    def cancel(self):
        self.cancelled = True
        self.events.append(("cancel",))
        self.release.set()

    def close_server(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.events = []
        self.attempts = []
        self.records = [
            {"id": "account-one", "label": "Student One", "connected": True, "sharing": True},
            {"id": "account-two", "label": "Student Two", "connected": True, "sharing": True},
        ]
        self.active_id = "account-one"
        self.available_models = [
            {"slug": "account/model-beta", "display_name": "Account Beta"},
            {"slug": "account/model-alpha", "display_name": "Account Alpha"},
        ]
        self.fail_active = False
        self.fail_profiles = False
        self.fail_models = False
        self.fail_signout = False
        self.fail_finish = False
        self.login_sharing = False
        self.remote_revoked = False
        self.model_gate = None
        self.signout_gate = None

    @property
    def is_ready(self):
        return False

    def profiles(self):
        if self.fail_profiles:
            raise RuntimeError(f"Authorization: Bearer {SECRET}")
        return deepcopy(self.records)

    def active_profile(self):
        return next((deepcopy(record) for record in self.records if record["id"] == self.active_id), None)

    def set_active(self, identity):
        self.events.append(("set_active", identity))
        if self.fail_active:
            raise RuntimeError(f"Authorization: Bearer {SECRET}")
        self.active_id = identity

    def start_sign_in(self, identity=None):
        attempt = FakeAttempt(self.events)
        self.attempts.append(attempt)
        self.events.append(("listener_started", identity))
        return attempt

    def finish_sign_in(self, attempt):
        self.events.append(("finish_wait",))
        attempt.started.set()
        try:
            if not attempt.release.wait(5):
                raise RuntimeError("Fake sign-in timed out")
            if attempt.cancelled or self.fail_finish:
                raise RuntimeError(f"Authorization: Bearer {SECRET}")
            record = next(record for record in self.records if record["id"] == self.active_id)
            record["connected"] = True
            record["sharing"] = self.login_sharing
            return deepcopy(record)
        finally:
            attempt.close_server()

    def list_models(self):
        self.events.append(("models", self.active_id))
        if self.model_gate is not None and not self.model_gate.wait(5):
            raise RuntimeError("Fake model request timed out")
        if self.fail_models:
            raise RuntimeError(f"Authorization: Bearer {SECRET}")
        return deepcopy(self.available_models)

    def sign_out(self):
        self.events.append(("signout", self.active_id))
        if self.signout_gate is not None and not self.signout_gate.wait(5):
            raise RuntimeError("Fake sign-out timed out")
        if self.fail_signout:
            raise RuntimeError(f"Authorization: Bearer {SECRET}")
        for record in self.records:
            if record["id"] == self.active_id:
                record["connected"] = record["sharing"] = False
        return self.remote_revoked


class FakeLocalService:
    def ask(self, _question, **_kwargs):
        return AIAnswer("本机回答", (), False)


@pytest.fixture
def connection_env(qtbot, monkeypatch):
    session_cache.clear()
    database = SQLiteDatabase(":memory:")
    connection = FakeConnection()
    parent = QWidget()
    parent._connection_thread = None
    qtbot.addWidget(parent)
    dialogs = []
    opened = []

    def open_fake_url(url):
        opened.append(url.toString())
        connection.events.append(("browser_open",))
        assert connection.attempts and connection.attempts[-1].listener_ready
        return True

    def no_http(*_args, **_kwargs):
        raise AssertionError("Real HTTP is forbidden in dialog tests")

    monkeypatch.setattr(QDesktopServices, "openUrl", open_fake_url)
    monkeypatch.setattr(httpx.Client, "request", no_http)

    def make_dialog():
        dialog = ChatGPTConnectionDialog(connection, database, parent)
        qtbot.addWidget(dialog)
        dialogs.append(dialog)
        dialog.show()
        QApplication.processEvents()
        return dialog

    yield SimpleNamespace(database=database, connection=connection, parent=parent,
                          dialogs=dialogs, opened=opened, make_dialog=make_dialog)
    for attempt in connection.attempts:
        attempt.cancel()
    for gate in (connection.model_gate, connection.signout_gate):
        if gate is not None:
            gate.set()
    qtbot.waitUntil(lambda: all(dialog._thread is None for dialog in dialogs), timeout=10_000)
    for dialog in dialogs:
        dialog.close()
    database.close()
    session_cache.clear()


def wait_idle(qtbot, dialog):
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10_000)
    QApplication.processEvents()


def test_construct_requires_explicit_consent_and_makes_no_auth_or_model_requests(connection_env):
    env = connection_env
    dialog = env.make_dialog()
    assert not dialog.share_checkbox.isChecked()
    assert env.database.get_preference("chatgpt.allow_study_data", False) is False
    assert env.connection.events == []
    assert env.opened == []
    assert dialog.model_combo.count() == 0
    assert dialog._thread is None
    assert not dialog.cancel_button.isEnabled()


def test_saved_consent_is_restored_without_creating_new_authorization(connection_env):
    env = connection_env
    env.database.set_preference("chatgpt.allow_study_data", True)
    dialog = env.make_dialog()
    assert dialog.share_checkbox.isChecked()
    assert env.connection.events == []
    assert env.opened == []


def test_consent_can_be_granted_and_revoked_and_is_persisted(connection_env):
    env = connection_env
    dialog = env.make_dialog()
    dialog.share_checkbox.setChecked(True)
    assert env.database.get_preference("chatgpt.allow_study_data") is True
    dialog.share_checkbox.setChecked(False)
    assert env.database.get_preference("chatgpt.allow_study_data") is False
    assert "停止" in dialog.status_label.text()
    assert env.connection.events == []


@pytest.mark.parametrize("initial", [False, True])
def test_consent_save_failure_reverts_checkbox_without_raw_diagnostics(connection_env, monkeypatch, initial):
    env = connection_env
    env.database.set_preference("chatgpt.allow_study_data", initial)
    dialog = env.make_dialog()
    original = env.database.set_preference

    def fail_consent(key, value, *args, **kwargs):
        if key == "chatgpt.allow_study_data":
            raise RuntimeError(f"Authorization: Bearer {SECRET}")
        return original(key, value, *args, **kwargs)

    monkeypatch.setattr(env.database, "set_preference", fail_consent)
    dialog.share_checkbox.setChecked(not initial)
    assert dialog.share_checkbox.isChecked() is initial
    assert env.database.get_preference("chatgpt.allow_study_data") is initial
    assert "原授权" in dialog.status_label.text()
    assert SECRET not in dialog.status_label.text()


def test_selecting_saved_account_changes_only_active_account_and_not_models_automatically(connection_env):
    env = connection_env
    dialog = env.make_dialog()
    dialog.account_combo.setCurrentIndex(dialog.account_combo.findData("account-two"))
    assert env.connection.active_id == "account-two"
    assert env.connection.events == [("set_active", "account-two")]
    assert dialog.model_combo.count() == 0
    assert env.opened == []


def test_account_switch_failure_restores_original_selection(connection_env):
    env = connection_env
    dialog = env.make_dialog()
    env.connection.fail_active = True
    dialog.account_combo.setCurrentIndex(dialog.account_combo.findData("account-two"))
    assert env.connection.active_id == "account-one"
    assert dialog.account_combo.currentData() == "account-one"
    assert SECRET not in dialog.status_label.text()


def test_add_another_account_does_not_signout_or_query_models_for_previous_account(connection_env):
    env = connection_env
    dialog = env.make_dialog()
    dialog.account_combo.setCurrentIndex(dialog.account_combo.findData(None))
    assert dialog.account_combo.currentData() is None
    assert not dialog.models_button.isEnabled()
    assert not dialog.sign_out_button.isEnabled()
    dialog.models_button.click()
    dialog.sign_out_button.click()
    dialog._models()
    dialog._sign_out()
    assert env.connection.events == []
    assert env.connection.active_id == "account-one"


def test_models_are_loaded_only_by_action_and_use_actual_account_slugs(connection_env, qtbot):
    env = connection_env
    env.database.set_preference("chatgpt.model", "account/model-alpha")
    dialog = env.make_dialog()
    assert dialog.model_combo.count() == 0
    assert env.connection.events == []
    dialog.models_button.click()
    wait_idle(qtbot, dialog)
    assert env.connection.events == [("models", "account-one")]
    assert [dialog.model_combo.itemData(index) for index in range(dialog.model_combo.count())] == [
        "account/model-beta", "account/model-alpha",
    ]
    assert dialog.model_combo.currentData() == "account/model-alpha"
    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("account/model-beta"))
    assert env.database.get_preference("chatgpt.model") == "account/model-beta"
    assert env.opened == []


def test_login_binds_listener_before_browser_open_and_runs_wait_off_gui_thread(connection_env, qtbot):
    env = connection_env
    dialog = env.make_dialog()
    ticked = []
    QTimer.singleShot(0, lambda: ticked.append(True))
    dialog.login_button.click()
    attempt = env.connection.attempts[-1]
    qtbot.waitUntil(attempt.started.is_set)
    qtbot.waitUntil(lambda: bool(ticked))
    assert env.connection.events[0][0] == "listener_started"
    assert next(index for index, event in enumerate(env.connection.events) if event[0] == "listener_started") < \
        next(index for index, event in enumerate(env.connection.events) if event[0] == "browser_open")
    assert env.opened == [FakeAttempt.authorization_url]
    assert dialog._thread is not None
    assert env.parent._connection_thread is dialog._thread
    for control in (dialog.login_button, dialog.sign_out_button, dialog.account_combo,
                    dialog.model_combo, dialog.models_button):
        assert not control.isEnabled()
    assert dialog.cancel_button.isEnabled()
    attempt.release.set()
    wait_idle(qtbot, dialog)
    assert env.parent._connection_thread is None
    assert attempt.closed
    assert dialog.login_button.isEnabled()
    assert not dialog.cancel_button.isEnabled()


def test_cancel_login_preserves_saved_accounts_and_discards_raw_errors(connection_env, qtbot):
    env = connection_env
    before = deepcopy(env.connection.records)
    dialog = env.make_dialog()
    dialog.login_button.click()
    dialog.cancel_button.click()
    wait_idle(qtbot, dialog)
    assert env.connection.attempts[-1].cancelled
    assert env.connection.records == before
    assert SECRET not in dialog.status_label.text()
    assert "问答保留" in dialog.status_label.text()
    assert env.parent._connection_thread is None


@pytest.mark.parametrize("close_method", ["reject", "close"])
def test_closing_login_defers_until_worker_finishes_and_parent_reference_is_cleared(connection_env, qtbot, close_method):
    env = connection_env
    dialog = env.make_dialog()
    dialog.login_button.click()
    getattr(dialog, close_method)()
    assert dialog._close_pending
    wait_idle(qtbot, dialog)
    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert env.connection.attempts[-1].cancelled
    assert env.parent._connection_thread is None
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_close_during_uncancellable_model_request_waits_safely(connection_env, qtbot):
    env = connection_env
    env.connection.model_gate = Event()
    dialog = env.make_dialog()
    dialog.models_button.click()
    dialog.reject()
    assert dialog.isVisible()
    assert dialog._thread is not None
    assert not dialog.cancel_button.isEnabled()
    env.connection.model_gate.set()
    wait_idle(qtbot, dialog)
    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert env.parent._connection_thread is None


def test_signout_warns_when_remote_revocation_is_unconfirmed_without_deleting_history(connection_env, qtbot):
    env = connection_env
    history = ChatHistoryStore(env.database)
    conversation = history.create_conversation("general", "保存的课程问答")
    history.append_message(conversation.id, "user", "退出登录后仍需保留的问题")
    history.append_message(conversation.id, "assistant", "退出登录后仍需保留的回答")
    before = history.list_messages(conversation.id)
    dialog = env.make_dialog()
    dialog.sign_out_button.click()
    wait_idle(qtbot, dialog)
    assert env.connection.events == [("signout", "account-one")]
    assert history.list_messages(conversation.id) == before
    assert history.get_conversation(conversation.id).title == "保存的课程问答"
    assert "远端撤销未确认" in dialog.status_label.text()
    assert "问答不删除" in dialog.status_label.text()
    assert not env.connection.active_profile()["connected"]


@pytest.mark.parametrize("job", ["models", "signout"])
def test_failed_network_jobs_display_generic_messages_and_keep_accounts(connection_env, qtbot, job):
    env = connection_env
    before = deepcopy(env.connection.records)
    setattr(env.connection, "fail_" + job, True)
    dialog = env.make_dialog()
    (dialog.models_button if job == "models" else dialog.sign_out_button).click()
    wait_idle(qtbot, dialog)
    assert env.connection.records == before
    assert SECRET not in dialog.status_label.text()
    assert "Authorization" not in dialog.status_label.text()
    assert "已有账户和问答保留" in dialog.status_label.text()


def test_browser_open_failure_cancels_listener_and_never_uses_real_browser(connection_env, qtbot, monkeypatch):
    env = connection_env
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda _url: False)
    dialog = env.make_dialog()
    dialog.login_button.click()
    wait_idle(qtbot, dialog)
    assert env.connection.attempts[-1].cancelled
    assert env.connection.attempts[-1].closed
    assert env.parent._connection_thread is None
    assert env.opened == []
    assert SECRET not in dialog.status_label.text()


def test_multiple_cancelled_open_close_cycles_leave_no_live_qthread(connection_env, qtbot):
    env = connection_env
    for _index in range(8):
        dialog = env.make_dialog()
        dialog.login_button.click()
        dialog.reject()
        wait_idle(qtbot, dialog)
        assert env.parent._connection_thread is None
        assert not dialog.isVisible()
    assert all(attempt.closed for attempt in env.connection.attempts)


def test_page_connection_button_uses_injected_connection_without_auto_login(connection_env, qtbot):
    env = connection_env
    page = AIChatPage(FakeLocalService(), database=env.database, connection=env.connection)
    qtbot.addWidget(page)
    page.show()
    visited = []

    def inspect_and_close():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ChatGPTConnectionDialog)
        assert dialog._connection is env.connection
        visited.append(True)
        dialog.reject()

    QTimer.singleShot(0, inspect_and_close)
    page.connection_button.click()
    assert visited == [True]
    assert page._connection_thread is None
    assert env.connection.events == []
    assert env.opened == []


def test_closing_parent_page_waits_for_child_connection_and_can_reopen_normally(connection_env, qtbot):
    env = connection_env
    page = AIChatPage(FakeLocalService(), database=env.database, connection=env.connection)
    qtbot.addWidget(page)
    page.show()
    dialog = ChatGPTConnectionDialog(env.connection, env.database, page)
    qtbot.addWidget(dialog)
    env.dialogs.append(dialog)
    dialog.show()
    env.connection.model_gate = Event()
    dialog.models_button.click()
    assert page._connection_thread is dialog._thread
    page.close()
    assert page._close_pending
    assert dialog._close_pending
    assert page.isVisible()
    assert page._connection_thread is not None
    env.connection.model_gate.set()
    wait_idle(qtbot, dialog)
    qtbot.waitUntil(lambda: not page.isVisible())
    assert page._connection_thread is None
    assert not page._close_pending
    page.show()
    qtbot.wait(30)
    assert page.isVisible()


def test_consent_can_be_revoked_while_login_is_waiting(connection_env, qtbot):
    env = connection_env
    env.database.set_preference("chatgpt.allow_study_data", True)
    dialog = env.make_dialog()
    dialog.login_button.click()
    assert dialog.share_checkbox.isEnabled()
    dialog.share_checkbox.setChecked(False)
    assert env.database.get_preference("chatgpt.allow_study_data") is False
    dialog.cancel_button.click()
    wait_idle(qtbot, dialog)
    assert env.database.get_preference("chatgpt.allow_study_data") is False


def test_connection_button_keeps_compact_chat_reading_height(qtbot, monkeypatch):
    class NoCredentials:
        def has_canvas_token(self):
            return False

        def has_ai_api_key(self):
            return False

    session_cache.clear()
    monkeypatch.setattr("app.ui.main_window.CredentialStore", NoCredentials)
    monkeypatch.setattr(MainWindow, "_refresh_current_page", lambda *_args: None)
    window = MainWindow()
    window._reminder_timer.stop()
    qtbot.addWidget(window)
    window.resize(1100, 720)
    window.show()
    page = next(window.pages.widget(index) for index in range(window.pages.count())
                if isinstance(window.pages.widget(index), AIChatPage))
    window.pages.setCurrentWidget(page)
    qtbot.wait(30)
    try:
        assert page.chat_view.height() >= 450
        assert page.connection_button.isVisible()
        assert page.connection_button.geometry().left() > page.mode_label.geometry().right()
        assert page.question_input.isVisible() and page.ask_button.isVisible()
    finally:
        window.close()
        window._database.close()
        session_cache.clear()
