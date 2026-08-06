"""Rotating local logging with mandatory sensitive-value filtering."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_path

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
AUTHORIZATION_URL_RE = re.compile(r"https?://[^\s]+[?&]cliAuthToken=[^\s]+", re.IGNORECASE)
QUERY_TOKEN_RE = re.compile(r"(?P<prefix>[?&]cliAuthToken=)[^&#\s]+", re.IGNORECASE)
HEADER_RE = re.compile(
    r"(?im)(?P<prefix>^\s*(?:Token|x-atlas-cli-auth-token)\s*[:：]\s*)"
    r"(?!<REDACTED>)[^\s`\"']+"
)
SENSITIVE_JSON_RE = re.compile(
    r'(?P<prefix>"(?:cliAuthToken|token|ak|sk|clientCode|cid)"\s*:\s*")'
    r'(?!<REDACTED>)[^"\r\n]*(?P<suffix>")',
    re.IGNORECASE,
)
INTERNAL_URL_RE = re.compile(r"https?://test1\.atrip(?:-restful)?\.yutu-api\.com[^\s]*", re.IGNORECASE)


def redact_sensitive(value: str) -> str:
    redacted = JWT_RE.sub("<REDACTED>", value)
    redacted = AUTHORIZATION_URL_RE.sub("<REDACTED>", redacted)
    redacted = QUERY_TOKEN_RE.sub(r"\g<prefix><REDACTED>", redacted)
    redacted = HEADER_RE.sub(r"\g<prefix><REDACTED>", redacted)
    redacted = SENSITIVE_JSON_RE.sub(r"\g<prefix><REDACTED>\g<suffix>", redacted)
    return INTERNAL_URL_RE.sub("<REDACTED>", redacted)


class SensitiveValueFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.getMessage())
        record.args = ()
        return True


def configure_logging(*, log_dir: Path | None = None, logger: logging.Logger | None = None) -> logging.Logger:
    target = logger or logging.getLogger("atlas_cli")
    directory = log_dir or Path(user_log_path("atlas-flight-booking"))

    for existing in target.handlers:
        existing.close()
    target.handlers.clear()

    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            directory / "atlas-flight-booking.log",
            maxBytes=1_048_576,
            backupCount=3,
            encoding="utf-8",
        )
        handler.addFilter(SensitiveValueFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    except OSError:
        handler = logging.NullHandler()
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    target.propagate = False
    return target
