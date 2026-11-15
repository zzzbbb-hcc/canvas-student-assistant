import logging
from app.security.log_filter import CredentialLogFilter
from app.security.sanitizer import redact_sensitive_values

def test_recursive_redaction():
    assert redact_sensitive_values({"token": "x", "safe": "y"}) == {"token": "[REDACTED]", "safe": "y"}

def test_log_filter_redacts_sensitive_fields():
    record = logging.LogRecord("test", logging.INFO, "", 0, "token=example safe=value", (), None)
    assert CredentialLogFilter().filter(record)
    assert record.getMessage() == "token=[REDACTED] safe=value"

