"""Sanitized readiness diagnostics for Atlas Flight Booking CLI."""

from __future__ import annotations

from atlas_cli.api_client import ApiClientError, AtlasApiClient
from atlas_cli.auth import AuthService
from atlas_cli.config import ConfigStore
from atlas_cli.models import (
    CommandResult,
    CommandStatus,
    action_required_result,
    retryable_error_result,
    success_result,
    terminal_error_result,
)
from atlas_cli.secure_store import SecretStore


class DoctorService:
    def __init__(
        self,
        *,
        config: ConfigStore,
        secrets: SecretStore,
        api: AtlasApiClient,
        auth: AuthService,
        cli_version: str,
    ) -> None:
        self._config = config
        self._secrets = secrets
        self._api = api
        self._auth = auth
        self._cli_version = cli_version

    def run(self) -> CommandResult:
        checks = {
            "cli_version": bool(self._cli_version.strip()),
            "config_directory": self._config.probe(),
            "secure_store": self._secrets.probe(),
            "api_reachable": False,
            "authenticated": False,
        }
        api_error: ApiClientError | None = None
        try:
            server_version = self._api.get_server_version()
            checks["api_reachable"] = True
            checks["cli_version"] = checks["cli_version"] and bool(server_version.version.strip())
        except ApiClientError as error:
            api_error = error
            checks["cli_version"] = False

        auth_result = self._auth.status()
        checks["authenticated"] = auth_result.code == "AUTHORIZED" and auth_result.data.get("authenticated") is True
        data: dict[str, object] = {"checks": checks}

        secure_store_failed = not checks["secure_store"] or auth_result.code == "SECURE_STORE_UNAVAILABLE"
        if secure_store_failed:
            return terminal_error_result(
                "SECURE_STORE_UNAVAILABLE",
                "Secure credential storage is unavailable",
                data=data,
            )

        terminal_api_error = api_error is not None and not api_error.retryable
        terminal_auth_error = auth_result.status is CommandStatus.TERMINAL_ERROR
        if terminal_api_error or terminal_auth_error:
            code = api_error.code if terminal_api_error and api_error is not None else auth_result.code
            request_id = (
                api_error.request_id if terminal_api_error and api_error is not None else auth_result.request_id
            )
            return terminal_error_result(
                code,
                "Readiness check could not be completed",
                request_id=request_id,
                data=data,
            )

        retryable_api_error = api_error is not None and api_error.retryable
        retryable_auth_error = auth_result.status is CommandStatus.RETRYABLE_ERROR
        if retryable_api_error or retryable_auth_error:
            code = api_error.code if retryable_api_error and api_error is not None else auth_result.code
            request_id = (
                api_error.request_id if retryable_api_error and api_error is not None else auth_result.request_id
            )
            return retryable_error_result(
                code,
                "Service temporarily unavailable",
                request_id=request_id,
                data=data,
            )

        if all(checks.values()):
            return success_result(
                "DOCTOR_OK",
                "Atlas Flight Booking CLI readiness checks passed",
                data=data,
            )
        return action_required_result(
            "DOCTOR_ISSUES",
            "Some Atlas Flight Booking CLI readiness checks need attention",
            data=data,
        )
