import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from atlas_cli.logging_config import configure_logging, redact_sensitive


def test_redact_sensitive_removes_credentials_and_authorization_query_values() -> None:
    jwt = "ey" + "JhbGciOiJIUzI1NiJ9." + "a" * 24 + "." + "b" * 24
    token = "temporary-" + "authorization-token-value"
    source = "\n".join(
        [
            f"Token: {jwt}",
            f"x-atlas-cli-auth-token: {token}",
            f'{{"cliAuthToken":"{token}"}}',
            f"https://example.invalid/login?utm=skill&cliAuthToken={token}&redirect=/skill-entry",
        ]
    )

    redacted = redact_sensitive(source)

    assert jwt not in redacted
    assert token not in redacted
    assert "<REDACTED>" in redacted


def test_configure_logging_rotates_and_redacts_formatted_arguments(tmp_path: Path) -> None:
    logger = logging.getLogger("atlas_cli.test.safe_logging")
    logger.handlers.clear()
    token = "temporary-" + "authorization-token-value"

    configured = configure_logging(log_dir=tmp_path, logger=logger)
    configured.warning("Token: %s", token)
    for handler in configured.handlers:
        handler.flush()

    assert len(configured.handlers) == 1
    handler = configured.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 1_048_576
    assert handler.backupCount == 3
    content = (tmp_path / "atlas-flight-booking.log").read_text(encoding="utf-8")
    assert token not in content
    assert "Token: <REDACTED>" in content


def test_configure_logging_falls_back_to_null_handler_when_directory_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    logger = logging.getLogger("atlas_cli.test.unavailable_log_directory")
    logger.handlers.clear()

    def fail_mkdir(*args: object, **kwargs: object) -> None:
        raise OSError("sensitive local path")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    configured = configure_logging(log_dir=tmp_path, logger=logger)

    assert len(configured.handlers) == 1
    assert isinstance(configured.handlers[0], logging.NullHandler)
    configured.warning("Token: should-not-reach-stderr")
