from __future__ import annotations

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.services.credential_store import CredentialStore
from app.services.canvas_client import CanvasClient, CanvasClientError
from app.services.notification_service import NotificationService
from app.storage.cache import Cache
from app.storage.session_cache import session_cache
from app.ui.canvas_worker import CanvasWorker, is_thread_running


class SettingsPage(QWidget):
    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        *,
        cache: Cache | None = None,
        notification_service: NotificationService | None = None,
    ) -> None:
        super().__init__()
        self._credential_store = credential_store or CredentialStore()
        self._cache = cache or Cache()
        self._notification_service = notification_service or NotificationService(
            self._cache.database
        )
        self._connection_thread: QThread | None = None
        self._connection_worker: CanvasWorker | None = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setObjectName("settingsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(8)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        heading = QLabel("Settings")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        group = QGroupBox("Canvas Connection")
        group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        form = QFormLayout(group)
        self._compact_form(form)

        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("Enter Canvas Personal Access Token")
        self.token_input.setClearButtonEnabled(True)
        form.addRow("Token", self.token_input)

        self.show_token = QCheckBox("Show token while typing")
        self.show_token.toggled.connect(self._set_token_visible)
        form.addRow("", self.show_token)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.save_button = QPushButton("Save Token")
        self.delete_button = QPushButton("Delete Token")
        self.test_button = QPushButton("Test Canvas Connection")
        self.save_button.clicked.connect(self._save_token)
        self.delete_button.clicked.connect(self._delete_token)
        self.test_button.clicked.connect(self._test_connection)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.test_button)
        buttons.addStretch()
        form.addRow("", buttons)

        self.status_label = QLabel()
        self.status_label.setObjectName("credentialStatus")
        self.status_label.setWordWrap(True)
        form.addRow("Status", self.status_label)
        layout.addWidget(group)

        reminder_group = QGroupBox("Deadline Reminders")
        reminder_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        reminder_form = QFormLayout(reminder_group)
        self._compact_form(reminder_form)
        self.reminders_enabled = QCheckBox("Enable deadline reminders")
        reminder_form.addRow("Reminders", self.reminders_enabled)
        self.reminder_7_days = QCheckBox("7 days before")
        self.reminder_3_days = QCheckBox("3 days before")
        self.reminder_1_day = QCheckBox("1 day before")
        self.reminder_6_hours = QCheckBox("6 hours before")
        self.reminder_overdue = QCheckBox("One overdue reminder")
        self.notify_after_submitted = QCheckBox("Notify even after submitted")
        reminder_options = QGridLayout()
        reminder_options.setHorizontalSpacing(12)
        reminder_options.setVerticalSpacing(4)
        for index, checkbox in enumerate((
            self.reminder_7_days,
            self.reminder_3_days,
            self.reminder_1_day,
            self.reminder_6_hours,
            self.reminder_overdue,
            self.notify_after_submitted,
        )):
            reminder_options.addWidget(checkbox, index // 2, index % 2)
        reminder_form.addRow("Rules", reminder_options)
        reminder_buttons = QHBoxLayout()
        reminder_buttons.setSpacing(6)
        self.test_notification_button = QPushButton("Test Notification")
        self.clear_cache_button = QPushButton("Clear Cache")
        self.clear_cache_button.setObjectName("secondaryButton")
        reminder_buttons.addWidget(self.test_notification_button)
        reminder_buttons.addWidget(self.clear_cache_button)
        reminder_buttons.addStretch()
        reminder_form.addRow("", reminder_buttons)
        self.reminder_status_label = QLabel(
            "Reminders are checked at startup and while the app is running."
        )
        self.reminder_status_label.setWordWrap(True)
        reminder_form.addRow("Status", self.reminder_status_label)
        layout.addWidget(reminder_group)

        preferences = self._notification_service.preferences
        self.reminders_enabled.setChecked(preferences.enabled)
        self.reminder_7_days.setChecked(preferences.seven_days)
        self.reminder_3_days.setChecked(preferences.three_days)
        self.reminder_1_day.setChecked(preferences.one_day)
        self.reminder_6_hours.setChecked(preferences.six_hours)
        self.reminder_overdue.setChecked(preferences.overdue)
        self.notify_after_submitted.setChecked(preferences.notify_after_submitted)
        for checkbox in (
            self.reminders_enabled,
            self.reminder_7_days,
            self.reminder_3_days,
            self.reminder_1_day,
            self.reminder_6_hours,
            self.reminder_overdue,
            self.notify_after_submitted,
        ):
            checkbox.toggled.connect(self._save_reminder_preferences)
        self.reminders_enabled.toggled.connect(self._set_reminder_controls_enabled)
        self.test_notification_button.clicked.connect(self._test_notification)
        self.clear_cache_button.clicked.connect(self._clear_cache)
        self._set_reminder_controls_enabled(preferences.enabled)

        ai_group = QGroupBox("AI Assistant")
        ai_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        ai_form = QFormLayout(ai_group)
        self._compact_form(ai_form)
        self.ai_key_input = QLineEdit()
        self.ai_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key_input.setPlaceholderText("Optional AI provider API key")
        self.ai_key_input.setClearButtonEnabled(True)
        ai_form.addRow("API Key", self.ai_key_input)
        self.ai_show_key = QCheckBox("Show key while typing")
        self.ai_show_key.toggled.connect(
            lambda visible: self._set_secret_visible(self.ai_key_input, visible)
        )
        ai_form.addRow("", self.ai_show_key)
        ai_buttons = QHBoxLayout()
        ai_buttons.setSpacing(6)
        self.ai_save_button = QPushButton("Save / Replace Key")
        self.ai_delete_button = QPushButton("Delete Key")
        self.ai_save_button.clicked.connect(self._save_ai_key)
        self.ai_delete_button.clicked.connect(self._delete_ai_key)
        ai_buttons.addWidget(self.ai_save_button)
        ai_buttons.addWidget(self.ai_delete_button)
        ai_buttons.addStretch()
        ai_form.addRow("", ai_buttons)
        self.ai_status_label = QLabel()
        self.ai_status_label.setWordWrap(True)
        ai_form.addRow("Status", self.ai_status_label)
        layout.addWidget(ai_group)
        layout.addStretch()
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._refresh_status()
        self._refresh_ai_status()

    @staticmethod
    def _compact_form(form: QFormLayout) -> None:
        form.setContentsMargins(10, 8, 10, 10)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

    def _set_token_visible(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.token_input.setEchoMode(mode)

    @staticmethod
    def _set_secret_visible(field: QLineEdit, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        field.setEchoMode(mode)

    def _refresh_status(self) -> None:
        try:
            stored = self._credential_store.has_canvas_token()
        except Exception:
            self.status_label.setText("Credential storage unavailable")
            return
        self.status_label.setText("Credential stored" if stored else "No credential stored")

    def _save_token(self) -> None:
        token = self.token_input.text()
        try:
            self._credential_store.save_canvas_token(token)
        except ValueError:
            self.status_label.setText("Enter a token before saving")
            return
        except Exception:
            self.status_label.setText("Could not save credential")
            return
        finally:
            self.token_input.clear()
            self.show_token.setChecked(False)
        self.status_label.setText("Credential stored")

    def _delete_token(self) -> None:
        self.token_input.clear()
        self.show_token.setChecked(False)
        try:
            self._credential_store.delete_canvas_token()
        except Exception:
            self.status_label.setText("Could not delete credential")
            return
        self.status_label.setText("No credential stored")

    def _connect_canvas(self) -> None:
        token = self._credential_store.get_canvas_token()
        if token is None:
            raise CanvasClientError("No Canvas token is stored.")
        with CanvasClient(token) as client:
            client.get_current_user()

    def _test_connection(self) -> None:
        if is_thread_running(self._connection_thread):
            return
        self.test_button.setEnabled(False)
        self.status_label.setText("Testing Canvas connection…")
        self._connection_thread = QThread(self)
        self._connection_worker = CanvasWorker(self._connect_canvas)
        self._connection_worker.moveToThread(self._connection_thread)
        self._connection_thread.started.connect(self._connection_worker.run)
        self._connection_worker.succeeded.connect(self._connection_succeeded)
        self._connection_worker.failed.connect(self.status_label.setText)
        self._connection_worker.finished.connect(self._connection_thread.quit)
        self._connection_worker.finished.connect(self._connection_worker.deleteLater)
        self._connection_thread.finished.connect(self._connection_thread.deleteLater)
        self._connection_thread.finished.connect(lambda: self.test_button.setEnabled(True))
        self._connection_thread.start()

    def _connection_succeeded(self, _result) -> None:
        self.status_label.setText("Canvas Connected")

    def _set_reminder_controls_enabled(self, enabled: bool) -> None:
        for checkbox in (
            self.reminder_7_days,
            self.reminder_3_days,
            self.reminder_1_day,
            self.reminder_6_hours,
            self.reminder_overdue,
            self.notify_after_submitted,
        ):
            checkbox.setEnabled(enabled)

    def _save_reminder_preferences(self, _checked: bool = False) -> None:
        try:
            self._notification_service.update_preferences(
                enabled=self.reminders_enabled.isChecked(),
                seven_days=self.reminder_7_days.isChecked(),
                three_days=self.reminder_3_days.isChecked(),
                one_day=self.reminder_1_day.isChecked(),
                six_hours=self.reminder_6_hours.isChecked(),
                overdue=self.reminder_overdue.isChecked(),
                notify_after_submitted=self.notify_after_submitted.isChecked(),
            )
        except Exception:
            self.reminder_status_label.setText("Could not save reminder settings.")
            return
        self.reminder_status_label.setText(
            "Reminder settings saved · checks run while the app is open."
        )

    def _test_notification(self) -> None:
        try:
            self._notification_service.test_notification()
        except Exception:
            self.reminder_status_label.setText(
                "Windows notifications are unavailable on this computer."
            )
            return
        self.reminder_status_label.setText("Test notification sent.")

    def _clear_cache(self) -> None:
        try:
            persistent_count = self._cache.clear()
            memory_count = session_cache.clear()
        except Exception:
            self.reminder_status_label.setText("Could not clear the local cache.")
            return
        self.reminder_status_label.setText(
            f"Cache cleared ({persistent_count + memory_count} item(s)). "
            "Reminder history and settings were kept."
        )

    def _refresh_ai_status(self) -> None:
        try:
            stored = self._credential_store.has_ai_api_key()
        except Exception:
            self.ai_status_label.setText("Credential storage unavailable")
            return
        self.ai_status_label.setText(
            "Credential stored · provider connection not configured"
            if stored
            else "No AI credential stored · local read-only answers remain available"
        )

    def _save_ai_key(self) -> None:
        key = self.ai_key_input.text()
        try:
            self._credential_store.save_ai_api_key(key)
        except ValueError:
            self.ai_status_label.setText("Enter an API key before saving")
            return
        except Exception:
            self.ai_status_label.setText("Could not save credential")
            return
        finally:
            self.ai_key_input.clear()
            self.ai_show_key.setChecked(False)
        self.ai_status_label.setText("Credential stored · provider connection not configured")

    def _delete_ai_key(self) -> None:
        self.ai_key_input.clear()
        self.ai_show_key.setChecked(False)
        try:
            self._credential_store.delete_ai_api_key()
        except Exception:
            self.ai_status_label.setText("Could not delete credential")
            return
        self._refresh_ai_status()
