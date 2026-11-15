import builtins
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from app.services import offline_translation as module
from app.services.offline_translation import OfflineTranslationError, OfflineTranslationService


@pytest.fixture(autouse=True)
def clean_runtime(monkeypatch):
    monkeypatch.setattr(module, "_RUNTIME", None)
    monkeypatch.setattr(module, "_CACHE", module.OrderedDict())
    monkeypatch.setattr(module, "_CACHE_CHARACTERS", 0)


@pytest.fixture
def model_dir(tmp_path):
    root = tmp_path / "en_zh"
    (root / "model").mkdir(parents=True)
    (root / "model" / "model.bin").write_bytes(b"test-model")
    (root / "sentencepiece.model").write_bytes(b"test-tokenizer")
    (root / "model" / "config.json").write_bytes(b"{}")
    (root / "model" / "shared_vocabulary.json").write_bytes(b"[]")
    return root


@pytest.fixture
def fake_runtime(monkeypatch):
    state = SimpleNamespace(loads=[], batches=[], options=[], active=0, maximum_active=0,
                            fail=False, truncate=False, dropped=False, decode_fail=False,
                            output=None)

    class Tokenizer:
        def __init__(self, **kwargs):
            state.tokenizer_bytes = kwargs["model_proto"]

        def encode(self, text, out_type):
            assert out_type is str
            return list(text)

        def decode_pieces(self, tokens):
            if state.decode_fail:
                raise RuntimeError("SENSITIVE_DECODE_TEXT")
            return state.output if state.output is not None else "".join(tokens)

    class Translator:
        def __init__(self, model_path, **kwargs):
            state.loads.append((model_path, kwargs))

        def translate_batch(self, batch, **kwargs):
            state.active += 1
            state.maximum_active = max(state.maximum_active, state.active)
            try:
                if state.fail:
                    raise RuntimeError("SENSITIVE_ASSIGNMENT_AND_SECRET")
                state.batches.extend([list(tokens) for tokens in batch])
                state.options.append(kwargs)
                if state.dropped:
                    return []
                return [SimpleNamespace(hypotheses=[list(tokens) + ([] if state.truncate else ["</s>"])])
                        for tokens in batch]
            finally:
                state.active -= 1

    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(Translator=Translator))
    monkeypatch.setitem(sys.modules, "sentencepiece", SimpleNamespace(SentencePieceProcessor=Tokenizer))
    return state


def test_optional_native_dependencies_are_not_imported_for_availability(model_dir, monkeypatch):
    original = builtins.__import__

    def reject_native(name, *args, **kwargs):
        if name in {"ctranslate2", "sentencepiece"}:
            raise AssertionError("Native dependencies must stay lazy")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_native)
    assert OfflineTranslationService(model_dir).is_available()


def test_model_availability_requires_nonempty_assets(model_dir):
    service = OfflineTranslationService(model_dir)
    assert service.is_available()
    (model_dir / "sentencepiece.model").write_bytes(b"")
    assert not service.is_available()


def test_missing_model_has_safe_error(tmp_path):
    with pytest.raises(OfflineTranslationError, match="翻译模型"):
        OfflineTranslationService(tmp_path).translate("Private introduction")


def test_default_model_location_for_source_and_frozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert OfflineTranslationService().model_dir == Path(module.__file__).resolve().parents[2] / "models" / "en_zh"
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert OfflineTranslationService().model_dir == tmp_path / "models" / "en_zh"


def test_frozen_model_prefers_assets_next_to_executable(monkeypatch, tmp_path):
    executable = tmp_path / "installed" / "StudentAssistant.exe"
    adjacent = executable.parent / "models" / "en_zh"
    (adjacent / "model").mkdir(parents=True)
    (adjacent / "model" / "model.bin").write_bytes(b"installed-model")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert OfflineTranslationService().model_dir == adjacent


def test_preserves_paragraphs_and_blank_lines(model_dir, fake_runtime):
    source = "First paragraph\n\nSecond paragraph\r\nThird paragraph\n  \nFourth"
    assert OfflineTranslationService(model_dir).translate(source) == source


def test_empty_text_needs_no_runtime_or_model(tmp_path):
    service = OfflineTranslationService(tmp_path)
    assert service.translate("") == ""
    assert service.translate(" \n\n\r\n ") == " \n\n\r\n "


def test_runtime_is_cpu_int8_and_only_two_threads(model_dir, fake_runtime):
    OfflineTranslationService(model_dir).translate("Assignment introduction")
    path, options = fake_runtime.loads[0]
    assert path == str(model_dir / "model")
    assert options == dict(device="cpu", compute_type="int8", inter_threads=1, intra_threads=2)
    assert fake_runtime.tokenizer_bytes == b"test-tokenizer"
    options = fake_runtime.options[0]
    assert options["max_input_length"] == 0
    assert options["replace_unknowns"] is True
    assert options["return_end_token"] is True


def test_long_sentence_is_not_truncated_or_dropped(model_dir, fake_runtime):
    source = "A" * 1027 + " last detail"
    assert OfflineTranslationService(model_dir).translate(source) == source
    assert "".join("".join(tokens) for tokens in fake_runtime.batches) == source
    assert max(map(len, fake_runtime.batches)) == module.MAX_INPUT_TOKENS
    assert len(fake_runtime.batches) > 8


def test_sentence_boundaries_split_before_token_limit(model_dir, fake_runtime):
    source = "First requirement. Second requirement! Last requirement?"
    OfflineTranslationService(model_dir).translate(source)
    assert ["".join(batch) for batch in fake_runtime.batches] == [
        "First requirement.", "Second requirement!", "Last requirement?"]


def test_translations_and_runtime_are_shared_by_model_with_hashed_keys(model_dir, fake_runtime):
    first = OfflineTranslationService(model_dir)
    second = OfflineTranslationService(model_dir)
    source = "Sensitive plain assignment introduction"
    assert first.translate(source) == source
    calls = len(fake_runtime.options)
    assert second.translate(source) == source
    assert len(fake_runtime.options) == calls
    second.translate("Another introduction")
    assert len(fake_runtime.loads) == 1
    for key in module._CACHE:
        assert source not in str(key)
        assert len(key[1]) == 64


def test_changed_model_invalidates_cached_results(model_dir, fake_runtime):
    service = OfflineTranslationService(model_dir)
    service.translate("Assignment")
    (model_dir / "model" / "model.bin").write_bytes(b"replacement-test-model")
    service.translate("Assignment")
    assert len(fake_runtime.loads) == 2


@pytest.mark.parametrize("asset", ["config.json", "shared_vocabulary.json"])
def test_changed_runtime_metadata_invalidates_cached_results(model_dir, fake_runtime, asset):
    service = OfflineTranslationService(model_dir)
    service.translate("Assignment")
    (model_dir / "model" / asset).write_bytes(b"changed-runtime-asset")
    service.translate("Assignment")
    assert len(fake_runtime.loads) == 2


def test_cache_is_bounded_lru(model_dir, fake_runtime, monkeypatch):
    monkeypatch.setattr(module, "MAX_CACHE_ENTRIES", 2)
    service = OfflineTranslationService(model_dir)
    for text in ("First", "Second", "First", "Third"):
        service.translate(text)
    assert len(module._CACHE) == 2
    calls = len(fake_runtime.options)
    service.translate("First")
    assert len(fake_runtime.options) == calls
    service.translate("Second")
    assert len(fake_runtime.options) == calls + 1


def test_cache_character_budget_is_enforced(model_dir, fake_runtime, monkeypatch):
    monkeypatch.setattr(module, "MAX_CACHE_CHARACTERS", 7)
    service = OfflineTranslationService(model_dir)
    service.translate("First")
    service.translate("Second")
    assert len(module._CACHE) == 1
    assert module._CACHE_CHARACTERS == 6


def test_oversize_text_is_rejected_explicitly_without_inference(model_dir, fake_runtime):
    with pytest.raises(OfflineTranslationError, match="50,000"):
        OfflineTranslationService(model_dir).translate("x" * (module.MAX_TEXT_CHARACTERS + 1))
    assert not fake_runtime.loads


def test_non_text_is_rejected(model_dir):
    with pytest.raises(OfflineTranslationError, match="文字"):
        OfflineTranslationService(model_dir).translate(None)


@pytest.mark.parametrize("failure", ["fail", "decode_fail", "truncate", "dropped"])
def test_backend_failure_never_leaks_source_or_backend_details(model_dir, fake_runtime, failure, caplog):
    setattr(fake_runtime, failure, True)
    with pytest.raises(OfflineTranslationError) as caught:
        OfflineTranslationService(model_dir).translate("PRIVATE_SOURCE_TEXT")
    assert "PRIVATE_SOURCE_TEXT" not in str(caught.value)
    assert "SENSITIVE" not in str(caught.value)
    assert "PRIVATE_SOURCE_TEXT" not in caplog.text
    assert "SENSITIVE" not in caplog.text
    assert not module._CACHE


def test_missing_dependency_has_safe_error(model_dir, monkeypatch):
    original = builtins.__import__

    def reject_native(name, *args, **kwargs):
        if name == "ctranslate2":
            raise ImportError("PRIVATE_IMPORT_DETAILS")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_native)
    with pytest.raises(OfflineTranslationError, match="组件尚未安装") as caught:
        OfflineTranslationService(model_dir).translate("PRIVATE_SOURCE_TEXT")
    assert "PRIVATE" not in str(caught.value)


def test_runtime_is_serialized_across_service_instances(model_dir, fake_runtime):
    services = [OfflineTranslationService(model_dir) for _ in range(4)]
    barrier = threading.Barrier(4)

    def translate(index):
        barrier.wait()
        return services[index].translate(f"Assignment {index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(translate, range(4))) == [f"Assignment {i}" for i in range(4)]
    assert len(fake_runtime.loads) == 1
    assert fake_runtime.maximum_active == 1


def test_output_is_unwrapped_plain_text(model_dir, fake_runtime):
    assert OfflineTranslationService(model_dir).translate("Required: Python and a report") == "Required: Python and a report"


def test_tokenizer_boundary_markers_are_removed_but_variable_names_are_kept(model_dir, fake_runtime):
    assert OfflineTranslationService(model_dir).translate("▁variable_name") == "variable_name"


@pytest.mark.parametrize("phrase", ["Assignment Guide", "assignment guide", "ASSIGNMENT GUIDE"])
def test_academic_guide_terminology_is_narrowly_corrected_and_cached(model_dir, fake_runtime, phrase):
    fake_runtime.output = "请阅读转让指南。"
    service = OfflineTranslationService(model_dir)
    source = f"Please read the {phrase}."
    assert service.translate(source) == "请阅读作业指南。"
    calls = len(fake_runtime.options)
    assert service.translate(source) == "请阅读作业指南。"
    assert len(fake_runtime.options) == calls


@pytest.mark.parametrize("source", ["Please read the transfer guide.", "Reassignment Guide", "Assignment Guidance"])
def test_glossary_does_not_rewrite_other_transfer_meanings(model_dir, fake_runtime, source):
    fake_runtime.output = "请阅读转让指南。"
    assert OfflineTranslationService(model_dir).translate(source) == "请阅读转让指南。"
