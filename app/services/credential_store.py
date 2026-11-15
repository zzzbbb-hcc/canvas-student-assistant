"""Secure local credential storage.

Only opaque credential values are passed to the operating-system keyring. They
must never be persisted by application storage or included in logs.
"""

from __future__ import annotations

import sys
from typing import Protocol

import keyring
from keyring.errors import PasswordDeleteError


class KeyringBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def get_password(self, service: str, username: str) -> str | None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class CredentialStore:
    """Store credentials in Windows Credential Manager through ``keyring``."""

    SERVICE_NAME = "StudentAssistant"
    CANVAS_ACCOUNT = "canvas_pat"
    AI_ACCOUNT = "ai_api_key"

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._backend = backend or keyring.get_keyring()
        if backend is None and sys.platform == "win32":
            backend_name = type(self._backend).__module__.lower()
            if "windows" not in backend_name:
                raise RuntimeError("Windows Credential Manager is unavailable.")

    def save_canvas_token(self, token: str) -> None:
        if not token or not token.strip():
            raise ValueError("Canvas token cannot be empty.")
        self._backend.set_password(self.SERVICE_NAME, self.CANVAS_ACCOUNT, token)

    def get_canvas_token(self) -> str | None:
        return self._backend.get_password(self.SERVICE_NAME, self.CANVAS_ACCOUNT)

    def delete_canvas_token(self) -> None:
        try:
            self._backend.delete_password(self.SERVICE_NAME, self.CANVAS_ACCOUNT)
        except PasswordDeleteError:
            return

    def has_canvas_token(self) -> bool:
        return self.get_canvas_token() is not None

    def save_ai_api_key(self, key: str) -> None:
        self._save_secret(self.AI_ACCOUNT, key, "AI API key")

    def get_ai_api_key(self) -> str | None:
        return self._backend.get_password(self.SERVICE_NAME, self.AI_ACCOUNT)

    def delete_ai_api_key(self) -> None:
        self._delete_secret(self.AI_ACCOUNT)

    def has_ai_api_key(self) -> bool:
        return self.get_ai_api_key() is not None

    def _save_secret(self, account: str, value: str, label: str) -> None:
        if not value or not value.strip():
            raise ValueError(f"{label} cannot be empty.")
        self._backend.set_password(self.SERVICE_NAME, account, value)

    def _delete_secret(self, account: str) -> None:
        try:
            self._backend.delete_password(self.SERVICE_NAME, account)
        except PasswordDeleteError:
            return
