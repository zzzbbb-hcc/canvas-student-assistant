"""Assignment introduction translation stays local, asynchronous and source-faithful."""

from threading import Event

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication, QDialog, QTextBrowser

from app.models.assignment import Assignment
from app.services.deadline_service import DeadlineService
from app.services.offline_translation import OfflineTranslationError
from app.ui.deadline_widgets import AssignmentDetailDialog


def assignment(description):
    return Assignment(
        assignment_id=72, course_id=14, course_name="Example Course",
        course_code="COMP1000", title="Assignment introduction",
        submission_status="unsubmitted", description=description,
    )


class Translator:
    def __init__(self, translate=None):
        self.calls = []
        self.threads = []
        self._translate = translate or (lambda text: "中文：" + text)

    def translate(self, text):
        self.calls.append(text)
        self.threads.append(QThread.currentThread())
        return self._translate(text)


def make_dialog(qtbot, description, translator, *, store=None):
    dialog = AssignmentDetailDialog(
        DeadlineService().build(assignment(description)),
        credential_store=store, translator=translator,
    )
    qtbot.addWidget(dialog)
    return dialog


def wait_ready(qtbot, dialog):
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=4000)


def test_original_introduction_and_plain_text_chinese_are_both_preserved(qtbot):
    english = "Write a report.\nUse the supplied data.\nSubmit before Friday."
    translated = "撰写报告。\n使用提供的数据。\n<script>这不是 HTML</script> & <b>保持原样</b>"
    translator = Translator(lambda text: translated)
    dialog = make_dialog(
        qtbot,
        "<p>Write a report.</p><p>Use the supplied data.</p><p>Submit before Friday.</p>",
        translator,
    )
    wait_ready(qtbot, dialog)

    assert dialog.description.toPlainText() == english
    assert translator.calls == [english]
    assert dialog.chinese_description.toPlainText() == translated
    assert "<script>" not in dialog.chinese_description.toHtml()
    assert "&lt;script&gt;" in dialog.chinese_description.toHtml()
    assert "离线翻译完成" in dialog.translation_status.text()
    assert dialog.translate_button.isEnabled()
    assert not dialog.chinese_description.openLinks()
    assert not dialog.chinese_description.openExternalLinks()


def test_translation_runs_off_gui_thread_but_rendering_runs_on_gui_thread(qtbot, monkeypatch):
    entered, release = Event(), Event()
    render_threads = []
    original_set_plain_text = QTextBrowser.setPlainText

    def record_set_plain_text(widget, text):
        if widget.objectName() == "assignmentChineseDescription":
            render_threads.append(QThread.currentThread())
        return original_set_plain_text(widget, text)

    def slow_translate(text):
        entered.set()
        release.wait(timeout=4)
        return "中文介绍"

    monkeypatch.setattr(QTextBrowser, "setPlainText", record_set_plain_text)
    translator = Translator(slow_translate)
    dialog = make_dialog(qtbot, "<p>Original introduction</p>", translator)
    dialog.show()
    try:
        qtbot.waitUntil(entered.is_set, timeout=2000)
        assert dialog.description.toPlainText() == "Original introduction"
        assert not dialog.chinese_description.toPlainText()
        assert not dialog.translate_button.isEnabled()
        assert dialog._phase == "translation"
    finally:
        release.set()
        wait_ready(qtbot, dialog)

    assert translator.threads
    assert all(thread is not QApplication.instance().thread() for thread in translator.threads)
    assert render_threads == [QApplication.instance().thread()]
    assert dialog.chinese_description.toPlainText() == "中文介绍"


@pytest.mark.parametrize("description", [None, "", "   ", "<p> </p>", "<script>do not translate</script>"])
def test_missing_introduction_does_not_invent_or_translate_instructions(qtbot, description):
    translator = Translator()
    dialog = make_dialog(qtbot, description, translator)
    wait_ready(qtbot, dialog)

    assert translator.calls == []
    assert not dialog.chinese_description.toPlainText()
    assert not dialog.translate_button.isEnabled()
    assert "未提供可翻译" in dialog.translation_status.text()


def test_translation_failure_keeps_original_and_retry_recovers(qtbot):
    attempts = []

    def first_failure(text):
        attempts.append(text)
        if len(attempts) == 1:
            raise OfflineTranslationError("The model is temporarily unavailable.")
        return "请独立完成这份报告。"

    translator = Translator(first_failure)
    dialog = make_dialog(qtbot, "<p>Complete this report independently.</p>", translator)
    wait_ready(qtbot, dialog)
    assert dialog.description.toPlainText() == "Complete this report independently."
    assert dialog.chinese_description.toPlainText() == ""
    assert "重新翻译" in dialog.translation_status.text()
    assert dialog.translate_button.isEnabled()

    dialog.translate_button.click()
    wait_ready(qtbot, dialog)
    assert attempts == ["Complete this report independently."] * 2
    assert dialog.description.toPlainText() == "Complete this report independently."
    assert dialog.chinese_description.toPlainText() == "请独立完成这份报告。"
    assert "离线翻译完成" in dialog.translation_status.text()


def test_refresh_reuses_unchanged_translation_but_replaces_changed_introduction(qtbot, monkeypatch):
    current = [assignment("<p>Current instructions</p>")]
    monkeypatch.setattr(AssignmentDetailDialog, "_fetch_assignment", lambda self: current[0])
    translator = Translator()
    dialog = make_dialog(qtbot, "<p>Stale cached instructions</p>", translator, store=object())
    wait_ready(qtbot, dialog)
    assert translator.calls == ["Current instructions"]
    assert dialog.description.toPlainText() == "Current instructions"
    assert dialog.chinese_description.toPlainText() == "中文：Current instructions"

    dialog.refresh()
    wait_ready(qtbot, dialog)
    assert translator.calls == ["Current instructions"]
    assert dialog.chinese_description.toPlainText() == "中文：Current instructions"

    current[0] = assignment("<p>Updated instructions</p>")
    dialog.refresh()
    wait_ready(qtbot, dialog)
    assert translator.calls == ["Current instructions", "Updated instructions"]
    assert dialog.description.toPlainText() == "Updated instructions"
    assert dialog.chinese_description.toPlainText() == "中文：Updated instructions"
    assert "Stale" not in dialog.chinese_description.toPlainText()


def test_latest_assignment_and_images_finish_before_translation(qtbot, monkeypatch):
    events = []

    def fetch_assignment(self):
        events.append("assignment")
        return assignment('<p>Read the introduction.</p><img src="https://canvas.sydney.edu.au/files/5/preview">')

    def fetch_images(self):
        events.append("images")
        return []

    def translate(text):
        events.append("translation")
        assert "CANVAS_IMAGE_SLOT" not in text
        assert "https://" not in text
        return "阅读作业介绍。"

    monkeypatch.setattr(AssignmentDetailDialog, "_fetch_assignment", fetch_assignment)
    monkeypatch.setattr(AssignmentDetailDialog, "_fetch_images", fetch_images)
    dialog = make_dialog(qtbot, None, Translator(translate), store=object())
    wait_ready(qtbot, dialog)

    assert events == ["assignment", "images", "translation"]
    assert dialog.chinese_description.toPlainText() == "阅读作业介绍。"
    assert "CANVAS_IMAGE_SLOT" not in dialog.description.toPlainText()
    assert dialog.refresh_button.isEnabled()


def test_attachment_link_is_only_introduction_text_not_attachment_contents(qtbot, monkeypatch):
    description = '<p>Follow the assignment guide <a href="/courses/14/files/42/download">here</a>.</p>'
    monkeypatch.setattr(AssignmentDetailDialog, "_fetch_assignment", lambda self: assignment(description))

    def unexpected_file_open(self, file_id):
        pytest.fail("Translating the introduction must not fetch or open attachments")

    monkeypatch.setattr(AssignmentDetailDialog, "_open_file", unexpected_file_open)
    translator = Translator()
    dialog = make_dialog(qtbot, None, translator, store=object())
    wait_ready(qtbot, dialog)

    assert translator.calls == ["Follow the assignment guide here."]
    assert dialog.file_list.count() == 1
    assert dialog.file_list.item(0).data(Qt.ItemDataRole.UserRole) == 42
    assert 'href="canvas-file:42"' in dialog.description.toHtml()
    assert "中文：Follow the assignment guide here." == dialog.chinese_description.toPlainText()


def test_escape_during_translation_waits_for_worker_before_closing(qtbot):
    entered, release = Event(), Event()

    def slow_translate(text):
        entered.set()
        release.wait(timeout=4)
        return "不应在关闭后更新界面。"

    dialog = make_dialog(qtbot, "<p>Original introduction</p>", Translator(slow_translate))
    dialog.show()
    try:
        qtbot.waitUntil(entered.is_set, timeout=2000)
        thread = dialog._thread
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        assert dialog.isVisible()
        assert dialog._thread is thread
        assert dialog._pending_result == QDialog.DialogCode.Rejected
    finally:
        release.set()
        wait_ready(qtbot, dialog)

    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.chinese_description.toPlainText() == ""
