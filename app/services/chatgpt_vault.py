"""Bounded, protected connection records stored only in the OS keyring.

Windows Credential Manager limits each credential blob, so a complete JSON
record is split into ASCII chunks. A single, stable manifest is the commit
pointer: newly written chunks are invisible until every part has been saved.
This module has no filesystem, SQLite, network, or logging fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from threading import RLock
from uuid import uuid4

from app.services.credential_store import CredentialStore


class ChatGPTVaultError(RuntimeError):
    """A safe error which never includes protected values or backend details."""


_VAULT_LOCK = RLock()
_REVISION = re.compile(r"[0-9a-f]{32}\Z")
_LOAD_ERROR = "Protected connection data could not be read."
_SAVE_ERROR = "Protected connection data could not be saved."


class ChatGPTVault:
    """Protect a JSON connection record using Windows Credential Manager.

    ``CredentialStore`` checks the native backend when constructed normally.
    Tests may inject a credential store with an isolated in-memory backend.
    Tokens are intentionally not kept as attributes of this helper.
    """

    MANIFEST_ACCOUNT = "chatgpt_connection_manifest"
    PART_PREFIX = "chatgpt_connection_"
    CHUNK_SIZE = 1_000
    MAX_PARTS = 256
    MAX_RECORD_BYTES = 1_048_576
    MAX_PROFILES = 32
    MAX_MANIFEST_LENGTH = 512
    MAX_DEPTH = 32
    MAX_NODES = 65_536

    def __init__(self, credential_store=None) -> None:
        try:
            store = credential_store if credential_store is not None else CredentialStore()
            self._backend = store._backend
            self._service_name = store.SERVICE_NAME
            if not isinstance(self._service_name, str) or not self._service_name:
                raise ValueError()
        except Exception:
            raise ChatGPTVaultError(_LOAD_ERROR) from None

    def __repr__(self) -> str:
        return "<ChatGPTVault protected OS credential storage>"

    def load(self) -> dict:
        """Return a complete protected record, or an empty first-run record.

        A missing manifest means no record has been saved. A damaged manifest
        or missing part is an error, never a silent reset of saved accounts.
        """

        try:
            with _VAULT_LOCK:
                # A separate process can commit and retire a revision while
                # this reader is retrieving it. Retry that changed pointer
                # once; never turn an incomplete stable record into {}.
                for attempt in range(2):
                    raw_manifest = self._backend.get_password(
                        self._service_name, self.MANIFEST_ACCOUNT,
                    )
                    if raw_manifest is None:
                        return {"profiles": {}}
                    revision, count = self._parse_manifest(raw_manifest)
                    parts = []
                    incomplete = False
                    for index in range(count):
                        part = self._backend.get_password(
                            self._service_name, self._part_account(revision, index),
                        )
                        if (not isinstance(part, str) or not part or len(part) > self.CHUNK_SIZE
                                or not part.isascii()
                                or (index < count - 1 and len(part) != self.CHUNK_SIZE)):
                            incomplete = True
                            break
                        parts.append(part)
                    if incomplete:
                        latest = self._backend.get_password(
                            self._service_name, self.MANIFEST_ACCOUNT,
                        )
                        if attempt == 0 and latest != raw_manifest:
                            continue
                        raise ValueError()
                    encoded = "".join(parts)
                    if len(encoded) > self.MAX_RECORD_BYTES:
                        raise ValueError()
                    decoded = json.loads(encoded)
                    record = self._validate_record(decoded)
                    return record
                raise ValueError()
        except Exception:
            raise ChatGPTVaultError(_LOAD_ERROR) from None

    def save(self, record: Mapping) -> None:
        """Commit a new record without exposing a partially written revision."""

        try:
            prepared = self._validate_record(record)
            # ASCII JSON makes each 1,000-character part at most 2,000 bytes
            # in the UTF-16 Windows blob, including non-BMP Unicode inputs.
            encoded = json.dumps(
                prepared, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
            )
            if len(encoded) > self.MAX_RECORD_BYTES:
                raise ValueError()
            parts = [encoded[offset:offset + self.CHUNK_SIZE]
                     for offset in range(0, len(encoded), self.CHUNK_SIZE)]
            if not parts or len(parts) > self.MAX_PARTS:
                raise ValueError()

            with _VAULT_LOCK:
                old_raw = self._backend.get_password(self._service_name, self.MANIFEST_ACCOUNT)
                old_manifest = self._parse_manifest(old_raw) if old_raw is not None else None
                revision = uuid4().hex
                manifest = json.dumps({"revision": revision, "count": len(parts)}, separators=(",", ":"))
                attempted_accounts = []
                published = False
                try:
                    for index, part in enumerate(parts):
                        account = self._part_account(revision, index)
                        # Include the current attempt: a backend could write
                        # the blob and then fail before acknowledging it.
                        attempted_accounts.append(account)
                        self._backend.set_password(self._service_name, account, part)
                    try:
                        self._backend.set_password(self._service_name, self.MANIFEST_ACCOUNT, manifest)
                        published = True
                    except Exception:
                        # Never remove chunks which an uncertain write has
                        # already published. Confirmed read-back is success.
                        try:
                            observed = self._backend.get_password(self._service_name, self.MANIFEST_ACCOUNT)
                        except Exception:
                            # Publication cannot be determined. Leaving these
                            # protected parts is safer than corrupting a
                            # possibly committed account record.
                            attempted_accounts = []
                            raise
                        if observed == manifest:
                            published = True
                        else:
                            raise
                except Exception:
                    if not published:
                        self._delete_accounts(attempted_accounts)
                    raise
                if old_manifest is not None:
                    old_revision, old_count = old_manifest
                    self._delete_accounts(
                        self._part_account(old_revision, index) for index in range(old_count)
                    )
        except Exception:
            raise ChatGPTVaultError(_SAVE_ERROR) from None

    @classmethod
    def _part_account(cls, revision: str, index: int) -> str:
        return f"{cls.PART_PREFIX}{revision}_{index}"

    @classmethod
    def _parse_manifest(cls, raw: str) -> tuple[str, int]:
        if not isinstance(raw, str) or not raw or len(raw) > cls.MAX_MANIFEST_LENGTH:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"revision", "count"}:
            raise ValueError()
        revision, count = value["revision"], value["count"]
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ValueError()
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= cls.MAX_PARTS:
            raise ValueError()
        return revision, count

    def _delete_accounts(self, accounts) -> None:
        for account in accounts:
            try:
                self._backend.delete_password(self._service_name, account)
            except Exception:
                # Cleanup cannot undo a successful manifest commit. All
                # remaining blobs stay inside the protected OS keyring.
                pass

    @classmethod
    def _validate_record(cls, value: Mapping) -> dict:
        if not isinstance(value, Mapping):
            raise ValueError()
        profiles = value.get("profiles")
        if not isinstance(profiles, Mapping) or len(profiles) > cls.MAX_PROFILES:
            raise ValueError()
        for identity, profile in profiles.items():
            if not isinstance(identity, str) or not identity or not isinstance(profile, Mapping):
                raise ValueError()
        nodes = 0
        active = set()

        def clone(item, depth):
            nonlocal nodes
            nodes += 1
            if nodes > cls.MAX_NODES or depth > cls.MAX_DEPTH:
                raise ValueError()
            if item is None or isinstance(item, (bool, int)):
                return item
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError()
                return item
            if isinstance(item, str):
                if len(item) > cls.MAX_RECORD_BYTES:
                    raise ValueError()
                return item
            if isinstance(item, (Mapping, list)):
                identity = id(item)
                if identity in active:
                    raise ValueError()
                active.add(identity)
                try:
                    if isinstance(item, Mapping):
                        copied = {}
                        for key, child in item.items():
                            if not isinstance(key, str) or len(key) > cls.MAX_RECORD_BYTES:
                                raise ValueError()
                            copied[key] = clone(child, depth + 1)
                        return copied
                    return [clone(child, depth + 1) for child in item]
                finally:
                    active.remove(identity)
            raise ValueError()

        return clone(value, 0)
