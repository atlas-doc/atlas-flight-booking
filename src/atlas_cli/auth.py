"""Atlas authorization application service."""

from __future__ import annotations

import platform
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from atlas_cli.access import AccessManagerError, AccessSnapshot, ticketing_available
from atlas_cli.api_client import ApiClientError, AtlasApiClient
from atlas_cli.api_models import AccessInfo
from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import CustomerMode
from atlas_cli.models import (
    CommandResult,
    action_required_result,
    retryable_error_result,
    success_result,
    terminal_error_result,
)
from atlas_cli.secure_store import Credentials, PendingAuth, SecretStore, SecureStoreError


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class CredentialSynchronizer(Protocol):
    def synchronize(self, jwt: str) -> AccessSnapshot: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def build_authorization_url(base_url: str, token: str) -> str:
    query = urlencode({"utm": "skill", "cliAuthToken": token, "redirect": "/skill-entry"})
    return f"{base_url}?{query}"


def result_from_api_error(error: ApiClientError) -> CommandResult:
    if error.code == "SERVICE_TEMPORARILY_UNAVAILABLE":
        return retryable_error_result(
            "AUTH_SERVICE_UNAVAILABLE",
            "Authorization service temporarily unavailable",
            request_id=error.request_id,
        )
    if error.code in {"AUTH_EXPIRED", "AUTH_SESSION_MISSING"}:
        return action_required_result(error.code, error.message, request_id=error.request_id)
    if error.retryable:
        return retryable_error_result(error.code, error.message, request_id=error.request_id)
    return terminal_error_result(error.code, error.message, request_id=error.request_id)


def capability_payload(
    *,
    search_available: bool,
    ticketing_is_available: bool,
    ticketing_activation_url: str,
) -> dict[str, object]:
    data: dict[str, object] = {
        "authenticated": True,
        "search_available": search_available,
        "ticketing_available": ticketing_is_available,
    }
    if not ticketing_is_available:
        data["ticketing_activation_url"] = ticketing_activation_url
    return data


def capability_data(
    access: AccessInfo,
    *,
    mode: CustomerMode = CustomerMode.PROD,
    ticketing_activation_url: str,
) -> dict[str, object]:
    return capability_payload(
        search_available=True,
        ticketing_is_available=(
            mode is CustomerMode.SANDBOX
            or ticketing_available(access.activation_status, access.top_up_completed)
        ),
        ticketing_activation_url=ticketing_activation_url,
    )


class AuthService:
    def __init__(
        self,
        *,
        api: AtlasApiClient,
        secrets: SecretStore,
        settings: InternalSettings,
        cli_version: str,
        platform_system: Callable[[], str] = platform.system,
        platform_machine: Callable[[], str] = platform.machine,
        clock: Clock | None = None,
        credential_synchronizer: CredentialSynchronizer | None = None,
        customer_mode: CustomerMode = CustomerMode.PROD,
    ) -> None:
        self._api = api
        self._secrets = secrets
        self._settings = settings
        self._cli_version = cli_version
        self._platform_system = platform_system
        self._platform_machine = platform_machine
        self._clock = clock or SystemClock()
        self._credential_synchronizer = credential_synchronizer
        self._customer_mode = customer_mode

    def login(self) -> CommandResult:
        device_name = f"{self._platform_system()}-{self._platform_machine()}".lower()
        try:
            created = self._api.create_auth_token(cli_version=self._cli_version, device_name=device_name)
            self._secrets.save_pending_auth(PendingAuth(token=created.token, expires_at=created.expires_at))
        except SecureStoreError:
            return terminal_error_result(
                "SECURE_STORE_UNAVAILABLE",
                "Secure credential storage is unavailable",
            )
        except ApiClientError as error:
            return result_from_api_error(error)

        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Complete authorization in the browser",
            request_id=created.request_id,
            data={
                "authorization_url": build_authorization_url(self._settings.authorization_page_url, created.token),
                "expires_at": created.expires_at,
            },
        )

    def status(self) -> CommandResult:
        try:
            credentials = self._secrets.load_credentials()
        except SecureStoreError:
            return terminal_error_result(
                "SECURE_STORE_UNAVAILABLE",
                "Secure credential storage is unavailable",
            )
        if credentials is None:
            return action_required_result(
                "AUTHORIZATION_REQUIRED",
                "Authorization required",
                data={"authenticated": False},
            )

        try:
            access = self._api.check_access_info(credentials.jwt)
        except ApiClientError as error:
            return self._result_from_protected_api_error(error)
        return success_result(
            "AUTHORIZED",
            "Authorization active",
            request_id=access.request_id,
            data=capability_data(
                access,
                mode=self._customer_mode,
                ticketing_activation_url=self._settings.subscription_page_url,
            ),
        )

    def refresh_session(self) -> CommandResult:
        try:
            credentials = self._secrets.load_credentials()
        except SecureStoreError:
            return self._secure_store_unavailable()
        if credentials is None:
            return action_required_result(
                "AUTHORIZATION_REQUIRED",
                "Authorization required",
            )

        try:
            refreshed = self._api.refresh_session(credentials.jwt)
            self._secrets.save_credentials(
                credentials.model_copy(update={"jwt": refreshed.token})
            )
        except SecureStoreError:
            return self._secure_store_unavailable()
        except ApiClientError as error:
            return self._result_from_protected_api_error(error)
        return success_result(
            "SESSION_REFRESHED",
            "Authorization session refreshed",
            request_id=refreshed.request_id,
            data={"expire_seconds": refreshed.expire_seconds},
        )

    def poll(self, timeout_seconds: int) -> CommandResult:
        try:
            pending = self._secrets.load_pending_auth()
            credentials = self._secrets.load_credentials() if pending is None else None
        except SecureStoreError:
            return self._secure_store_unavailable()
        if pending is None:
            if credentials is not None:
                deadline = self._clock.monotonic() + float(timeout_seconds)
                return self._validate_credentials(credentials, deadline)
            return action_required_result(
                "AUTH_SESSION_MISSING",
                "Authorization session is missing",
            )

        token_lifetime = self._remaining_token_lifetime(pending)
        if token_lifetime <= 0:
            return self._expire_pending()

        deadline = self._clock.monotonic() + min(float(timeout_seconds), token_lifetime)
        last_request_id: str | None = None
        while True:
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                return self._pending_result(last_request_id)
            try:
                status = self._api.get_auth_token_status(pending.token, timeout_seconds=remaining)
            except ApiClientError as error:
                if error.code == "AUTH_EXPIRED":
                    return self._expire_pending(request_id=error.request_id)
                return result_from_api_error(error)
            last_request_id = status.request_id
            normalized_status = status.status.upper()

            if normalized_status == "PENDING":
                remaining = deadline - self._clock.monotonic()
                if remaining <= 0:
                    return self._pending_result(last_request_id)
                interval = status.retry_after_seconds or self._settings.poll_interval_seconds
                self._clock.sleep(min(interval, remaining))
                continue
            if normalized_status == "COMPLETED":
                if deadline - self._clock.monotonic() <= 0:
                    return self._pending_result(last_request_id)
                return self._complete_authorization(pending, deadline)
            if normalized_status == "EXPIRED":
                return self._expire_pending(request_id=last_request_id)
            return terminal_error_result(
                "AUTH_STATUS_INVALID",
                "Authorization status could not be processed",
                request_id=last_request_id,
            )

    def _complete_authorization(self, pending: PendingAuth, deadline: float) -> CommandResult:
        try:
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                return self._pending_result(None)
            exchanged = self._api.exchange_auth_token(pending.token, timeout_seconds=remaining)
            credentials = Credentials(
                jwt=exchanged.jwt,
                client_code=exchanged.client_code,
                cid=exchanged.cid,
            )
            self._secrets.save_credentials(credentials)
            if self._credential_synchronizer is not None:
                snapshot = self._credential_synchronizer.synchronize(credentials.jwt)
                self._secrets.clear_pending_auth()
                return success_result(
                    "AUTHORIZED",
                    "Authorization active",
                    request_id=snapshot.request_id,
                    data=capability_payload(
                        search_available=snapshot.search_available,
                        ticketing_is_available=snapshot.ticketing_available,
                        ticketing_activation_url=self._settings.subscription_page_url,
                    ),
                )
            self._secrets.clear_pending_auth()
        except SecureStoreError:
            return self._secure_store_unavailable()
        except ApiClientError as error:
            if error.code == "AUTH_EXPIRED":
                return self._expire_pending(request_id=error.request_id)
            return self._result_from_protected_api_error(error)
        except AccessManagerError as error:
            if error.retryable:
                return retryable_error_result(
                    error.code,
                    error.message,
                    request_id=error.request_id,
                )
            return terminal_error_result(
                error.code,
                error.message,
                request_id=error.request_id,
            )

        return self._validate_credentials(credentials, deadline)

    def _validate_credentials(self, credentials: Credentials, deadline: float) -> CommandResult:
        remaining = deadline - self._clock.monotonic()
        if remaining <= 0:
            return retryable_error_result(
                "AUTH_SERVICE_UNAVAILABLE",
                "Authorization verification did not complete in time",
            )
        try:
            access = self._api.check_access_info(credentials.jwt, timeout_seconds=remaining)
        except ApiClientError as error:
            return self._result_from_protected_api_error(error)
        return success_result(
            "AUTHORIZED",
            "Authorization active",
            request_id=access.request_id,
            data=capability_data(
                access,
                mode=self._customer_mode,
                ticketing_activation_url=self._settings.subscription_page_url,
            ),
        )

    def _remaining_token_lifetime(self, pending: PendingAuth) -> float:
        try:
            expires_at = datetime.fromisoformat(pending.expires_at)
        except ValueError:
            return 0.0
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=ZoneInfo(self._settings.server_timezone))
        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return (expires_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()

    def _result_from_protected_api_error(self, error: ApiClientError) -> CommandResult:
        if error.code != "AUTHORIZATION_REQUIRED":
            return result_from_api_error(error)
        try:
            self._secrets.clear_credentials()
        except SecureStoreError:
            return self._secure_store_unavailable()
        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Authorization required",
            request_id=error.request_id,
        )

    def _expire_pending(self, *, request_id: str | None = None) -> CommandResult:
        try:
            self._secrets.clear_pending_auth()
        except SecureStoreError:
            return self._secure_store_unavailable()
        return action_required_result(
            "AUTH_EXPIRED",
            "Authorization session expired",
            request_id=request_id,
        )

    @staticmethod
    def _pending_result(request_id: str | None) -> CommandResult:
        return action_required_result(
            "AUTH_PENDING",
            "Authorization is still pending",
            request_id=request_id,
        )

    @staticmethod
    def _secure_store_unavailable() -> CommandResult:
        return terminal_error_result(
            "SECURE_STORE_UNAVAILABLE",
            "Secure credential storage is unavailable",
        )
