"""Offline release security checks.

The scanner intentionally reports only file paths and rule names. It never
prints a discovered credential value. Run it before and after packaging:

    python scripts/security_audit.py
    python scripts/security_audit.py --include-build
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
from typing import Iterable


_SKIP_DIRECTORIES = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".venv",
    ".vscode",
    "__pycache__",
}

_SOURCE_AND_DATA_SUFFIXES = {
    ".bat",
    ".cfg",
    ".db",
    ".db-shm",
    ".db-wal",
    ".dat",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".spec",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

_GENERATED_SUFFIXES = _SOURCE_AND_DATA_SUFFIXES | {
    ".dll",
    ".exe",
    ".pyd",
    ".pyc",
    ".zip",
}


def regression_secrets() -> tuple[str, str, str]:
    """Return the three fake Phase 9 regression credentials.

    Fragments are joined at runtime so the scanner's own source never contains
    a complete regression credential.
    """

    return (
        "_".join(("SUPER", "SECRET", "CANVAS", "TOKEN", "123")),
        "_".join(("SUPER", "SECRET", "ED", "TOKEN", "456")),
        "_".join(("SUPER", "SECRET", "AI", "KEY", "789")),
    )


@dataclass(frozen=True)
class Finding:
    path: Path
    rule: str


def _is_skipped(path: Path, root: Path, include_build: bool) -> bool:
    relative_parts = path.relative_to(root).parts
    if any(part in _SKIP_DIRECTORIES for part in relative_parts):
        return True
    if not include_build and any(part in {"build", "dist"} for part in relative_parts):
        return True
    return False


def iter_audit_files(root: Path, *, include_build: bool = False) -> Iterable[Path]:
    """Yield source/data files and, optionally, frozen build artifacts."""

    allowed_suffixes = _GENERATED_SUFFIXES if include_build else _SOURCE_AND_DATA_SUFFIXES
    for path in root.rglob("*"):
        if not path.is_file() or _is_skipped(path, root, include_build):
            continue
        if path.name == ".env" or path.suffix.lower() in allowed_suffixes:
            yield path


def _contains_any_secret(path: Path, secret_bytes: tuple[bytes, ...]) -> bool:
    """Scan in chunks without decoding or printing sensitive data."""

    overlap = max(map(len, secret_bytes)) - 1
    tail = b""
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                block = tail + chunk
                if any(secret in block for secret in secret_bytes):
                    return True
                tail = block[-overlap:] if overlap else b""
    except OSError:
        return False
    return False


def scan_regression_secret_leaks(root: Path, *, include_build: bool = False) -> list[Finding]:
    encoded = tuple(secret.encode("utf-8") for secret in regression_secrets())
    return [
        Finding(path, "fake-secret-regression")
        for path in iter_audit_files(root, include_build=include_build)
        if _contains_any_secret(path, encoded)
    ]


def default_user_data_root() -> Path | None:
    """Return the exact per-user data folder used by the Windows app."""

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return Path(base) / "StudentAssistant" if base else None


_TLS_BYPASS = re.compile(r"\bverify\s*=\s*False\b")
_CREDENTIAL_IN_URL = re.compile(
    r"https?://[^\s'\"]*[?&](?:access_?token|api_?key|password|session|cookie)=",
    re.IGNORECASE,
)

_PUBLIC_PACKAGE_ASSETS = {
    (("app", "assets", "student-assistant-butter-v1.ico"), "app/assets"),
}


def _project_path_parts(node: ast.AST) -> tuple[str, ...] | None:
    """Recognize a literal PROJECT_ROOT / path, without evaluating spec code."""

    if isinstance(node, ast.Name) and node.id == "PROJECT_ROOT":
        return ()
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
        parent = _project_path_parts(node.left)
        if parent is not None:
            return (*parent, node.right.value)
    return None


def _has_explicit_public_package_data(source: str) -> bool:
    """Allow only the icon file, not asset globs, directories, or private data."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    analyses = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "Analysis"]
    if len(analyses) != 1:
        return False
    data_keywords = [keyword for keyword in analyses[0].keywords if keyword.arg == "datas"]
    if len(data_keywords) != 1 or not isinstance(data_keywords[0].value, ast.List):
        return False
    assets = set()
    for item in data_keywords[0].value.elts:
        if not isinstance(item, ast.Tuple) or len(item.elts) != 2:
            return False
        source_path, destination = item.elts
        if not (isinstance(source_path, ast.Call) and isinstance(source_path.func, ast.Name)
                and source_path.func.id == "str" and len(source_path.args) == 1
                and not source_path.keywords and isinstance(destination, ast.Constant)
                and isinstance(destination.value, str)):
            return False
        asset = (_project_path_parts(source_path.args[0]), destination.value)
        if asset not in _PUBLIC_PACKAGE_ASSETS or asset in assets:
            return False
        assets.add(asset)
    # Do not allow later assignments or mutations to bypass the input allowlist.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "datas" and isinstance(node.ctx, ast.Store):
            return False
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "datas"):
            return False
    return True


def scan_python_network_rules(root: Path) -> list[Finding]:
    """Reject TLS bypasses and literal credential-bearing URLs in app code."""

    findings: list[Finding] = []
    app_root = root / "app"
    if not app_root.exists():
        return findings
    for path in app_root.rglob("*.py"):
        if "tests" in path.relative_to(app_root).parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _TLS_BYPASS.search(text):
            findings.append(Finding(path, "tls-verification-disabled"))
        if _CREDENTIAL_IN_URL.search(text):
            findings.append(Finding(path, "credential-in-literal-url"))
    return findings


def scan_packaging_manifest(root: Path) -> list[Finding]:
    """Ensure the PyInstaller manifest does not collect local/private data."""

    spec = root / "StudentAssistant.spec"
    if not spec.exists():
        return [Finding(spec, "missing-package-manifest")]
    try:
        text = spec.read_text(encoding="utf-8")
    except OSError:
        return [Finding(spec, "unreadable-package-manifest")]

    forbidden = (
        ".env",
        "*.db",
        "*.log",
        "*.sqlite",
        "appdata",
        "localappdata",
    )
    # Comments document exclusions, so inspect only executable spec lines.
    executable_lines = "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ).lower()
    findings: list[Finding] = []
    if not _has_explicit_public_package_data(text):
        findings.append(Finding(spec, "package-data-must-be-explicit-public-assets"))
    for marker in forbidden:
        if marker in executable_lines:
            findings.append(Finding(spec, f"forbidden-package-input:{marker}"))
    return findings


def run_audit(root: Path, *, include_build: bool = False) -> list[Finding]:
    root = root.resolve()
    findings = scan_regression_secret_leaks(root, include_build=include_build)
    findings.extend(scan_python_network_rules(root))
    findings.extend(scan_packaging_manifest(root))
    return sorted(findings, key=lambda item: (str(item.path), item.rule))


def _run_gitleaks(root: Path) -> int | None:
    executable = shutil.which("gitleaks")
    if not executable:
        return None
    result = subprocess.run(
        [executable, "detect", "--no-git", "--source", str(root), "--redact", "--exit-code", "1"],
        check=False,
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Student Assistant release security checks.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project root (defaults to the directory containing this script's parent).",
    )
    parser.add_argument(
        "--include-build",
        action="store_true",
        help="Also scan build/dist binaries and generated files.",
    )
    parser.add_argument(
        "--gitleaks",
        action="store_true",
        help="Run gitleaks too when it is installed.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=default_user_data_root(),
        help="Local app-data directory to scan (defaults to the Windows user data folder).",
    )
    parser.add_argument(
        "--skip-user-data",
        action="store_true",
        help="Do not scan the local app-data directory.",
    )
    args = parser.parse_args(argv)

    findings = run_audit(args.root, include_build=args.include_build)
    if not args.skip_user_data and args.data_root is not None and args.data_root.exists():
        findings.extend(
            scan_regression_secret_leaks(args.data_root.resolve(), include_build=True)
        )
    if findings:
        print("Security audit failed. Potential secret values are not printed.", file=sys.stderr)
        for finding in findings:
            try:
                display_path = finding.path.resolve().relative_to(args.root.resolve())
            except ValueError:
                display_path = finding.path
            print(f"- {display_path}: {finding.rule}", file=sys.stderr)
        return 1

    if args.gitleaks:
        gitleaks_result = _run_gitleaks(args.root.resolve())
        if gitleaks_result is None:
            print("gitleaks: not installed; optional scan skipped.")
        elif gitleaks_result != 0:
            print("gitleaks reported a finding; review its redacted output.", file=sys.stderr)
            return 1
        else:
            print("gitleaks: passed.")

    scope = "source and build artifacts" if args.include_build else "source and local data files"
    print(f"Security audit passed ({scope}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
