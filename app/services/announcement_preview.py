"""Private, in-memory Chinese announcement titles and extractive previews.

The caller owns scheduling and caching. This service only passes the title and
the model's short, plain-text summary to the existing offline translator; it
does not fetch links, log content, or persist source text or translations.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from app.models.announcement import Announcement
from app.services.offline_translation import OfflineTranslationError, offline_translation


MAX_TITLE_CHARACTERS = 2_000
_LATIN_TEXT = re.compile(r"[A-Za-z\u00c0-\u024f\u1e00-\u1eff]")
_TITLE_FINAL_PUNCTUATION = frozenset(".!?。！？…:：;；")
_EMPTY_TITLE = "未命名公告"
_TRANSLATION_ERROR = "公告中文预览暂不可用，请稍后重试；仍可打开公告查看原文。"


@dataclass(frozen=True, slots=True)
class ChineseAnnouncementPreview:
    title: str
    summary: str


def announcement_preview_key(item: Announcement) -> str:
    """Hash exact preview source content, independent of course/announcement ID.

    Length-prefixing both UTF-8 fields avoids ambiguous field boundaries. Even
    whitespace-only edits invalidate the key, so stale results cannot be shown
    after Canvas edits a title or its extracted summary.
    """

    digest = sha256()
    for source in (item.title, item.summary):
        encoded = source.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _translate_text(source: str, translator, *, is_title: bool = False) -> str:
    source = source.strip()
    if not _LATIN_TEXT.search(source):
        return source
    # The bundled sentence translator can drop deadline/assignment words in
    # unpunctuated headings. A terminal period supplies only a sentence boundary,
    # not invented context. Keep the original title and its cache key unchanged.
    if is_title and source[-1] not in _TITLE_FINAL_PUNCTUATION:
        source += "."
    try:
        result = translator.translate(source)
    except Exception:
        # Backends (including injected ones) can include private source text or
        # filesystem paths in exceptions. Never propagate those to the UI/log.
        raise OfflineTranslationError(_TRANSLATION_ERROR) from None
    if not isinstance(result, str) or not result.strip():
        raise OfflineTranslationError(_TRANSLATION_ERROR)
    return result.strip()


def translate_announcement_preview(
    item: Announcement, translator=offline_translation
) -> ChineseAnnouncementPreview:
    """Translate two short fields separately, off the GUI thread.

    Chinese/non-Latin fields need no inference. Do not silently truncate a long
    title or return the untranslated English under a successful Chinese result
    when translation fails: let the caller show an explicit pending/error view.
    """

    if len(item.title) > MAX_TITLE_CHARACTERS:
        raise OfflineTranslationError("公告标题超过 2,000 字符，暂时无法生成中文预览；仍可打开公告查看原文。")
    title = item.title.strip() or _EMPTY_TITLE
    summary = item.summary
    return ChineseAnnouncementPreview(
        title=_translate_text(title, translator, is_title=True),
        summary=_translate_text(summary, translator),
    )
