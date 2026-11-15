from app.services.credential_store import CredentialStore


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def get_password(self, service, username):
        return self.values.get((service, username))

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


def test_canvas_token_save_get_has_and_delete():
    backend = MemoryKeyring()
    store = CredentialStore(backend)
    store.save_canvas_token("test-value")
    assert store.has_canvas_token()
    assert store.get_canvas_token() == "test-value"
    store.delete_canvas_token()
    assert not store.has_canvas_token()
    assert store.get_canvas_token() is None


def test_canvas_token_cannot_be_empty():
    store = CredentialStore(MemoryKeyring())
    try:
        store.save_canvas_token("  ")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected empty credential to be rejected")
