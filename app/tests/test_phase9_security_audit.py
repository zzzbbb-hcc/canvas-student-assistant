from __future__ import annotations

import io
import logging
from pathlib import Path

from app.security.log_filter import CredentialLogFilter
from scripts.security_audit import (
    regression_secrets,
    run_audit,
    scan_packaging_manifest,
    scan_regression_secret_leaks,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_phase9_project_audit_passes():
    assert run_audit(PROJECT_ROOT) == []


def test_phase9_scanner_finds_fake_secret_without_echoing_it(tmp_path):
    secret = regression_secrets()[0]
    artifact = tmp_path / "application.log"
    artifact.write_text(f"unsafe={secret}", encoding="utf-8")

    findings = scan_regression_secret_leaks(tmp_path)

    assert [(finding.path, finding.rule) for finding in findings] == [
        (artifact, "fake-secret-regression")
    ]
    assert all(secret not in finding.rule for finding in findings)


def test_fake_credentials_are_removed_from_logging_output():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CredentialLogFilter())
    logger = logging.Logger("phase9-security-test")
    logger.addHandler(handler)

    for name, secret in zip(("canvas_pat", "authorization", "api_key"), regression_secrets()):
        logger.warning("%s=%s", name, secret)

    output = stream.getvalue()
    assert "[REDACTED]" in output
    assert all(secret not in output for secret in regression_secrets())


def test_frozen_manifest_collects_no_user_data_or_secrets():
    manifest = (PROJECT_ROOT / "StudentAssistant.spec").read_text(encoding="utf-8")
    assert scan_packaging_manifest(PROJECT_ROOT) == []
    assert '"student-assistant-butter-v1.ico"' in manifest
    assert all(secret not in manifest for secret in regression_secrets())
