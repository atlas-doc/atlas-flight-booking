"""HTTP boundary for the Atlas control API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import NoReturn
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
    ServerVersion,
)
from atlas_cli.config import InternalSettings

logger = logging.getLogger(__name__)
access_credential_records_adapter = TypeAdapter(list[AccessCredentialRecord])


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
    ) -> tuple[ControlEnvelope[object], httpx.Response]:
        if timeout_seconds is not None and timeout_seconds <= 0:
            self._raise_public_error(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )
        try:
            url = f"{self._settings.control_api_base_url.rstrip('/')}{path}"
            if timeout_seconds is None:
                response = self._client.request(method, url, json=json, headers=headers)
            else:
                response = asyncio.run(
                    self._request_with_total_timeout(
                        method,
                        url,
                        json=json,
                        headers=headers,
                        timeout_seconds=timeout_seconds,
                    )
                )
        except (TimeoutError, httpx.RequestError):
            self._raise_public_error(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )

        if protected and response.status_code == 401:
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

        if not envelope.success or envelope.code != 200:
            self._raise_service_error(envelope.code, envelope.uuid, protected=protected)
        return envelope, response

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
        if protected and service_code == 5107:
            self._raise_public_error(
                code="AUTHORIZATION_REQUIRED",
                message="Authorization required",
                retryable=False,
                request_id=request_id,
            )
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
