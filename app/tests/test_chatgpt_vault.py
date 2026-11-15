"""Protected, bounded credential chunking never publishes partial records."""

from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3

import pytest

from app.services.chatgpt_vault import ChatGPTVault, ChatGPTVaultError
from app.services.credential_store import CredentialStore


class MemoryBackend:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.reads = []
        self.deletes = []
        self.fail_write = None
        self.fail_read = None
        self.fail_delete = False
        self.write_then_fail = False

    def set_password(self, service, account, password):
        self.writes.append((service, account, password))
        if self.fail_write and self.fail_write(account):
            if self.write_then_fail:
                self.values[(service, account)] = password
            raise RuntimeError("provider diagnostic containing SUPER_SECRET_VAULT_VALUE")
        self.values[(service, account)] = password

    def get_password(self, service, account):
        self.reads.append((service, account))
        if self.fail_read and self.fail_read(account):
            raise RuntimeError("provider diagnostic containing SUPER_SECRET_VAULT_VALUE")
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.deletes.append((service, account))
        if self.fail_delete:
            raise RuntimeError("provider diagnostic containing SUPER_SECRET_VAULT_VALUE")
        self.values.pop((service, account), None)


@pytest.fixture
def protected():
    backend = MemoryBackend()
    store = CredentialStore(backend)
    return ChatGPTVault(store), backend, store


def sample_record():
    return {
        "host_id": "urn:uuid:61e55c90-0736-4329-a16a-a10e3863c19d",
        "active_id": "student-a",
        "profiles": {
            "student-a": {
                "client_id": "registered-client-a", "subject": "subject-a",
                "email": "student-a@example.invalid", "access_token": "access-a",
                "refresh_token": "refresh-a", "id_token": "id-a", "expires_at": 1_800_000_000,
                "scopes": ["openid", "email", "profile"],
            },
            "student-b": {
                "client_id": "registered-client-b", "subject": "subject-b",
                "email": "student-b@example.invalid", "access_token": "access-b",
                "refresh_token": "refresh-b", "id_token": "id-b", "expires_at": 1_800_000_100,
                "scopes": ["openid", "email", "profile"],
            },
        },
    }


def manifest(backend, vault):
    return json.loads(backend.values[(CredentialStore.SERVICE_NAME, vault.MANIFEST_ACCOUNT)])


def revision_accounts(backend, vault, revision):
    prefix = f"{vault.PART_PREFIX}{revision}_"
    return [account for service, account in backend.values
            if service == CredentialStore.SERVICE_NAME and account.startswith(prefix)]


def test_first_run_returns_independent_empty_profile_records(protected):
    vault, backend, _store = protected
    empty = vault.load()
    assert empty == {"profiles": {}}
    empty["profiles"]["local-only-mutation"] = {}
    assert vault.load() == {"profiles": {}}
    assert backend.writes == []


def test_round_trip_survives_vault_recreation_without_mutating_input(protected):
    vault, backend, store = protected
    record = sample_record()
    expected = deepcopy(record)
    vault.save(record)
    assert record == expected
    record["profiles"]["student-a"]["access_token"] = "changed-after-save"
    restored = ChatGPTVault(store).load()
    assert restored == expected
    restored["profiles"]["student-b"]["refresh_token"] = "changed-after-load"
    assert vault.load() == expected
    assert {service for service, _account in backend.values} == {CredentialStore.SERVICE_NAME}


def test_two_accounts_remain_isolated_and_signout_preserves_registration(protected):
    vault, _backend, _store = protected
    record = sample_record()
    vault.save(record)
    restored = vault.load()
    for field in ("access_token", "refresh_token", "id_token"):
        restored["profiles"]["student-a"].pop(field)
    restored["active_id"] = "student-b"
    vault.save(restored)
    loaded = vault.load()
    assert loaded["profiles"]["student-b"] == record["profiles"]["student-b"]
    assert loaded["profiles"]["student-a"]["client_id"] == "registered-client-a"
    assert "access_token" not in loaded["profiles"]["student-a"]
    assert loaded["active_id"] == "student-b"


def test_unicode_records_use_bounded_ascii_utf16_safe_chunks(protected):
    vault, backend, _store = protected
    record = sample_record()
    record["profiles"]["student-a"]["subject"] = "学生😀📚𐐷" * 800
    vault.save(record)
    metadata = manifest(backend, vault)
    assert metadata["count"] > 1
    assert set(metadata) == {"revision", "count"}
    assert "student" not in json.dumps(metadata)
    for account in revision_accounts(backend, vault, metadata["revision"]):
        chunk = backend.values[(CredentialStore.SERVICE_NAME, account)]
        assert chunk.isascii()
        assert 0 < len(chunk) <= vault.CHUNK_SIZE
        assert len(chunk.encode("utf-16-le")) <= 2_000
    assert vault.load() == record


def test_successful_commit_retires_old_revision_only_after_pointer_write(protected):
    vault, backend, _store = protected
    vault.save(sample_record())
    old = manifest(backend, vault)
    updated = sample_record()
    updated["profiles"]["student-a"]["access_token"] = "updated-access"
    vault.save(updated)
    new = manifest(backend, vault)
    assert new["revision"] != old["revision"]
    assert revision_accounts(backend, vault, old["revision"]) == []
    assert len(revision_accounts(backend, vault, new["revision"])) == new["count"]
    assert vault.load() == updated
    assert all(account != vault.MANIFEST_ACCOUNT for _service, account in backend.deletes)


def test_partial_new_chunks_are_invisible_to_other_readers(protected):
    vault, backend, store = protected
    old_record = sample_record()
    vault.save(old_record)
    updated = sample_record()
    updated["profiles"]["student-a"]["access_token"] = "new-access" * 800
    original_set = backend.set_password
    observations = []

    def observing_set(service, account, password):
        original_set(service, account, password)
        observations.append((account == vault.MANIFEST_ACCOUNT, ChatGPTVault(store).load()))

    backend.set_password = observing_set
    vault.save(updated)
    assert len(observations) > 2
    assert all(record == old_record for committed, record in observations if not committed)
    assert observations[-1] == (True, updated)


def test_reader_retries_if_another_commit_retires_its_current_revision(protected):
    vault, backend, store = protected
    vault.save(sample_record())
    updated = sample_record()
    updated["active_id"] = "student-b"
    original_get = backend.get_password
    rotated = False

    def rotating_get(service, account):
        nonlocal rotated
        if not rotated and account != vault.MANIFEST_ACCOUNT:
            rotated = True
            ChatGPTVault(store).save(updated)
        return original_get(service, account)

    backend.get_password = rotating_get
    assert vault.load() == updated
    assert rotated


@pytest.mark.parametrize("fail_manifest", [False, True])
def test_failed_write_keeps_previous_record_and_removes_new_parts(protected, fail_manifest):
    vault, backend, _store = protected
    old_record = sample_record()
    vault.save(old_record)
    old_values = deepcopy(backend.values)
    replacement = sample_record()
    replacement["profiles"]["student-a"]["access_token"] = "replacement" * 600
    backend.fail_write = (
        (lambda account: account == vault.MANIFEST_ACCOUNT) if fail_manifest
        else (lambda account: account.startswith(vault.PART_PREFIX) and account.endswith("_1"))
    )
    with pytest.raises(ChatGPTVaultError) as captured:
        vault.save(replacement)
    assert "SUPER_SECRET" not in str(captured.value)
    assert "SUPER_SECRET" not in repr(captured.value)
    assert backend.values == old_values
    assert vault.load() == old_record


def test_failed_first_save_removes_unpublished_parts_and_stays_first_run(protected):
    vault, backend, _store = protected
    backend.fail_write = lambda account: account == vault.MANIFEST_ACCOUNT
    with pytest.raises(ChatGPTVaultError):
        vault.save(sample_record())
    assert backend.values == {}
    assert vault.load() == {"profiles": {}}


def test_part_written_before_failure_is_also_cleaned(protected):
    vault, backend, _store = protected
    vault.save(sample_record())
    original = deepcopy(backend.values)
    backend.fail_write = lambda account: account != vault.MANIFEST_ACCOUNT
    backend.write_then_fail = True
    with pytest.raises(ChatGPTVaultError):
        vault.save(sample_record())
    assert backend.values == original


def test_uncertain_manifest_commit_confirmed_by_readback_is_not_deleted(protected):
    vault, backend, _store = protected
    vault.save(sample_record())
    updated = sample_record()
    updated["active_id"] = "student-b"
    backend.fail_write = lambda account: account == vault.MANIFEST_ACCOUNT
    backend.write_then_fail = True
    vault.save(updated)
    assert vault.load() == updated


def test_old_revision_cleanup_failure_does_not_undo_new_record(protected):
    vault, backend, _store = protected
    vault.save(sample_record())
    updated = sample_record()
    updated["active_id"] = "student-b"
    backend.fail_delete = True
    vault.save(updated)
    assert vault.load() == updated


def test_missing_chunk_is_error_not_an_empty_account_reset(protected):
    vault, backend, _store = protected
    vault.save(sample_record())
    metadata = manifest(backend, vault)
    backend.values.pop((CredentialStore.SERVICE_NAME, vault._part_account(metadata["revision"], 0)))
    with pytest.raises(ChatGPTVaultError, match="could not be read"):
        vault.load()


@pytest.mark.parametrize("raw", [
    "", "not-json SUPER_SECRET_VAULT_VALUE", "[]", "null", "{}",
    json.dumps({"revision": "../canvas_pat", "count": 1}),
    json.dumps({"revision": "a" * 32, "count": True}),
    json.dumps({"revision": "a" * 32, "count": 0}),
    json.dumps({"revision": "a" * 32, "count": -1}),
    json.dumps({"revision": "a" * 32, "count": 257}),
    json.dumps({"revision": "a" * 32, "count": 1.0}),
    json.dumps({"revision": "a" * 32, "count": "1"}),
    json.dumps({"revision": "a" * 32, "count": 1, "extra": "SUPER_SECRET_VAULT_VALUE"}),
    "a" * 513,
])
def test_corrupt_manifest_fails_safely_without_reading_any_chunks(protected, raw):
    vault, backend, _store = protected
    backend.values[(CredentialStore.SERVICE_NAME, vault.MANIFEST_ACCOUNT)] = raw
    with pytest.raises(ChatGPTVaultError) as captured:
        vault.load()
    assert "SUPER_SECRET" not in repr(captured.value)
    assert backend.reads == [(CredentialStore.SERVICE_NAME, vault.MANIFEST_ACCOUNT)]
    assert backend.writes == []
    with pytest.raises(ChatGPTVaultError):
        vault.save(sample_record())
    assert backend.writes == []


@pytest.mark.parametrize("bad_chunk", ["", "x" * 1_001, "学生", "not-json", "[]", "{}", "null"])
def test_corrupt_payload_is_never_returned_as_a_valid_record(protected, bad_chunk):
    vault, backend, _store = protected
    revision = "a" * 32
    backend.values[(CredentialStore.SERVICE_NAME, vault.MANIFEST_ACCOUNT)] = json.dumps(
        {"revision": revision, "count": 1},
    )
    backend.values[(CredentialStore.SERVICE_NAME, vault._part_account(revision, 0))] = bad_chunk
    with pytest.raises(ChatGPTVaultError):
        vault.load()


@pytest.mark.parametrize("record", [
    None, [], {}, {"profiles": []}, {"profiles": {"a": "not-a-profile"}},
    {"profiles": {"": {}}}, {"profiles": {1: {}}},
    {"profiles": {}, "bad": object()}, {"profiles": {}, "bad": b"secret"},
    {"profiles": {}, "bad": float("nan")}, {"profiles": {}, "bad": float("inf")},
    {"profiles": {}, 1: "not-a-JSON-object-key"},
    {"profiles": {str(index): {} for index in range(33)}},
])
def test_invalid_records_do_not_touch_protected_storage(protected, record):
    vault, backend, _store = protected
    with pytest.raises(ChatGPTVaultError):
        vault.save(record)
    assert backend.values == {}
    assert backend.reads == backend.writes == backend.deletes == []


def test_cyclic_or_too_deep_records_fail_without_writes(protected):
    vault, backend, _store = protected
    cycle = {"profiles": {}}
    cycle["self"] = cycle
    with pytest.raises(ChatGPTVaultError):
        vault.save(cycle)
    nested = {"profiles": {}}
    node = nested
    for _index in range(35):
        child = {}
        node["child"] = child
        node = child
    with pytest.raises(ChatGPTVaultError):
        vault.save(nested)
    assert backend.writes == []


def test_record_and_part_count_limits_reject_before_storage(protected):
    vault, backend, _store = protected
    for size in (vault.MAX_RECORD_BYTES + 1, vault.CHUNK_SIZE * vault.MAX_PARTS):
        with pytest.raises(ChatGPTVaultError):
            vault.save({"profiles": {"a": {"access_token": "x" * size}}})
    assert backend.reads == backend.writes == []


def test_read_and_constructor_errors_do_not_expose_backend_details(protected):
    vault, backend, _store = protected
    backend.fail_read = lambda _account: True
    with pytest.raises(ChatGPTVaultError) as captured:
        vault.load()
    assert "SUPER_SECRET" not in repr(captured.value)
    assert "SUPER_SECRET" not in repr(vault)

    class BrokenStore:
        @property
        def _backend(self):
            raise RuntimeError("SUPER_SECRET_VAULT_VALUE")

    with pytest.raises(ChatGPTVaultError) as captured:
        ChatGPTVault(BrokenStore())
    assert "SUPER_SECRET" not in repr(captured.value)


def test_no_file_sqlite_or_network_fallback_even_when_keyring_fails(protected, monkeypatch):
    vault, backend, store = protected

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unprotected persistence is forbidden")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    record = sample_record()
    vault.save(record)
    assert ChatGPTVault(store).load() == record
    backend.fail_write = lambda _account: True
    with pytest.raises(ChatGPTVaultError):
        vault.save(record)
    assert vault.load() == record
