import json
import subprocess
import sys

from typer.testing import CliRunner

from atlas_cli.api_client import ApiClientError
from atlas_cli.cli import app
from atlas_cli.logging_config import configure_logging
from atlas_cli.models import action_required_result, success_result

runner = CliRunner()


class FakeAuthService:
    def login(self):
        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Complete authorization in the browser",
            data={"authorization_url": "https://web.example.invalid/authorize", "expires_at": "2026-08-03 19:00:00"},
        )

    def status(self):
        return success_result(
            "AUTHORIZED",
            "Authorization active",
            data={"authenticated": True, "search_available": True, "ticketing_available": False},
        )

    def poll(self, timeout_seconds: int):
        return success_result(
            "AUTHORIZED",
            "Authorization active",
            data={"authenticated": True, "timeout_used": timeout_seconds},
        )


def test_auth_login_json_writes_exactly_one_stdout_object(monkeypatch) -> None:
    monkeypatch.setattr("atlas_cli.cli.build_auth_service", FakeAuthService)

    result = runner.invoke(app, ["auth", "login", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "AUTHORIZATION_REQUIRED"


def test_auth_status_json_writes_exactly_one_stdout_object(monkeypatch) -> None:
    monkeypatch.setattr("atlas_cli.cli.build_auth_service", FakeAuthService)

    result = runner.invoke(app, ["auth", "status", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["data"]["authenticated"] is True


def test_auth_poll_passes_validated_timeout_and_writes_one_json_object(monkeypatch) -> None:
    monkeypatch.setattr("atlas_cli.cli.build_auth_service", FakeAuthService)

    result = runner.invoke(app, ["auth", "poll", "--timeout", "45", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["data"]["timeout_used"] == 45


def test_auth_poll_invalid_timeout_is_json_and_exit_two(monkeypatch) -> None:
    monkeypatch.setattr("atlas_cli.cli.build_auth_service", FakeAuthService)

    result = runner.invoke(app, ["auth", "poll", "--timeout", "0", "--json"])

    assert result.exit_code == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_ARGUMENT"
    assert payload["status"] == "terminal_error"


def test_real_cli_builder_routes_api_warnings_to_file_not_json_stderr(monkeypatch, tmp_path) -> None:
    class UnavailableApi:
        def __init__(self, settings, *, credential_store=None) -> None:
            del settings, credential_store

        def create_auth_token(self, *, cli_version: str, device_name: str):
            raise ApiClientError(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )

    monkeypatch.setattr("atlas_cli.cli.AtlasApiClient", UnavailableApi)
    monkeypatch.setattr(
        "atlas_cli.cli.configure_logging",
        lambda: configure_logging(log_dir=tmp_path),
    )

    result = runner.invoke(app, ["auth", "login", "--json"])

    assert result.exit_code == 20
    assert result.stderr == ""
    assert json.loads(result.stdout)["code"] == "AUTH_SERVICE_UNAVAILABLE"


def test_hidden_session_refresh_command_emits_only_safe_json(monkeypatch) -> None:
    class FakeSessionService:
        def refresh_session(self):
            return success_result(
                "SESSION_REFRESHED",
                "Authorization session refreshed",
                request_id="req-refresh",
                data={"expire_seconds": 36000},
            )

    monkeypatch.setattr("atlas_cli.cli.build_auth_service", FakeSessionService)

    result = runner.invoke(app, ["session", "refresh", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["code"] == "SESSION_REFRESHED"
    assert payload["data"] == {"expire_seconds": 36000}
    assert "token" not in result.stdout.lower()


def test_json_mode_parse_error_is_one_invalid_argument_object() -> None:
    result = runner.invoke(app, ["auth", "status", "--unsupported", "--json"])

    assert result.exit_code == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_ARGUMENT"
    assert payload["details"] == {"argument": "invalid"}


def test_json_mode_unexpected_exception_is_sanitized_terminal_object(monkeypatch) -> None:
    def fail_builder():
        raise RuntimeError("secret setup details")

    monkeypatch.setattr("atlas_cli.cli.build_auth_service", fail_builder)

    result = runner.invoke(app, ["auth", "status", "--json"])

    assert result.exit_code == 30
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INTERNAL_ERROR"
    assert "secret" not in result.stdout


def test_subprocess_json_parse_error_preserves_stdout_stderr_and_exit_contract() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "atlas_cli", "auth", "status", "--unsupported", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert len(completed.stdout.splitlines()) == 1
    assert json.loads(completed.stdout)["code"] == "INVALID_ARGUMENT"
