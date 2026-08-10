"""HTTP boundary for the Atlas control API."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import NoReturn, Protocol
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError

from atlas_cli.api_models import (
    AccessCredentialRecord,
    AccessInfo,
    AccessInfoPayload,
    AuthTokenCreated,
    AuthTokenStatus,
    ControlEnvelope,
    ExchangedCredentials,
    FareSearchUsage,
    PreProductionAccessInfos,
    ProductionAccessInfos,
    RefreshedSession,
    ServerVersion,
)
from atlas_cli.config import InternalSettings
from atlas_cli.secure_store import Credentials, SecureStoreError

logger = logging.getLogger(__name__)
access_credential_records_adapter = TypeAdapter(list[AccessCredentialRecord])
PROTECTED_SESSION_REFRESH_CODES = frozenset({5107, 5555})
PROTECTED_SESSION_REFRESH_HTTP_STATUSES = frozenset({401, 461})


class SessionCredentialStore(Protocol):
    def load_credentials(self) -> Credentials | None: ...

    def save_credentials(self, credentials: Credentials) -> None: ...


class ApiClientError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id


class AtlasApiClient:
    def __init__(
        self,
        settings: InternalSettings,
        client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
        credential_store: SessionCredentialStore | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._default_timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.read_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )
        self._client = client or httpx.Client(timeout=self._default_timeout)
        self._async_client = async_client
        self._credential_store = credential_store

    def get_server_version(self) -> ServerVersion:
        envelope, _ = self._request("GET", "/cli/version")
        if not isinstance(envelope.data, str):
            self._raise_invalid_response(envelope.uuid)
        return ServerVersion(version=envelope.data, request_id=envelope.uuid)

    def create_auth_token(self, *, cli_version: str, device_name: str) -> AuthTokenCreated:
        envelope, _ = self._request(
            "POST",
            "/cli/auth/token",
            json={"cliVersion": cli_version, "channel": "skill", "deviceName": device_name},
        )
        try:
            parsed = AuthTokenCreated.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return parsed.model_copy(update={"request_id": envelope.uuid})

    def get_auth_token_status(self, token: str, *, timeout_seconds: float | None = None) -> AuthTokenStatus:
        envelope, response = self._request(
            "GET",
            f"/cli/auth/token/{quote(token, safe='')}/status",
            timeout_seconds=timeout_seconds,
        )
        try:
            parsed = AuthTokenStatus.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return parsed.model_copy(
            update={
                "request_id": envelope.uuid,
                "retry_after_seconds": self._parse_retry_after(response.headers.get("Retry-After")),
            }
        )

    def exchange_auth_token(self, token: str, *, timeout_seconds: float | None = None) -> ExchangedCredentials:
        envelope, _ = self._request(
            "GET",
            f"/cli/auth/token/{quote(token, safe='')}",
            timeout_seconds=timeout_seconds,
        )
        try:
            parsed = ExchangedCredentials.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return parsed.model_copy(update={"request_id": envelope.uuid})

    def refresh_session(
        self,
        jwt: str,
        *,
        timeout_seconds: float | None = None,
        _deadline: float | None = None,
    ) -> RefreshedSession:
        envelope, _ = self._request(
            "POST",
            "/cli/session/refresh",
            headers={"Token": jwt},
            timeout_seconds=timeout_seconds,
            protected=True,
            _allow_refresh=False,
            _deadline=_deadline,
        )
        try:
            parsed = RefreshedSession.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return parsed.model_copy(update={"request_id": envelope.uuid})

    def check_access_info(self, jwt: str, *, timeout_seconds: float | None = None) -> AccessInfo:
        envelope, _ = self._request(
            "GET",
            "/cli/agent/access-info/check",
            headers={"Token": jwt},
            timeout_seconds=timeout_seconds,
            protected=True,
        )
        try:
            parsed = AccessInfoPayload.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return AccessInfo(
            activation_status=parsed.client_status.activation_status,
            top_up_completed=parsed.top_up.completed,
            access_info_exists=parsed.access_info.exists,
            request_id=envelope.uuid,
        )

    def get_preproduction_access_infos(
        self, jwt: str, *, timeout_seconds: float | None = None
    ) -> PreProductionAccessInfos:
        envelope, _ = self._request(
            "GET",
            "/cli/pre-production/access-infos",
            headers={"Token": jwt},
            timeout_seconds=timeout_seconds,
            protected=True,
        )
        try:
            parsed = PreProductionAccessInfos.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return parsed.model_copy(update={"request_id": envelope.uuid})

    def get_or_create_production_access_infos(
        self, jwt: str, *, timeout_seconds: float | None = None
    ) -> ProductionAccessInfos:
        envelope, _ = self._request(
            "POST",
            "/cli/production/access-info",
            headers={"Token": jwt},
            timeout_seconds=timeout_seconds,
            protected=True,
        )
        try:
            if isinstance(envelope.data, list):
                records = access_credential_records_adapter.validate_python(envelope.data)
                parsed = ProductionAccessInfos(prd=records)
            else:
                parsed = ProductionAccessInfos.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return parsed.model_copy(update={"request_id": envelope.uuid})

    def get_fare_search_usage(self, jwt: str) -> FareSearchUsage:
        envelope, _ = self._request(
            "GET",
            "/cli/fare-search/usage",
            headers={"Token": jwt},
            protected=True,
        )
        try:
            parsed = FareSearchUsage.model_validate(envelope.data)
        except ValidationError:
            self._raise_invalid_response(envelope.uuid)
        return parsed.model_copy(update={"request_id": envelope.uuid})

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        protected: bool = False,
        _allow_refresh: bool = True,
        _deadline: float | None = None,
    ) -> tuple[ControlEnvelope[object], httpx.Response]:
        if timeout_seconds is not None and timeout_seconds <= 0:
            self._raise_public_error(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )
        deadline = _deadline
        request_timeout = timeout_seconds
        if deadline is None and timeout_seconds is not None:
            deadline = time.monotonic() + timeout_seconds
        elif deadline is not None:
            request_timeout = deadline - time.monotonic()
            if request_timeout <= 0:
                self._raise_public_error(
                    code="SERVICE_TEMPORARILY_UNAVAILABLE",
                    message="Service temporarily unavailable",
                    retryable=True,
                )
        request_headers = self._current_session_headers(headers) if protected and _allow_refresh else headers
        try:
            url = f"{self._settings.control_api_base_url.rstrip('/')}{path}"
            if request_timeout is None:
                response = self._client.request(method, url, json=json, headers=request_headers)
            else:
                response = asyncio.run(
                    self._request_with_total_timeout(
                        method,
                        url,
                        json=json,
                        headers=request_headers,
                        timeout_seconds=request_timeout,
                    )
                )
        except (TimeoutError, httpx.RequestError):
            self._raise_public_error(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )

        if protected and response.status_code in PROTECTED_SESSION_REFRESH_HTTP_STATUSES:
            if _allow_refresh:
                return self._refresh_and_retry(
                    method,
                    path,
                    json=json,
                    headers=request_headers,
                    deadline=deadline,
                    request_id=None,
                )
            self._raise_public_error(
                code="AUTHORIZATION_REQUIRED",
                message="Authorization required",
                retryable=False,
            )
        if response.status_code == 429 or response.status_code >= 500:
            self._raise_public_error(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )
        if response.status_code >= 400:
            self._raise_public_error(
                code="SERVICE_REQUEST_FAILED",
                message="Request could not be completed",
                retryable=False,
            )

        try:
            payload = response.json()
            envelope = ControlEnvelope[object].model_validate(payload)
        except (ValueError, ValidationError):
            self._raise_invalid_response(None)

        if (
            protected
            and envelope.code in PROTECTED_SESSION_REFRESH_CODES
            and (not envelope.success or envelope.code != 200)
            and _allow_refresh
        ):
            return self._refresh_and_retry(
                method,
                path,
                json=json,
                headers=request_headers,
                deadline=deadline,
                request_id=envelope.uuid,
            )
        if not envelope.success or envelope.code != 200:
            self._raise_service_error(envelope.code, envelope.uuid, protected=protected)
        return envelope, response

    def _current_session_headers(
        self,
        headers: Mapping[str, str] | None,
    ) -> Mapping[str, str] | None:
        supplied_token = headers.get("Token") if headers is not None else None
        if not isinstance(supplied_token, str) or not supplied_token or self._credential_store is None:
            return headers
        current = self._load_credentials()
        if current is None or current.jwt == supplied_token:
            return headers
        assert headers is not None
        updated_headers = dict(headers)
        updated_headers["Token"] = current.jwt
        return updated_headers

    def _refresh_and_retry(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None,
        headers: Mapping[str, str] | None,
        deadline: float | None,
        request_id: str | None,
    ) -> tuple[ControlEnvelope[object], httpx.Response]:
        failed_token = headers.get("Token") if headers is not None else None
        if not isinstance(failed_token, str) or not failed_token or self._credential_store is None:
            self._raise_authorization_required(request_id)

        current = self._load_credentials()
        if current is None:
            self._raise_authorization_required(request_id)
        if current.jwt != failed_token:
            return self._retry_with_token(
                method,
                path,
                token=current.jwt,
                json=json,
                headers=headers,
                deadline=deadline,
            )

        try:
            refreshed = self.refresh_session(failed_token, _deadline=deadline)
        except ApiClientError as error:
            if error.code == "AUTHORIZATION_REQUIRED":
                concurrent = self._load_credentials()
                if concurrent is not None and concurrent.jwt != failed_token:
                    return self._retry_with_token(
                        method,
                        path,
                        token=concurrent.jwt,
                        json=json,
                        headers=headers,
                        deadline=deadline,
                    )
            raise

        concurrent = self._load_credentials()
        if concurrent is not None and concurrent.jwt != failed_token:
            retry_token = concurrent.jwt
        else:
            updated = current.model_copy(update={"jwt": refreshed.token})
            self._save_credentials(updated)
            retry_token = refreshed.token
        return self._retry_with_token(
            method,
            path,
            token=retry_token,
            json=json,
            headers=headers,
            deadline=deadline,
        )

    def _retry_with_token(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: Mapping[str, object] | None,
        headers: Mapping[str, str] | None,
        deadline: float | None,
    ) -> tuple[ControlEnvelope[object], httpx.Response]:
        retried_headers = dict(headers or {})
        retried_headers["Token"] = token
        return self._request(
            method,
            path,
            json=json,
            headers=retried_headers,
            protected=True,
            _allow_refresh=False,
            _deadline=deadline,
        )

    def _load_credentials(self) -> Credentials | None:
        if self._credential_store is None:
            return None
        try:
            return self._credential_store.load_credentials()
        except SecureStoreError:
            self._raise_secure_store_unavailable()

    def _save_credentials(self, credentials: Credentials) -> None:
        if self._credential_store is None:
            self._raise_authorization_required(None)
        try:
            self._credential_store.save_credentials(credentials)
        except SecureStoreError:
            self._raise_secure_store_unavailable()

    async def _request_with_total_timeout(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, object] | None,
        headers: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> httpx.Response:
        async with asyncio.timeout(timeout_seconds):
            if self._async_client is not None:
                return await self._async_client.request(
                    method,
                    url,
                    json=json,
                    headers=headers,
                    timeout=httpx.Timeout(timeout_seconds),
                )
            async with httpx.AsyncClient(timeout=self._default_timeout) as client:
                return await client.request(
                    method,
                    url,
                    json=json,
                    headers=headers,
                    timeout=httpx.Timeout(timeout_seconds),
                )

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    def _raise_service_error(
        self,
        service_code: int,
        request_id: str | None,
        *,
        protected: bool,
    ) -> NoReturn:
        if protected and service_code in PROTECTED_SESSION_REFRESH_CODES:
            self._raise_public_error(
                code="AUTHORIZATION_REQUIRED",
                message="Authorization required",
                retryable=False,
                request_id=request_id,
            )
        if service_code == 5120:
            self._raise_authorization_required(request_id)
        if service_code == 5119:
            self._raise_public_error(
                code="AUTH_EXPIRED",
                message="Authorization expired",
                retryable=False,
                request_id=request_id,
            )
        self._raise_public_error(
            code="SERVICE_REQUEST_FAILED",
            message="Request could not be completed",
            retryable=False,
            request_id=request_id,
        )

    def _raise_invalid_response(self, request_id: str | None) -> NoReturn:
        self._raise_public_error(
            code="SERVICE_RESPONSE_INVALID",
            message="Service returned an invalid response",
            retryable=False,
            request_id=request_id,
        )

    def _raise_authorization_required(self, request_id: str | None) -> NoReturn:
        self._raise_public_error(
            code="AUTHORIZATION_REQUIRED",
            message="Authorization required",
            retryable=False,
            request_id=request_id,
        )

    def _raise_secure_store_unavailable(self) -> NoReturn:
        self._raise_public_error(
            code="SECURE_STORE_UNAVAILABLE",
            message="Secure credential storage is unavailable",
            retryable=False,
        )

    @staticmethod
    def _raise_public_error(
        *,
        code: str,
        message: str,
        retryable: bool,
        request_id: str | None = None,
    ) -> NoReturn:
        logger.warning("Atlas API request failed code=%s request_id=%s", code, request_id)
        raise ApiClientError(code=code, message=message, retryable=retryable, request_id=request_id)
