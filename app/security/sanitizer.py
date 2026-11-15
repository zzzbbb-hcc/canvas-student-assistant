"""Helpers for keeping credentials and unnecessary identity data out of AI payloads.

The AI sanitizer is deliberately independent from ``CredentialStore``. It
does not need (and must never be given) access to the operating-system keyring.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from enum import Enum
import re
from typing import Any


SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "token",
        "authorization",
        "cookie",
        "session",
        "password",
        "api_key",
        "secret",
        "x_api_key", "proxy_authorization", "set_cookie", "cookies", "session_id",
        "canvas_pat", "ed_token", "ai_api_key", "id_token", "code_verifier",
    }
)
PERSONAL_KEYS = frozenset({"email", "student_id"})

_INLINE_SECRET = re.compile(
    r"(?i)\b(access[\s_-]*token|refresh[\s_-]*token|token|authorization|cookie|"
    r"session|password|api[\s_-]*key|secret)\b(\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n]+)"
)
_MAX_DEPTH = 32


def _normalise_key(key: object) -> str:
    """Normalise common JSON/header spellings without weakening the blocklist."""

    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().casefold()).strip("_")


def _safe_string(value: str) -> str:
    # A secret placed inside an otherwise innocuous text field (for example a
    # copied HTTP diagnostic) should not bypass the key-based recursion.
    return _INLINE_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def sanitize_for_ai(data: Any, *, remove_personal_data: bool = True) -> Any:
    """Return a JSON-friendly copy safe to place in an AI request.

    Credential-bearing keys are *removed*, recursively and case-insensitively.
    ``email`` and ``student_id`` are also removed by default because they are
    not needed for study assistance. Unknown objects are omitted instead of
    calling an arbitrary ``repr``/``str`` implementation that could expose a
    credential.
    """

    blocked_keys = SENSITIVE_KEYS | (PERSONAL_KEYS if remove_personal_data else frozenset())
    active: set[int] = set()

    def clean(value: Any, depth: int) -> Any:
        if depth > _MAX_DEPTH:
            return "[MAXIMUM DEPTH OMITTED]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return _safe_string(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, Enum):
            return clean(value.value, depth + 1)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "[BINARY DATA OMITTED]"

        converted = value
        if is_dataclass(value) and not isinstance(value, type):
            converted = asdict(value)
        elif hasattr(value, "model_dump") and callable(value.model_dump):
            # Pydantic models are used by Canvas data throughout the app.
            converted = value.model_dump(mode="python")

        if isinstance(converted, Mapping):
            identity = id(converted)
            if identity in active:
                return "[CYCLIC REFERENCE OMITTED]"
            active.add(identity)
            try:
                result: dict[str, Any] = {}
                for key, item in converted.items():
                    if _normalise_key(key) in blocked_keys:
                        continue
                    result[str(key)] = clean(item, depth + 1)
                return result
            finally:
                active.remove(identity)

        if isinstance(converted, (list, tuple, set, frozenset)):
            identity = id(converted)
            if identity in active:
                return "[CYCLIC REFERENCE OMITTED]"
            active.add(identity)
            try:
                return [clean(item, depth + 1) for item in converted]
            finally:
                active.remove(identity)

        return f"[{type(value).__name__} OMITTED]"

    return clean(data, 0)


def redact_sensitive_values(value: Any) -> Any:
    """Redact rather than remove sensitive mapping values for safe diagnostics."""

    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]"
            if _normalise_key(key) in SENSITIVE_KEYS
            else redact_sensitive_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_values(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(redact_sensitive_values(item) for item in value)
    if isinstance(value, str):
        return _safe_string(value)
    return value
