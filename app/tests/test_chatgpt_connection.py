"""Official OAuth contract and safe Responses transport, without a real login.

All remote traffic uses MockTransport and a test-only RSA issuer. The one HTTP
callback test talks only to the app's temporary 127.0.0.1 listener.
"""

from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from threading import Barrier
import time
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from app.services.chatgpt_connection import (
    AUTHORIZE, ISSUER, PLAN_SCOPE, RESOURCE, SCOPE, TOKEN,
    ChatGPTConnection, ChatGPTConnectionError,
)
from app.services import chatgpt_connection as connection_module
from app.storage.database import SQLiteDatabase


class MemoryVault:
    def __init__(self):
        self.record = {"profiles": {}}
        self.saved = []
        self.fail_save = False

    def load(self):
        return deepcopy(self.record)

    def save(self, value):
        if self.fail_save:
            raise RuntimeError("PRIVATE_VAULT_DIAGNOSTIC")
        self.record = deepcopy(value)
        self.saved.append(deepcopy(value))


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def sse(*events, final_blank=True):
    value = "\n\n".join("data: " + json.dumps(event, ensure_ascii=False) for event in events)
    return value + ("\n\n" if final_blank else "")


class MockIssuer:
    def __init__(self, key):
        self.key = key
        self.requests = []
        self.forms = []
        self.nonce = ""
        self.client_id = "oaiapp_test_client"
        self.subject = "test-subject-one"
        self.email = "student@example.invalid"
        self.claim_overrides = {}
        self.remove_claims = set()
        self.token_overrides = {}
        self.token_status = 200
        self.discovery_overrides = {}
        self.models = [
            {"slug": "test-model-b", "display_name": "Model B", "visibility": "list"},
            {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hidden"},
            {"slug": "test-model-a", "display_name": "Model A", "visibility": "list"},
        ]
        self.models_hook = None
        self.stream = sse(
            {"type": "response.output_text.delta", "delta": "你好，"},
            {"type": "response.output_text.delta", "delta": "这是回答。"},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
        self.response_status = 200
        self.response_hook = None
        self.refresh_hook = None
        self.refreshes = 0
        self.revocation_status = 200

    def claims(self):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": self.client_id, "sub": self.subject,
                  "iat": now - 1, "exp": now + 3600, "nonce": self.nonce,
                  "email": self.email}
        claims.update(self.claim_overrides)
        for name in self.remove_claims:
            claims.pop(name, None)
        return claims

    def id_token(self):
        return jwt.encode(self.claims(), self.key, algorithm="RS256", headers={"kid": "test-key"})

    def __call__(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url == ISSUER + "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "jwks_uri": ISSUER + "/test-jwks",
                "revocation_endpoint": ISSUER + "/test-revoke",
                **self.discovery_overrides,
            })
        if url == ISSUER + "/test-jwks":
            key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            key.update(kid="test-key", use="sig", alg="RS256")
            return httpx.Response(200, json={"keys": [key]})
        if url == TOKEN:
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            self.forms.append(form)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "PRIVATE_TOKEN_DIAGNOSTIC"})
            if form["grant_type"] == "refresh_token":
                self.refreshes += 1
                if self.refresh_hook:
                    result = self.refresh_hook(request, form)
                    if result is not None:
                        return result
                return httpx.Response(200, json={
                    "access_token": f"test-rotated-access-{self.refreshes}",
                    "refresh_token": f"test-rotated-refresh-{self.refreshes}",
                    "token_type": "Bearer", "expires_in": 3600,
                    **self.token_overrides,
                })
            return httpx.Response(200, json={
                "id_token": self.id_token(), "access_token": "test-initial-access",
                "refresh_token": "test-initial-refresh", "token_type": "Bearer",
                "expires_in": 3600, "scope": SCOPE, **self.token_overrides,
            })
        if url == RESOURCE + "/models":
            if self.models_hook:
                result = self.models_hook(request)
                if result is not None:
                    return result
            return httpx.Response(200, json={"models": self.models})
        if url == RESOURCE + "/responses":
            if self.response_hook:
                result = self.response_hook(request)
                if result is not None:
                    return result
            return httpx.Response(self.response_status, text=self.stream,
                                  headers={"Content-Type": "text/event-stream"})
        if url == ISSUER + "/test-revoke":
            return httpx.Response(self.revocation_status)
        pytest.fail(f"Unexpected remote endpoint {request.method} {request.url.host}{request.url.path}")


class Harness:
    def __init__(self, key):
        self.database = SQLiteDatabase(":memory:")
        self.database.set_preference("chatgpt.allow_study_data", True)
        self.vault = MemoryVault()
        self.issuer = MockIssuer(key)
        self.clock_value = time.time()
        self.connection = ChatGPTConnection(
            self.database, vault=self.vault, transport=httpx.MockTransport(self.issuer),
            clock=lambda: self.clock_value,
        )
        self.attempts = []

    def start(self, profile_id=None, **kwargs):
        attempt = self.connection.start_sign_in(profile_id, **kwargs)
        self.attempts.append(attempt)
        self.issuer.nonce = attempt.nonce
        return attempt

    def sign_in(self, profile_id=None, **fields):
        attempt = self.start(profile_id)
        attempt.callback = {"state": attempt.state, "code": "test-authorization-code",
                            "client_id": self.issuer.client_id, **fields}
        return self.connection.finish_sign_in(attempt)

    def seed(self, *, profile_id="profile-one", client_id="oaiapp_test_client", subject="test-subject-one",
             expired=False, active=True):
        profile = {"client_id": client_id, "subject": subject, "email": "student@example.invalid",
                   "access_token": "test-initial-access", "refresh_token": "test-initial-refresh",
                   "id_token": "test-id-token", "scopes": SCOPE.split(),
                   "expires_at": self.clock_value - 30 if expired else self.clock_value + 3600}
        self.vault.record["profiles"][profile_id] = profile
        if active:
            self.vault.record["active_id"] = profile_id
        return profile

    def payload(self):
        return {"system": "Read-only study assistant.", "question": "请解释课程资料",
                "history": [{"role": "user", "content": "Previous question"},
                            {"role": "assistant", "content": "Previous answer"}],
                "course_id": 10, "tool_results": [{"source": "Canvas", "course_id": 10}],
                "study_context": [{"source_id": "source-1", "text": "Read-only evidence"}]}


@pytest.fixture
def harness(signing_key):
    value = Harness(signing_key)
    yield value
    for attempt in value.attempts:
        attempt.cancel()
        attempt.server.server_close()
    value.connection._client.close()
    value.database.close()


def test_authorization_uses_fresh_pkce_nonce_state_and_stable_nonsecret_host(harness):
    first, second = harness.start(), harness.start()
    one = parse_qs(urlparse(first.authorization_url).query)
    two = parse_qs(urlparse(second.authorization_url).query)
    assert first.authorization_url.startswith(AUTHORIZE + "?")
    assert one["client_id"] == ["dynamic_agent_client"]
    assert one["agent_name_hint"] == ["Student Assistant"]
    assert one["response_type"] == ["code"]
    assert one["resource"] == [RESOURCE]
    assert set(one["scope"][0].split()) == set(SCOPE.split())
    assert one["ext_agent_host_id"] == two["ext_agent_host_id"]
    assert one["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert one["state"] != two["state"] and one["nonce"] != two["nonce"]
    assert first.verifier != second.verifier
    challenge = urlsafe_b64encode(sha256(first.verifier.encode()).digest()).decode().rstrip("=")
    assert one["code_challenge"] == [challenge]
    assert one["code_challenge_method"] == ["S256"]
    callback = urlparse(one["redirect_uri"][0])
    assert callback.hostname == "127.0.0.1" and callback.path == "/auth/callback"
    assert callback.scheme == "http" and callback.port > 0
    assert harness.issuer.requests == []
    assert harness.connection.active_profile() is None


def test_verified_sign_in_exchanges_original_pkce_and_exposes_metadata_only(harness):
    attempt = harness.start()
    verifier, redirect_uri = attempt.verifier, attempt.redirect_uri
    attempt.callback = {"state": attempt.state, "code": "test-authorization-code",
                        "client_id": harness.issuer.client_id}
    public = harness.connection.finish_sign_in(attempt)
    assert set(public) == {"id", "label", "connected", "sharing"}
    assert public["connected"] and public["sharing"]
    assert public["label"] == "student@example.invalid"
    form = harness.issuer.forms[0]
    assert form == {"grant_type": "authorization_code", "client_id": harness.issuer.client_id,
                    "code": "test-authorization-code", "code_verifier": verifier,
                    "redirect_uri": redirect_uri, "resource": RESOURCE}
    assert harness.connection.profiles() == [public]
    assert harness.connection.active_profile() == public
    assert "test-initial-access" in repr(harness.vault.record)
    assert "test-initial-access" not in repr(public)
    assert "test-initial-refresh" not in repr(harness.connection.profiles())
    database_dump = "\n".join(harness.database._connection.iterdump())
    assert "test-initial-access" not in database_dump
    assert "test-initial-refresh" not in database_dump
    assert harness.issuer.id_token() not in database_dump
    assert attempt.consumed and not attempt.state and not attempt.nonce and not attempt.verifier
    assert attempt.callback is None and attempt.server.fileno() == -1
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.finish_sign_in(attempt)


@pytest.mark.parametrize("fields", [
    {"state": "wrong-state"}, {"code": ""}, {"client_id": ""},
    {"client_id": "dynamic_agent_client"}, {"invalid": True},
    {"error": "access_denied", "error_description": "PRIVATE_OAUTH_DIAGNOSTIC"},
])
def test_invalid_callback_never_exchanges_or_replaces_active_tokens(harness, fields):
    harness.seed()
    before = deepcopy(harness.vault.record)
    attempt = harness.start()
    attempt.callback = {"state": attempt.state, "code": "test-code",
                        "client_id": harness.issuer.client_id, **fields}
    with pytest.raises(ChatGPTConnectionError) as error:
        harness.connection.finish_sign_in(attempt)
    assert "PRIVATE_OAUTH_DIAGNOSTIC" not in str(error.value)
    assert harness.vault.record == before
    assert harness.issuer.forms == []
    assert attempt.server.fileno() == -1
    assert attempt.callback is None and attempt.state == attempt.nonce == attempt.verifier == ""


def test_cancelled_and_expired_attempts_do_not_send_credentials(harness):
    cancelled = harness.start()
    cancelled.cancel()
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.finish_sign_in(cancelled)
    timed_out = harness.start(timeout=1)
    harness.clock_value += 2
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.finish_sign_in(timed_out)
    assert harness.issuer.requests == []
    assert harness.connection.profiles() == []


@pytest.mark.parametrize("overrides,removed", [
    ({"iss": "https://wrong.example.invalid"}, set()),
    ({"aud": "wrong-client"}, set()),
    ({"nonce": "wrong-nonce"}, set()),
    ({"sub": ""}, set()), ({"sub": 42}, set()),
    ({"exp": 1}, set()), ({"iat": int(time.time()) + 3600}, set()),
    ({"azp": "wrong-client"}, set()),
    ({"aud": ["oaiapp_test_client", "another-client"]}, set()),
    ({}, {"iat"}), ({}, {"exp"}), ({}, {"sub"}), ({}, {"nonce"}),
])
def test_invalid_oidc_claims_are_rejected_and_leave_existing_account(harness, overrides, removed):
    harness.seed()
    before = deepcopy(harness.vault.record["profiles"])
    harness.issuer.claim_overrides = overrides
    harness.issuer.remove_claims = removed
    with pytest.raises(ChatGPTConnectionError):
        harness.sign_in()
    assert harness.vault.record["profiles"] == before
    assert harness.vault.record["active_id"] == "profile-one"


@pytest.mark.parametrize("kind", ["unsigned", "wrong-signature", "unknown-kid"])
def test_unsigned_forged_or_unknown_signing_key_is_rejected(harness, signing_key, kind):
    attempt = harness.start()
    claims = harness.issuer.claims()
    if kind == "unsigned":
        token = jwt.encode(claims, key="", algorithm="none")
    elif kind == "unknown-kid":
        token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "unknown"})
    else:
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": "test-key"})
    harness.issuer.token_overrides["id_token"] = token
    attempt.callback = {"state": attempt.state, "code": "test-code",
                        "client_id": harness.issuer.client_id}
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.finish_sign_in(attempt)
    assert harness.connection.profiles() == []


@pytest.mark.parametrize("jwks_uri", [
    "https://evil.example.invalid/jwks", "http://auth.openai.com/jwks",
    "https://auth.openai.com@evil.example.invalid/jwks",
    "https://auth.openai.com/jwks?token=test", "https://auth.openai.com/jwks#fragment",
])
def test_discovery_cannot_redirect_jwks_to_untrusted_origin(harness, jwks_uri):
    harness.issuer.discovery_overrides["jwks_uri"] = jwks_uri
    with pytest.raises(ChatGPTConnectionError):
        harness.sign_in()
    assert all(request.url.host == "auth.openai.com" for request in harness.issuer.requests)
    assert harness.connection.profiles() == []


def test_returning_account_uses_saved_registration_and_rejects_changed_identity(harness):
    public = harness.sign_in()
    original = deepcopy(harness.vault.record)
    attempt = harness.start(public["id"])
    params = parse_qs(urlparse(attempt.authorization_url).query)
    assert params["client_id"] == [harness.issuer.client_id]
    assert "agent_name_hint" not in params
    # Optional hints are deliberately omitted: credentials never enter URLs,
    # so returning users may see the official account selector again.
    assert "id_token_hint" not in params and "login_hint" not in params
    assert original["profiles"][public["id"]]["id_token"] not in attempt.authorization_url
    harness.issuer.subject = "different-person"
    attempt.callback = {"state": attempt.state, "code": "new-test-code"}
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.finish_sign_in(attempt)
    assert harness.vault.record == original


def test_returning_callback_cannot_substitute_client_registration(harness):
    public = harness.sign_in()
    original = deepcopy(harness.vault.record)
    attempt = harness.start(public["id"])
    attempt.callback = {"state": attempt.state, "code": "new-test-code", "client_id": "oaiapp_other"}
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.finish_sign_in(attempt)
    assert harness.vault.record == original
    assert len(harness.issuer.forms) == 1


def test_permission_is_taken_from_token_grant_not_callback_scope(harness):
    harness.issuer.token_overrides["scope"] = "openid profile email"
    public = harness.sign_in(scope=SCOPE)
    assert public["connected"] and not public["sharing"]
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.list_models()
    assert all(request.url.path != "/v1/models" for request in harness.issuer.requests)


def test_same_email_different_registrations_remain_separate(harness):
    first = harness.sign_in()
    harness.issuer.client_id = "oaiapp_second_registration"
    second = harness.sign_in()
    assert first["id"] != second["id"]
    assert len(harness.connection.profiles()) == 2
    assert {profile["label"] for profile in harness.connection.profiles()} == {first["label"]}
    harness.connection.set_active(first["id"])
    assert harness.connection.active_profile()["id"] == first["id"]
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.set_active("missing-profile")
    assert harness.connection.active_profile()["id"] == first["id"]


def test_failed_vault_commit_does_not_activate_new_account_or_expose_diagnostics(harness):
    harness.seed()
    before = deepcopy(harness.vault.record)
    attempt = harness.start()
    attempt.callback = {"state": attempt.state, "code": "new-test-code", "client_id": harness.issuer.client_id}
    harness.vault.fail_save = True
    with pytest.raises(ChatGPTConnectionError) as error:
        harness.connection.finish_sign_in(attempt)
    assert "PRIVATE_VAULT_DIAGNOSTIC" not in str(error.value)
    assert harness.vault.record == before


def test_model_catalog_uses_selected_token_preserves_order_and_hides_nonlisted_models(harness):
    harness.seed()
    models = harness.connection.list_models()
    assert models == [{"slug": "test-model-b", "display_name": "Model B"},
                      {"slug": "test-model-a", "display_name": "Model A"}]
    request = harness.issuer.requests[-1]
    assert str(request.url) == RESOURCE + "/models"
    assert request.headers["authorization"] == "Bearer test-initial-access"
    assert request.url.query == b""


@pytest.mark.parametrize("consent", [None, False, "true", 1, True])
def test_readiness_requires_explicit_boolean_consent_and_does_not_call_api(harness, consent):
    harness.seed()
    harness.database.set_preference("chatgpt.allow_study_data", consent)
    assert harness.connection.is_ready is (consent is True)
    assert harness.issuer.requests == []
    if consent is not True:
        with pytest.raises(ChatGPTConnectionError):
            harness.connection.complete(harness.payload())
        assert harness.issuer.requests == []


def test_default_consent_is_off_and_vault_failure_is_not_readiness(harness, monkeypatch):
    harness.seed()
    with harness.database._transaction() as connection:
        connection.execute("DELETE FROM preferences WHERE preference_key = ?", ("chatgpt.allow_study_data",))
    assert harness.connection.is_ready is False
    harness.database.set_preference("chatgpt.allow_study_data", True)

    def unavailable():
        raise RuntimeError("PRIVATE_VAULT_DIAGNOSTIC")

    monkeypatch.setattr(harness.vault, "load", unavailable)
    assert harness.connection.is_ready is False
    with pytest.raises(ChatGPTConnectionError) as error:
        harness.connection.complete(harness.payload())
    assert "PRIVATE_VAULT_DIAGNOSTIC" not in str(error.value)
    assert harness.issuer.requests == []


def test_401_cannot_resend_an_existing_question_with_another_account_token(harness):
    harness.seed()
    other = harness.seed(profile_id="profile-two", client_id="oaiapp_other", subject="other", active=False)
    other["access_token"] = "test-other-account-access"
    response_requests = []

    def switch_during_first_request(request):
        response_requests.append(request)
        harness.connection.set_active("profile-two")
        return httpx.Response(401)

    harness.issuer.response_hook = switch_during_first_request
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.complete(harness.payload())
    assert len(response_requests) == 1
    assert response_requests[0].headers["authorization"] == "Bearer test-initial-access"
    assert harness.issuer.refreshes == 0
    assert all(request.headers.get("authorization") != "Bearer test-other-account-access"
               for request in harness.issuer.requests)


def test_switched_account_cannot_publish_an_old_accounts_model_catalog(harness):
    harness.seed()
    harness.seed(profile_id="profile-two", client_id="oaiapp_other", subject="other", active=False)
    harness.issuer.models_hook = lambda _request: harness.connection.set_active("profile-two")
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.list_models()
    assert harness.connection.active_profile()["id"] == "profile-two"
    assert harness.connection._models == []
    assert harness.connection._models_profile_id is None


def test_model_results_do_not_expose_mutable_cached_model_records(harness):
    harness.seed()
    models = harness.connection.list_models()
    models[0]["slug"] = "changed-by-caller"
    assert harness.connection._models[0]["slug"] == "test-model-b"


def test_final_sse_event_without_blank_line_is_consumed(harness):
    harness.seed()
    harness.issuer.stream = sse(
        {"type": "response.output_text.delta", "delta": "最后一帧"},
        {"type": "response.completed", "response": {"status": "completed"}},
        final_blank=False,
    )
    assert harness.connection.complete(harness.payload()) == "最后一帧"


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
def test_completed_event_cannot_hide_a_noncompleted_response_status(harness, status):
    harness.seed()
    harness.issuer.stream = sse({"type": "response.output_text.delta", "delta": "partial"},
                              {"type": "response.completed", "response": {"status": status}})
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.complete(harness.payload())


@pytest.mark.parametrize("tokens", [
    {"token_type": "MAC"}, {"expires_in": -1}, {"expires_in": float("nan")},
    {"refresh_token": 123}, {"access_token": ""},
])
def test_invalid_refresh_credentials_do_not_replace_existing_session(harness, tokens):
    harness.seed(expired=True)
    before = deepcopy(harness.vault.record)
    harness.issuer.token_overrides = tokens
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.list_models()
    assert harness.vault.record == before


def test_refresh_revalidates_new_id_token_and_does_not_accept_another_subject(harness):
    harness.seed(expired=True)
    before = deepcopy(harness.vault.record)
    harness.issuer.subject = "different-subject"
    harness.issuer.token_overrides["id_token"] = harness.issuer.id_token()
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.list_models()
    assert harness.vault.record == before


def test_valid_refresh_updates_retained_id_token_together_with_rotation(harness):
    harness.seed(expired=True)
    new_id_token = harness.issuer.id_token()
    harness.issuer.token_overrides["id_token"] = new_id_token
    harness.connection.list_models()
    profile = harness.vault.record["profiles"]["profile-one"]
    assert profile["id_token"] == new_id_token
    assert profile["refresh_token"] == "test-rotated-refresh-1"


def test_refresh_vault_failure_never_uses_uncommitted_replacement_token(harness):
    harness.seed(expired=True)
    before = deepcopy(harness.vault.record)
    harness.vault.fail_save = True
    with pytest.raises(ChatGPTConnectionError) as error:
        harness.connection.list_models()
    assert "PRIVATE_VAULT_DIAGNOSTIC" not in str(error.value)
    assert harness.vault.record == before
    assert all(request.url.path != "/v1/models" for request in harness.issuer.requests)


@pytest.mark.skipif(os.name != "nt", reason="Packaged runtime uses the native Windows vault mutex")
def test_independent_connections_do_not_race_a_rotating_session(harness):
    harness.seed(expired=True)
    second = ChatGPTConnection(harness.database, vault=harness.vault,
                              transport=httpx.MockTransport(harness.issuer),
                              clock=lambda: harness.clock_value)
    gate = Barrier(2)
    harness.issuer.refresh_hook = lambda *_args: time.sleep(0.03)

    def access(connection):
        gate.wait(timeout=3)
        return connection._access()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(access, [harness.connection, second]))
        assert results == ["test-rotated-access-1"] * 2
        assert harness.issuer.refreshes == 1
    finally:
        second._client.close()


@pytest.mark.skipif(os.name != "nt", reason="Packaged runtime uses the native Windows vault mutex")
def test_independent_connection_selection_does_not_erase_another_connections_rotation(harness):
    harness.seed(expired=True)
    harness.seed(profile_id="profile-two", client_id="oaiapp_other", subject="other", active=False)
    second = ChatGPTConnection(harness.database, vault=harness.vault,
                              transport=httpx.MockTransport(harness.issuer),
                              clock=lambda: harness.clock_value)
    gate = Barrier(2)
    harness.issuer.refresh_hook = lambda *_args: time.sleep(0.03)

    def refresh():
        gate.wait(timeout=3)
        try:
            return harness.connection._access()
        except ChatGPTConnectionError:
            # The other window may select first. Aborting is safe; combining
            # its identity with this profile's token would not be.
            return "account-switched"

    def switch():
        gate.wait(timeout=3)
        second.set_active("profile-two")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            renewed = executor.submit(refresh)
            selected = executor.submit(switch)
            result, _ = renewed.result(timeout=5), selected.result(timeout=5)
        assert harness.vault.record["active_id"] == "profile-two"
        assert set(harness.vault.record["profiles"]) == {"profile-one", "profile-two"}
        if result != "account-switched":
            assert harness.vault.record["profiles"]["profile-one"]["refresh_token"] == "test-rotated-refresh-1"
    finally:
        second._client.close()


class FakeCallbackSocket:
    """Run the actual HTTP handler without needing sandbox loopback access."""
    def __init__(self, request):
        self.request = request
        self.response = bytearray()

    def makefile(self, *_args, **_kwargs):
        return BytesIO(self.request)

    def sendall(self, data):
        self.response.extend(data)


@pytest.mark.parametrize("kind", ["duplicate-state", "too-many-fields", "wrong-path", "wrong-host"])
def test_actual_callback_handler_rejects_ambiguous_or_unbounded_requests(harness, kind, capsys):
    attempt = harness.start()
    query = urlencode({"state": attempt.state, "code": "private-test-code", "client_id": harness.issuer.client_id})
    path, host = "/auth/callback", urlparse(attempt.redirect_uri).netloc
    if kind == "duplicate-state":
        query += "&state=second-state"
    elif kind == "too-many-fields":
        query += "&" + urlencode({f"field{index}": "value" for index in range(20)})
    elif kind == "wrong-path":
        path = "/callback"
    else:
        host = "evil.example.invalid"
    socket = FakeCallbackSocket(f"GET {path}?{query} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
    attempt.server.RequestHandlerClass(socket, ("127.0.0.1", 1234), attempt.server)
    assert attempt.callback in (None, {"invalid": True})
    assert b"private-test-code" not in socket.response
    assert "Traceback" not in capsys.readouterr().err
    if attempt.callback is None:
        attempt.cancel()
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.finish_sign_in(attempt)
    assert harness.issuer.forms == []


def test_accepted_callback_socket_has_an_idle_timeout(harness, monkeypatch):
    class AcceptedSocket:
        timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

    accepted = AcceptedSocket()
    attempt = harness.start()
    monkeypatch.setattr(connection_module.HTTPServer, "get_request", lambda _server: (accepted, ("127.0.0.1", 1234)))
    request, _address = attempt.server.get_request()
    assert request is accepted and accepted.timeout == 1.0


def test_absolute_callback_watchdog_bounds_a_trickling_request(harness, monkeypatch, capsys):
    from threading import Event

    ended = Event()

    class TrickleSocket:
        def shutdown(self, _how):
            ended.set()

    attempt = harness.start()
    monkeypatch.setattr(connection_module.HTTPServer, "process_request", lambda *_args: ended.wait(3))
    attempt.server.process_request(TrickleSocket(), ("127.0.0.1", 1234))
    assert ended.is_set()
    attempt.server.handle_error(None, None)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("stream", [
    sse({"type": "response.output_text.delta", "delta": "x" * 120_001}, {"type": "response.completed"}),
    ("data: " + "x" * 20 + "\n") * 50_001,
], ids=["output-limit", "aggregate-event-limit"])
def test_stream_bounds_cover_both_text_and_many_small_event_lines(harness, stream):
    harness.seed()
    harness.issuer.stream = stream
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.complete(harness.payload())


def test_failed_sign_out_commit_still_stops_requests_from_this_process(harness):
    harness.seed()
    harness.vault.fail_save = True
    with pytest.raises(ChatGPTConnectionError) as error:
        harness.connection.sign_out()
    assert "PRIVATE_VAULT_DIAGNOSTIC" not in str(error.value)
    assert harness.connection.is_ready is False
    assert harness.connection.active_profile()["connected"] is False
    count = len(harness.issuer.requests)
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.complete(harness.payload())
    assert len(harness.issuer.requests) == count


def test_responses_uses_public_streaming_nonstored_request_with_current_history(harness):
    harness.seed()
    harness.database.set_preference("chatgpt.model", "test-model-a")
    assert harness.connection.complete(harness.payload()) == "你好，这是回答。"
    request = harness.issuer.requests[-1]
    assert str(request.url) == RESOURCE + "/responses"
    assert request.headers["authorization"] == "Bearer test-initial-access"
    body = json.loads(request.content)
    assert body["model"] == "test-model-a"
    assert body["store"] is False and body["stream"] is True
    assert [item["role"] for item in body["input"]] == ["system", "user", "assistant", "user"]
    assert body["input"][1]["content"] == "Previous question"
    assert body["input"][2]["content"] == "Previous answer"
    assert "Read-only evidence" in body["input"][-1]["content"]
    assert "test-initial-access" not in request.content.decode()


@pytest.mark.parametrize("events", [
    [{"type": "response.output_text.delta", "delta": "partial"}],
    [{"type": "response.output_text.delta", "delta": "partial"}, {"type": "response.failed"}],
    [{"type": "response.output_text.delta", "delta": "partial"}, {"type": "response.incomplete"}],
    [{"type": "response.output_text.delta", "delta": "partial"}, {"type": "error", "message": "PRIVATE_STREAM_DIAGNOSTIC"}],
    [{"type": "response.completed"}],
])
def test_partial_failed_incomplete_or_empty_streams_are_never_successful(harness, events):
    harness.seed()
    harness.issuer.stream = sse(*events)
    with pytest.raises(ChatGPTConnectionError) as error:
        harness.connection.complete(harness.payload())
    assert "partial" not in str(error.value)
    assert "PRIVATE_STREAM_DIAGNOSTIC" not in str(error.value)


def test_refresh_rotation_is_atomic_and_reuses_issued_client_and_resource(harness):
    harness.seed(expired=True)
    harness.connection.list_models()
    assert harness.issuer.refreshes == 1
    form = harness.issuer.forms[-1]
    assert form == {"grant_type": "refresh_token", "client_id": "oaiapp_test_client",
                    "refresh_token": "test-initial-refresh", "resource": RESOURCE}
    record = harness.vault.record["profiles"]["profile-one"]
    assert record["access_token"] == "test-rotated-access-1"
    assert record["refresh_token"] == "test-rotated-refresh-1"
    assert record["expires_at"] == harness.clock_value + 3600
    assert harness.issuer.requests[-1].headers["authorization"] == "Bearer test-rotated-access-1"


def test_parallel_expiry_checks_refresh_only_once(harness):
    harness.seed(expired=True)
    gate = Barrier(4)

    def get_access():
        gate.wait(timeout=3)
        return harness.connection._access()

    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda _index: get_access(), range(4)))
    assert values == ["test-rotated-access-1"] * 4
    assert harness.issuer.refreshes == 1


def test_401_is_retried_only_once_with_refreshed_credentials(harness):
    harness.seed()
    response_calls = []

    def unauthorized_once(request):
        response_calls.append(request)
        return httpx.Response(401) if len(response_calls) == 1 else None

    harness.issuer.response_hook = unauthorized_once
    assert harness.connection.complete(harness.payload()) == "你好，这是回答。"
    assert len(response_calls) == 2 and harness.issuer.refreshes == 1
    assert response_calls[0].headers["authorization"] == "Bearer test-initial-access"
    assert response_calls[1].headers["authorization"] == "Bearer test-rotated-access-1"
    response_calls.clear()
    harness.issuer.response_hook = lambda request: response_calls.append(request) or httpx.Response(401)
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.complete(harness.payload())
    assert len(response_calls) == 2


def test_refresh_scope_loss_does_not_overwrite_previous_session(harness):
    harness.seed(expired=True)
    before = deepcopy(harness.vault.record)
    harness.issuer.token_overrides["scope"] = "openid profile email"
    with pytest.raises(ChatGPTConnectionError):
        harness.connection.list_models()
    assert harness.vault.record == before


@pytest.mark.parametrize("revocation_status", [200, 503])
def test_sign_out_clears_only_selected_tokens_and_retains_registration(harness, revocation_status):
    harness.seed()
    harness.seed(profile_id="profile-two", client_id="oaiapp_other", subject="other", active=False)
    other = deepcopy(harness.vault.record["profiles"]["profile-two"])
    harness.issuer.revocation_status = revocation_status
    assert harness.connection.sign_out() is (revocation_status == 200)
    first = harness.vault.record["profiles"]["profile-one"]
    assert first["subject"] == "test-subject-one" and first["client_id"] == "oaiapp_test_client"
    assert not {"access_token", "refresh_token", "id_token", "expires_at", "scopes"}.intersection(first)
    assert harness.vault.record["profiles"]["profile-two"] == other
    assert harness.connection.active_profile()["connected"] is False
    request = harness.issuer.requests[-1]
    assert str(request.url) == ISSUER + "/test-revoke"
    form = parse_qs(request.content.decode())
    assert form == {"token": ["test-initial-refresh"], "token_type_hint": ["refresh_token"],
                    "client_id": ["oaiapp_test_client"]}


def test_sign_out_discovery_cannot_send_refresh_token_to_arbitrary_url(harness):
    harness.seed()
    harness.issuer.discovery_overrides["revocation_endpoint"] = "https://evil.example.invalid/revoke"
    assert harness.connection.sign_out() is False
    assert len(harness.issuer.requests) == 1
    assert harness.connection.active_profile()["connected"] is False


def test_real_loopback_callback_is_bound_to_exact_path_host_and_one_attempt(harness):
    attempt = harness.start(timeout=5)
    query = urlencode({"state": attempt.state, "code": "loopback-test-code", "client_id": harness.issuer.client_id})
    with ThreadPoolExecutor(max_workers=1) as executor:
        finished = executor.submit(harness.connection.finish_sign_in, attempt)
        try:
            with httpx.Client(timeout=2, trust_env=False) as callback_client:
                try:
                    invalid = callback_client.get(attempt.redirect_uri.replace("/auth/callback", "/callback") + "?" + query)
                except httpx.ConnectError as error:
                    if "10013" in str(error):
                        pytest.skip("This Windows sandbox blocks loopback socket connections")
                    raise
                assert invalid.status_code == 400
                invalid_host = callback_client.get(attempt.redirect_uri + "?" + query, headers={"Host": "evil.example.invalid"})
                assert invalid_host.status_code == 400
                response = callback_client.get(attempt.redirect_uri + "?" + query)
            public = finished.result(timeout=3)
        finally:
            attempt.cancel()
    assert response.status_code == 200 and public["connected"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert "loopback-test-code" not in response.text
    assert attempt.server.fileno() == -1
