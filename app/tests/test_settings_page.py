from app.services.credential_store import CredentialStore
from app.tests.test_credential_store import MemoryKeyring
from app.ui.settings_page import SettingsPage


def test_settings_saves_clears_and_deletes_token(qtbot):
    store = CredentialStore(MemoryKeyring())
    page = SettingsPage(store)
    qtbot.addWidget(page)
    page.token_input.setText("test-value")
    page.save_button.click()
    assert store.get_canvas_token() == "test-value"
    assert page.token_input.text() == ""
    assert page.status_label.text() == "Credential stored"
    page.delete_button.click()
    assert store.get_canvas_token() is None
    assert page.status_label.text() == "No credential stored"


def test_token_visibility_toggle(qtbot):
    page = SettingsPage(CredentialStore(MemoryKeyring()))
    qtbot.addWidget(page)
    assert page.token_input.echoMode().name == "Password"
    page.show_token.setChecked(True)
    assert page.token_input.echoMode().name == "Normal"
