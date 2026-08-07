"""Isolated AK/SK-authenticated HTTP boundary for Atlas business APIs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import NoReturn

import httpx

from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import BusinessRoute, SearchRoute
from atlas_cli.secure_store import ApiCredential

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True)
class BusinessResponse:
    status: int
    msg: str | None
    request_id: str | None
    data: dict[str, object]


class BusinessApiError(RuntimeError):
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


class AtlasBusinessClient:
    def __init__(
        self,
        settings: InternalSettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client()
        self._connect_timeout_seconds = settings.connect_timeout_seconds
        self._read_timeout_seconds = settings.read_timeout_seconds
        self._search_read_timeout_seconds = settings.search_read_timeout_seconds
        self._timeout = httpx.Timeout(
            connect=self._connect_timeout_seconds,
            read=self._read_timeout_seconds,
            write=self._read_timeout_seconds,
            pool=self._connect_timeout_seconds,
        )
        self._search_timeout = httpx.Timeout(
            connect=self._connect_timeout_seconds,
            read=self._search_read_timeout_seconds,
            write=self._read_timeout_seconds,
            pool=self._connect_timeout_seconds,
        )

    def post(
        self,
        route: SearchRoute | BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
        *,
        request_timeout_seconds: float | None = None,
    ) -> BusinessResponse:
        if request_timeout_seconds is not None and not request_timeout_seconds > 0:
            raise ValueError("request_timeout_seconds must be positive")
        url = f"{route.base_url.rstrip('/')}{route.path}"
        headers = {
            "x-atlas-client-id": credential.ak,
            "x-atlas-client-secret": credential.sk,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        try:
            response = self._client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout_for(
                    request_timeout_seconds,
                    search=isinstance(route, SearchRoute),
                ),
            )
        except httpx.RequestError:
            self._raise(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )

        request_id = self._header_request_id(response)
        if response.status_code == 401:
            self._raise(
                code="CREDENTIAL_REJECTED",
                message="Service credentials need to be refreshed",
                retryable=True,
                request_id=request_id,
            )
        if response.status_code == 429 or response.status_code >= 500:
            self._raise(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
                request_id=request_id,
            )
        if response.status_code < 200 or response.status_code >= 300:
            self._raise(
                code="SERVICE_REQUEST_FAILED",
                message="Service request could not be completed",
                retryable=False,
                request_id=request_id,
            )

        try:
            body = response.json()
        except ValueError:
            self._raise_invalid_response(request_id)
        if not isinstance(body, dict):
            self._raise_invalid_response(request_id)

        status = body.get("status")
        if not isinstance(status, int) or isinstance(status, bool):
            self._raise_invalid_response(request_id)

        body_request_id = self._safe_request_id(body.get("requestId")) or self._safe_request_id(body.get("uuid"))
        request_id = body_request_id or request_id
        if status == 900:
            self._raise(
                code="CREDENTIAL_REJECTED",
                message="Service credentials need to be refreshed",
                retryable=True,
                request_id=request_id,
            )

        msg_value = body.get("msg")
        msg = msg_value if isinstance(msg_value, str) else None
        data = {
            str(key): value
            for key, value in body.items()
            if key not in {"status", "msg", "requestId", "uuid"}
        }
        return BusinessResponse(
            status=status,
            msg=msg,
            request_id=request_id,
            data=data,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _timeout_for(
        self,
        request_timeout_seconds: float | None,
        *,
        search: bool,
    ) -> httpx.Timeout:
        if request_timeout_seconds is None:
            return self._search_timeout if search else self._timeout
        read_timeout_seconds = (
            self._search_read_timeout_seconds if search else self._read_timeout_seconds
        )
        return httpx.Timeout(
            connect=min(self._connect_timeout_seconds, request_timeout_seconds),
            read=min(read_timeout_seconds, request_timeout_seconds),
            write=min(self._read_timeout_seconds, request_timeout_seconds),
            pool=min(self._connect_timeout_seconds, request_timeout_seconds),
        )

    @classmethod
    def _header_request_id(cls, response: httpx.Response) -> str | None:
        return cls._safe_request_id(response.headers.get("X-Request-ID")) or cls._safe_request_id(
            response.headers.get("X-Atlas-Request-ID")
        )

    @staticmethod
    def _safe_request_id(value: object) -> str | None:
        if isinstance(value, str) and value:
            return value[:200]
        return None

    @classmethod
    def _raise_invalid_response(cls, request_id: str | None) -> NoReturn:
        cls._raise(
            code="SERVICE_RESPONSE_INVALID",
            message="Service response could not be processed",
            retryable=False,
            request_id=request_id,
        )

    @staticmethod
    def _raise(
        *,
        code: str,
        message: str,
        retryable: bool,
        request_id: str | None = None,
    ) -> NoReturn:
        raise BusinessApiError(
            code=code,
            message=message,
            retryable=retryable,
            request_id=request_id,
        )
