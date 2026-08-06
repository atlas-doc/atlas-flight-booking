import pytest

from atlas_cli.api_client import ApiClientError
from atlas_cli.api_models import ServerVersion
from atlas_cli.doctor import DoctorService
from atlas_cli.models import (
    CommandResult,
    CommandStatus,
    ExitCode,
    action_required_result,
    exit_code_for,
    success_result,
    terminal_error_result,
)

EXPECTED_CHECKS = {
    "cli_version": True,
    "config_directory": True,
    "secure_store": True,
    "api_reachable": True,
    "authenticated": True,
}


class Probe:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls = 0

    def probe(self) -> bool:
        self.calls += 1
        return self.result


class DoctorApi:
    def __init__(self, outcome: ServerVersion | ApiClientError) -> None:
        self.outcome = outcome
        self.version_calls = 0

    def get_server_version(self) -> ServerVersion:
        self.version_calls += 1
        if isinstance(self.outcome, ApiClientError):
            raise self.outcome
        return self.outcome


class DoctorAuth:
    def __init__(self, authenticated: bool, outcome: CommandResult | None = None) -> None:
        self.authenticated = authenticated
        self.outcome = outcome
        self.calls = 0

    def status(self):
        self.calls += 1
        if self.outcome is not None:
            return self.outcome
        if self.authenticated:
            return success_result("AUTHORIZED", "Authorization active", data={"authenticated": True})
        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Authorization required",
            data={"authenticated": False},
        )


def make_service(
    *,
    config_ok: bool = True,
    secure_ok: bool = True,
    api_outcome: ServerVersion | ApiClientError | None = None,
    authenticated: bool = True,
    auth_outcome: CommandResult | None = None,
) -> tuple[DoctorService, Probe, Probe, DoctorApi, DoctorAuth]:
    config = Probe(config_ok)
    secrets = Probe(secure_ok)
    api = DoctorApi(api_outcome or ServerVersion(version="1.0.0", request_id="req-version"))
    auth = DoctorAuth(authenticated, auth_outcome)
    service = DoctorService(
        config=config,
        secrets=secrets,
        api=api,
        auth=auth,
        cli_version="0.1.0",
    )
    return service, config, secrets, api, auth


def test_all_checks_true_returns_doctor_ok_and_runs_every_check() -> None:
    service, config, secrets, api, auth = make_service()

    result = service.run()

    assert result.status is CommandStatus.SUCCESS
    assert result.code == "DOCTOR_OK"
    assert result.data == {"checks": EXPECTED_CHECKS}
    assert (config.calls, secrets.calls, api.version_calls, auth.calls) == (1, 1, 1, 1)


def test_only_unauthenticated_returns_action_required_exit_zero() -> None:
    service, *_ = make_service(authenticated=False)

    result = service.run()

    expected = dict(EXPECTED_CHECKS, authenticated=False)
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.code == "DOCTOR_ISSUES"
    assert result.data == {"checks": expected}
    assert exit_code_for(result) is ExitCode.OK


def test_api_unavailable_returns_retryable_error_with_complete_checks() -> None:
    error = ApiClientError(
        code="SERVICE_TEMPORARILY_UNAVAILABLE",
        message="raw internal failure should not escape",
        retryable=True,
        request_id="req-api",
    )
    service, config, secrets, api, auth = make_service(api_outcome=error)

    result = service.run()

    expected = dict(EXPECTED_CHECKS, cli_version=False, api_reachable=False)
    assert result.status is CommandStatus.RETRYABLE_ERROR
    assert result.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert result.message == "Service temporarily unavailable"
    assert result.data == {"checks": expected}
    assert exit_code_for(result) is ExitCode.RETRYABLE
    assert (config.calls, secrets.calls, api.version_calls, auth.calls) == (1, 1, 1, 1)


def test_keyring_probe_failure_is_terminal_but_other_checks_still_run() -> None:
    service, config, secrets, api, auth = make_service(secure_ok=False)

    result = service.run()

    expected = dict(EXPECTED_CHECKS, secure_store=False)
    assert result.status is CommandStatus.TERMINAL_ERROR
    assert result.code == "SECURE_STORE_UNAVAILABLE"
    assert result.data == {"checks": expected}
    assert exit_code_for(result) is ExitCode.TERMINAL
    assert (config.calls, secrets.calls, api.version_calls, auth.calls) == (1, 1, 1, 1)


@pytest.mark.parametrize("public_code", ["SERVICE_REQUEST_FAILED", "SERVICE_RESPONSE_INVALID"])
def test_non_retryable_version_api_failure_remains_terminal(public_code: str) -> None:
    error = ApiClientError(
        code=public_code,
        message="raw upstream detail",
        retryable=False,
        request_id="req-terminal-api",
    )
    service, *_ = make_service(api_outcome=error)

    result = service.run()

    assert result.status is CommandStatus.TERMINAL_ERROR
    assert result.code == public_code
    assert result.message == "Readiness check could not be completed"
    assert result.request_id == "req-terminal-api"
    assert exit_code_for(result) is ExitCode.TERMINAL


def test_terminal_auth_failure_is_not_downgraded_to_doctor_issues() -> None:
    service, *_ = make_service(
        auth_outcome=terminal_error_result(
            "SERVICE_RESPONSE_INVALID",
            "raw auth parser detail",
            request_id="req-auth-terminal",
        )
    )

    result = service.run()

    assert result.status is CommandStatus.TERMINAL_ERROR
    assert result.code == "SERVICE_RESPONSE_INVALID"
    assert result.message == "Readiness check could not be completed"
    assert result.request_id == "req-auth-terminal"
    assert exit_code_for(result) is ExitCode.TERMINAL


def test_output_contains_only_sanitized_checks_and_public_metadata() -> None:
    error = ApiClientError(
        code="SERVICE_TEMPORARILY_UNAVAILABLE",
        message="https://internal.example.invalid token jwt clientCode cid stack production",
        retryable=True,
    )
    service, *_ = make_service(api_outcome=error)

    rendered = service.run().model_dump_json()

    for forbidden in ["internal.example", " token ", "jwt", "clientCode", "cid", "stack", "production"]:
        assert forbidden not in rendered
