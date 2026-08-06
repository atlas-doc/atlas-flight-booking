"""Scan committed text for Atlas credential-shaped values."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
TOKEN_PATH_RE = re.compile(r"(?P<prefix>/cli/auth/token/)(?!<REDACTED_)[^/\s?#]{16,}")
QUERY_TOKEN_RE = re.compile(
    r"(?P<prefix>[?&]cliAuthToken=)"
    r"(?!<REDACTED_|YOUR_CLI_AUTH_TOKEN(?:[&#\s]|$)|\{cli_auth_token\}(?:[&#\s]|$)|"
    r"token_value_is_redacted(?:[&#\s]|$))"
    r"[^&#\s]+",
    re.IGNORECASE,
)

SENSITIVE_JSON_RE = re.compile(
    r'"(?:cliAuthToken|token|ak|sk|clientCode|cid)"\s*:\s*"(?!<REDACTED_)[^"\r\n]+"',
    re.IGNORECASE,
)
SENSITIVE_HEADER_RE = re.compile(
    r"(?im)^\s*(?:--header\s+['\"]?)?(?:Token|x-atlas-cli-auth-token)\s*[:：]\s*"
    r"(?!<REDACTED_)(?P<value>[^\s`\"']{12,})"
)
PASSENGER_PROBE_RE = re.compile(
    "(?:" + "LEAK" + "CHECK/PRIVACY|PRIVACY" + "DOC0001|privacy\\.probe" + "@example\\.invalid|0065-" + "55555555)"
)
SYNTHETIC_PASSENGER_FIXTURES = frozenset(
    {
        "SYNTHETIC/EXAMPLE",
        "SYNTHETICDOC0001",
        "synthetic@example.invalid",
        "maria@example.com",
    }
)
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".superpowers",
    ".venv",
    ".worktrees",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}
EXCLUDED_PATH_SUFFIXES = {
    "tests/unit/test_logging_config.py",
    "tests/unit/test_scan_secrets.py",
}


def scan_text(text: str) -> list[str]:
    """Return stable labels for credential shapes found in text."""
    findings: list[str] = []
    safe_text = text
    for fixture in SYNTHETIC_PASSENGER_FIXTURES:
        safe_text = safe_text.replace(fixture, "<SYNTHETIC_PASSENGER_FIXTURE>")
    jwt_spans = [match.span() for match in JWT_RE.finditer(text)]
    if jwt_spans:
        findings.append("jwt-like token")
    if SENSITIVE_JSON_RE.search(text):
        findings.append("sensitive json value")
    if PASSENGER_PROBE_RE.search(text):
        findings.append("passenger fixture value")

    def overlaps_jwt(span: tuple[int, int]) -> bool:
        return any(start < span[1] and span[0] < end for start, end in jwt_spans)

    if any(not overlaps_jwt(match.span("value")) for match in SENSITIVE_HEADER_RE.finditer(text)):
        findings.append("sensitive header value")
    if EMAIL_RE.search(safe_text):
        findings.append("email address")
    if TOKEN_PATH_RE.search(text):
        findings.append("auth token path segment")
    if QUERY_TOKEN_RE.search(text):
        findings.append("authorization query value")
    return findings


def _iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_file():
            yield path
            continue
        if not path.is_dir():
            continue
        for candidate in sorted(path.rglob("*")):
            candidate_name = candidate.as_posix()
            excluded_source = any(candidate_name.endswith(suffix) for suffix in EXCLUDED_PATH_SUFFIXES)
            if candidate.is_file() and not EXCLUDED_DIRECTORIES.intersection(candidate.parts) and not excluded_source:
                yield candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)

    found = False
    for path in _iter_files(args.paths):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for finding in scan_text(text):
            found = True
            print(f"{path}: {finding}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
