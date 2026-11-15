"""Private, CPU-only English → Simplified Chinese assignment translation.

The bundled Argos en_zh model is used directly, without Argos' optional
network clients or sentence-boundary-model downloads. Source and translations
exist only in this process; this module never writes them to disk.
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
import re
import sys
from threading import RLock


MAX_TEXT_CHARACTERS = 50_000
MAX_INPUT_TOKENS = 128
MAX_OUTPUT_TOKENS = 512
MAX_OUTPUT_CHARACTERS = 400_000
MAX_CACHE_ENTRIES = 64
MAX_CACHE_CHARACTERS = 2_000_000
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+")
_LINE_BREAK = re.compile(r"(\r\n|\r|\n)")
_ASSIGNMENT_GUIDE = re.compile(r"\bAssignment Guide\b", re.IGNORECASE)

# Serialize loading/inference across all dialogs and service instances. Keep at
# most one native model resident, rather than one copy for each assignment.
_LOCK = RLock()
_RUNTIME = None
_CACHE: OrderedDict[tuple, str] = OrderedDict()
_CACHE_CHARACTERS = 0


class OfflineTranslationError(RuntimeError):
    """A safe user-facing error; never contains backend errors or source text."""


def _default_model_dir() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root or getattr(sys, "frozen", False):
        adjacent = Path(sys.executable).resolve().parent / "models" / "en_zh"
        if (adjacent / "model" / "model.bin").is_file() or not bundle_root:
            return adjacent
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parents[2]
    return root / "models" / "en_zh"


def _model_key(model_dir: Path) -> tuple:
    """Invalidate a cached translation when its on-disk model changes."""
    files = (
        model_dir / "model" / "model.bin", model_dir / "sentencepiece.model",
        model_dir / "model" / "config.json", model_dir / "model" / "shared_vocabulary.json",
    )
    return (str(model_dir), *((p.stat().st_size, p.stat().st_mtime_ns) for p in files))


def _runtime(model_dir: Path, key: tuple):
    global _RUNTIME
    if _RUNTIME is not None and _RUNTIME[0] == key:
        return _RUNTIME[1], _RUNTIME[2]

    # These optional native packages must not affect normal app startup.
    try:
        import ctranslate2
        import sentencepiece
    except (ImportError, OSError):
        raise OfflineTranslationError("离线翻译组件尚未安装，请重新安装完整版本。") from None

    # Release the old model before loading another, bounding native RAM use.
    _RUNTIME = None
    translator = ctranslate2.Translator(
        str(model_dir / "model"),
        device="cpu",
        compute_type="int8",
        inter_threads=1,
        intra_threads=2,
    )
    # SentencePiece's native fopen does not reliably accept non-ASCII Windows
    # paths. Python reads the tiny tokenizer asset with proper Unicode support.
    tokenizer = sentencepiece.SentencePieceProcessor(
        model_proto=(model_dir / "sentencepiece.model").read_bytes()
    )
    _RUNTIME = (key, translator, tokenizer)
    return translator, tokenizer


def _token_chunks(line: str, tokenizer) -> list[list[str]]:
    chunks = []
    # Split natural sentences first, then split any unusually long sentence by
    # token count. Never rely on the runtime's implicit input truncation.
    for sentence in _SENTENCE_BOUNDARY.split(line.strip()):
        if not sentence:
            continue
        tokens = tokenizer.encode(sentence, out_type=str)
        if not tokens or not all(isinstance(token, str) for token in tokens):
            raise OfflineTranslationError("离线翻译无法完整处理这段文字，请稍后重试。")
        chunks.extend(tokens[start:start + MAX_INPUT_TOKENS]
                      for start in range(0, len(tokens), MAX_INPUT_TOKENS))
    return chunks


def _translate_line(line: str, translator, tokenizer) -> str:
    if not line.strip():
        return line
    chunks = _token_chunks(line, tokenizer)
    translated_tokens = []
    for start in range(0, len(chunks), 8):
        batch = chunks[start:start + 8]
        results = translator.translate_batch(
            batch,
            replace_unknowns=True,
            beam_size=4,
            num_hypotheses=1,
            length_penalty=0.2,
            max_batch_size=8,
            batch_type="examples",
            max_input_length=0,
            max_decoding_length=MAX_OUTPUT_TOKENS,
            end_token="</s>",
            return_end_token=True,
        )
        if len(results) != len(batch):
            raise OfflineTranslationError("离线翻译未能完整生成结果，请稍后重试。")
        for result in results:
            hypotheses = result.hypotheses
            tokens = hypotheses[0] if hypotheses else []
            # Explicitly reject an output that hit the decoder limit instead
            # of silently displaying an incomplete translation as complete.
            if not tokens or tokens[-1] != "</s>" or not all(isinstance(t, str) for t in tokens):
                raise OfflineTranslationError("离线翻译未能完整生成结果，请稍后重试。")
            translated_tokens.extend(tokens[:-1])

    # This Argos tokenizer occasionally leaves a SentencePiece word-boundary
    # marker in Chinese output. Keep literal underscores (e.g. variable names).
    result = tokenizer.decode_pieces(translated_tokens).replace("▁", " ").strip()
    # A narrow course terminology correction for this model's ambiguous sense
    # of "assignment". Never rewrite a transfer/finance guide without that
    # exact English academic phrase in the corresponding source line.
    if _ASSIGNMENT_GUIDE.search(line):
        result = result.replace("转让指南", "作业指南")
    if not result or len(result) > MAX_OUTPUT_CHARACTERS:
        raise OfflineTranslationError("离线翻译无法完整处理这段文字，请稍后重试。")
    return result


def _cache_result(key: tuple, translated: str) -> None:
    global _CACHE_CHARACTERS
    _CACHE[key] = translated
    _CACHE_CHARACTERS += len(translated)
    while len(_CACHE) > MAX_CACHE_ENTRIES or _CACHE_CHARACTERS > MAX_CACHE_CHARACTERS:
        _, removed = _CACHE.popitem(last=False)
        _CACHE_CHARACTERS -= len(removed)


class OfflineTranslationService:
    def __init__(self, model_dir: Path | None = None):
        self.model_dir = (Path(model_dir) if model_dir is not None else _default_model_dir()).resolve()

    def is_available(self) -> bool:
        """Whether model assets are installed (does not import native runtimes)."""
        try:
            return all(path.is_file() and path.stat().st_size > 0 for path in (
                self.model_dir / "model" / "model.bin",
                self.model_dir / "sentencepiece.model",
                self.model_dir / "model" / "config.json",
                self.model_dir / "model" / "shared_vocabulary.json",
            ))
        except OSError:
            return False

    def translate(self, text: str) -> str:
        """Return plain text, preserving paragraph/newline boundaries exactly.

        Call from a background worker: first inference loads the local model.
        Errors are deliberately generic so source text or backend paths never
        become part of UI/log messages.
        """
        global _RUNTIME
        if not isinstance(text, str):
            raise OfflineTranslationError("离线翻译只支持作业介绍文字。")
        if len(text) > MAX_TEXT_CHARACTERS:
            raise OfflineTranslationError("作业介绍超过 50,000 字符，暂时无法离线翻译；英文原文仍可查看。")
        if not text.strip():
            return text
        if not self.is_available():
            raise OfflineTranslationError("本机尚未安装中英翻译模型，请安装完整版本后重试。")
        with _LOCK:
            try:
                model_key = _model_key(self.model_dir)
                cache_key = (model_key, sha256(text.encode("utf-8")).hexdigest())
                if cache_key in _CACHE:
                    _CACHE.move_to_end(cache_key)
                    return _CACHE[cache_key]
                translator, tokenizer = _runtime(self.model_dir, model_key)
                translated = "".join(
                    part if _LINE_BREAK.fullmatch(part) else _translate_line(part, translator, tokenizer)
                    for part in _LINE_BREAK.split(text)
                )
                if len(translated) > MAX_OUTPUT_CHARACTERS:
                    raise OfflineTranslationError("离线翻译结果过长，无法完整显示；英文原文仍可查看。")
                _cache_result(cache_key, translated)
                return translated
            except OfflineTranslationError:
                raise
            except Exception:
                _RUNTIME = None
                raise OfflineTranslationError("离线翻译暂时不可用，请重试；英文原文仍可查看。") from None


offline_translation = OfflineTranslationService()
