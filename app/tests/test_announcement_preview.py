"""Chinese announcement preview service: no real model or network is needed."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.models.announcement import Announcement
from app.services.announcement_preview import (
    MAX_TITLE_CHARACTERS,
    ChineseAnnouncementPreview,
    announcement_preview_key,
    translate_announcement_preview,
)
from app.services.offline_translation import OfflineTranslationError


def notice(*, title="Important reminder", message="<p>Submit the report by Friday 23:59.</p>", course_id=1, announcement_id=2):
    return Announcement(
        course_id=course_id, announcement_id=announcement_id,
        course_name="Course", title=title, message=message,
    )


class Translator:
    def __init__(self):
        self.sources = []

    def translate(self, source):
        self.sources.append(source)
        return {"Important reminder.": "重要提醒", "Submit the report by Friday 23:59.": "请在周五 23:59 前提交报告。"}[source]


def test_title_and_summary_translated_separately_as_plain_text():
    translator = Translator()
    item = notice()
    result = translate_announcement_preview(item, translator)
    assert result == ChineseAnnouncementPreview("重要提醒", "请在周五 23:59 前提交报告。")
    assert translator.sources == [item.title + ".", item.summary]
    assert "<p>" not in "".join(translator.sources)
    assert item.title == "Important reminder"
    assert item.message == "<p>Submit the report by Friday 23:59.</p>"


def test_result_is_an_immutable_value():
    result = ChineseAnnouncementPreview("标题", "摘要")
    with pytest.raises(FrozenInstanceError):
        result.title = "Changed"


@pytest.mark.parametrize("title,message", [
    ("重要提醒", "<p>请在周五 23:59 前提交报告。</p>"),
    ("2026/10/16", "<p>23:59</p>"),
    ("【通知】", "<p>⚠️</p>"),
])
def test_chinese_and_non_latin_fields_skip_the_translator(title, message):
    class NoInference:
        def translate(self, source):
            pytest.fail("Chinese/non-Latin content must not load a model")

    item = notice(title=title, message=message)
    assert translate_announcement_preview(item, NoInference()) == ChineseAnnouncementPreview(title, item.summary)


def test_each_field_independently_skips_inference_when_already_chinese():
    translator = Translator()
    result = translate_announcement_preview(notice(title="重要提醒"), translator)
    assert result.title == "重要提醒"
    assert translator.sources == ["Submit the report by Friday 23:59."]


def test_mixed_language_and_latin_accented_text_is_translated():
    seen = []

    class MixedTranslator:
        def translate(self, source):
            seen.append(source)
            return "中文结果"

    result = translate_announcement_preview(notice(title="重要 Quiz 提醒", message="<p>Échéance</p>"), MixedTranslator())
    assert seen == ["重要 Quiz 提醒.", "Échéance"]
    assert result == ChineseAnnouncementPreview("中文结果", "中文结果")


def test_empty_title_and_body_use_chinese_defaults_without_inference():
    class NoInference:
        def translate(self, source):
            pytest.fail("Empty content should use local Chinese placeholders")

    result = translate_announcement_preview(notice(title=" \r\n ", message=""), NoInference())
    assert result == ChineseAnnouncementPreview("未命名公告", "暂无文字内容，请打开公告查看。")


def test_title_bound_is_explicit_and_does_not_truncate_or_infer():
    translator = Translator()
    with pytest.raises(OfflineTranslationError, match="2,000"):
        translate_announcement_preview(notice(title="x" * (MAX_TITLE_CHARACTERS + 1)), translator)
    assert translator.sources == []


def test_title_at_limit_is_not_truncated():
    seen = []

    class Echo:
        def translate(self, source):
            seen.append(source)
            return "中文"

    translate_announcement_preview(notice(title="x" * MAX_TITLE_CHARACTERS, message=""), Echo())
    assert seen == ["x" * MAX_TITLE_CHARACTERS + "."]


@pytest.mark.parametrize("result", [None, "", " \r\n ", 42, [], {"translation": "中文"}])
def test_invalid_translation_is_not_returned_as_success(result):
    class Invalid:
        def translate(self, source):
            return result

    with pytest.raises(OfflineTranslationError, match="中文预览暂不可用"):
        translate_announcement_preview(notice(), Invalid())


def test_summary_failure_does_not_return_partial_title_or_source(caplog):
    seen = []

    class Failing:
        def translate(self, source):
            seen.append(source)
            if len(seen) == 1:
                return "重要提醒"
            raise RuntimeError("PRIVATE_SOURCE PRIVATE_MODEL_PATH")

    with pytest.raises(OfflineTranslationError) as error:
        translate_announcement_preview(notice(), Failing())
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    assert len(seen) == 2


def test_even_offline_translation_errors_are_sanitized():
    class Failing:
        def translate(self, source):
            raise OfflineTranslationError("PRIVATE_SOURCE_FROM_BACKEND")

    with pytest.raises(OfflineTranslationError) as error:
        translate_announcement_preview(notice(), Failing())
    assert "PRIVATE" not in str(error.value)


def test_translation_whitespace_is_removed_but_original_is_unchanged():
    seen = []

    class Whitespace:
        def translate(self, source):
            seen.append(source)
            return " \n中文\n "

    item = notice(title=" Important reminder ", message="")
    assert translate_announcement_preview(item, Whitespace()).title == "中文"
    assert seen == ["Important reminder."]
    assert item.title == " Important reminder "


def test_content_key_is_stable_private_and_not_tied_to_canvas_identity():
    first = notice()
    other = notice(course_id=20, announcement_id=200)
    key = announcement_preview_key(first)
    assert key == announcement_preview_key(first) == announcement_preview_key(other)
    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")
    assert first.title not in key and first.summary not in key


@pytest.mark.parametrize("changed", [
    {"title": "Important reminder edited"},
    {"title": " Important reminder"},
    {"message": "<p>Submit the report by Monday 23:59.</p>"},
])
def test_key_invalidates_exact_title_or_summary_edits(changed):
    assert announcement_preview_key(notice()) != announcement_preview_key(notice(**changed))


def test_key_has_unambiguous_field_boundaries_and_keeps_exact_whitespace():
    assert announcement_preview_key(SimpleNamespace(title="ab", summary="c")) != announcement_preview_key(SimpleNamespace(title="a", summary="bc"))
    assert announcement_preview_key(SimpleNamespace(title="a", summary=" b")) != announcement_preview_key(SimpleNamespace(title="a", summary="b"))


def test_key_depends_on_summary_not_irrelevant_html_or_other_fields():
    first = notice(message="<p>Submit the report by Friday 23:59.</p>")
    second = notice(message="<div>Submit the report by Friday 23:59.</div>", course_id=88)
    assert first.summary == second.summary
    assert announcement_preview_key(first) == announcement_preview_key(second)


@pytest.mark.parametrize("title", [
    "Assignment 2 deadline reminder",
    "A2 Video Submission (Action Required)",
    "Quiz [updated]",
    'Teacher said "Important reminder"',
])
def test_latin_titles_gain_only_terminal_sentence_boundary(title):
    seen = []

    class Capturing:
        def translate(self, source):
            seen.append(source)
            return "中文"

    item = notice(title=title, message="<p>Submit by Friday</p>")
    key_before = announcement_preview_key(item)
    translate_announcement_preview(item, Capturing())
    assert seen == [title + ".", "Submit by Friday"]
    assert item.title == title
    assert announcement_preview_key(item) == key_before


@pytest.mark.parametrize("punctuation", list(".!?。！？…:：;；"))
def test_existing_latin_title_punctuation_is_not_duplicated(punctuation):
    seen = []

    class Capturing:
        def translate(self, source):
            seen.append(source)
            return "中文"

    title = "Important reminder" + punctuation
    translate_announcement_preview(notice(title=title, message=""), Capturing())
    assert seen == [title]


def test_key_distinguishes_original_title_punctuation_despite_normalized_inference():
    assert announcement_preview_key(notice(title="Important reminder")) != announcement_preview_key(notice(title="Important reminder."))
