"""Secure synchronization and selection of Atlas API access credentials."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, Protocol

from atlas_cli.api_models import (
    AccessCredentialRecord,
    AccessInfo,
    PreProductionAccessInfos,
    ProductionAccessInfos,
)
from atlas_cli.endpoints import (
    BusinessOperation,
    BusinessRoute,
    CredentialSlot,
    CustomerMode,
    EndpointResolver,
    SearchRoute,
)
from atlas_cli.secure_store import ApiCredential, ApiCredentials


class AccessApi(Protocol):
    def check_access_info(self, jwt: str, *, timeout_seconds: float | None = None) -> AccessInfo: ...

    def get_preproduction_access_infos(
        self, jwt: str, *, timeout_seconds: float | None = None
    ) -> PreProductionAccessInfos: ...

    def get_or_create_production_access_infos(
        self, jwt: str, *, timeout_seconds: float | None = None
    ) -> ProductionAccessInfos: ...


class ApiCredentialStore(Protocol):
    def load_api_credentials(self) -> ApiCredentials | None: ...

    def save_api_credentials(self, credentials: ApiCredentials) -> None: ...


class AccessManagerError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        request_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.details = details or {}


def ticketing_available(activation_status: int, top_up_completed: bool) -> bool:
    return activation_status == 3 and top_up_completed


@dataclass(frozen=True)
class AccessSnapshot:
    activation_status: int
    top_up_completed: bool
    search_available: bool
    ticketing_available: bool
    request_id: str | None = None


@dataclass(frozen=True)
class SearchAccess:
    route: SearchRoute
    credential: ApiCredential
    activation_status: int
    top_up_completed: bool


@dataclass(frozen=True)
class TransactionAccess:
    route: BusinessRoute
    credential: ApiCredential
    request_id: str | None = None


class AccessManager:
    def __init__(
        self,
        *,
        api: AccessApi,
        secrets: ApiCredentialStore,
        resolver: EndpointResolver,
        mode: CustomerMode = CustomerMode.PROD,
    ) -> None:
        self._api = api
        self._secrets = secrets
        self._resolver = resolver
        self._mode = mode

    def synchronize(self, jwt: str) -> AccessSnapshot:
        snapshot, _ = self._synchronize(jwt, mode=self._mode)
        return snapshot

    def resolve_search_access(
        self,
        jwt: str,
        *,
        mode: CustomerMode | None = None,
    ) -> SearchAccess:
        selected_mode = mode or self._mode
        snapshot, credentials = self._synchronize(jwt, mode=selected_mode)
        route = self._resolver.resolve_search(
            activation_status=snapshot.activation_status,
            top_up_completed=snapshot.top_up_completed,
            mode=selected_mode,
        )
        credential = self._credential_for_route(credentials, route)
        if credential is None:
            self._raise_invalid_response(snapshot.request_id)
        return SearchAccess(
            route=route,
            credential=credential,
            activation_status=snapshot.activation_status,
            top_up_completed=snapshot.top_up_completed,
        )

    def resolve_transaction_access(
        self,
        jwt: str,
        operation: BusinessOperation,
        *,
        mode: CustomerMode | None = None,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> TransactionAccess:
        selected_mode = mode or self._mode
        snapshot, credentials = self._synchronize(
            jwt,
            mode=selected_mode,
            validate_search_credential=False,
            deadline=deadline,
            monotonic=monotonic,
        )
        if selected_mode is CustomerMode.PROD and not snapshot.ticketing_available:
            url = self._resolver.subscription_url
            raise AccessManagerError(
                code="SUBSCRIPTION_REQUIRED",
                message=f"出票需订阅套餐，详见 {url}",
                request_id=snapshot.request_id,
                details={"url": url},
            )

        route = self._resolver.resolve_business(
            operation=operation,
            activation_status=snapshot.activation_status,
            top_up_completed=snapshot.top_up_completed,
            mode=selected_mode,
        )
        credential = self._credential_for_route(credentials, route)
        if credential is None:
            self._raise_invalid_response(snapshot.request_id)
        return TransactionAccess(
            route=route,
            credential=credential,
            request_id=snapshot.request_id,
        )

    def _synchronize(
        self,
        jwt: str,
        *,
        mode: CustomerMode,
        validate_search_credential: bool = True,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[AccessSnapshot, ApiCredentials]:
        access_timeout = self._remaining_timeout(deadline, monotonic)
        access = (
            self._api.check_access_info(jwt)
            if access_timeout is None
            else self._api.check_access_info(jwt, timeout_seconds=access_timeout)
        )
        existing = self._secrets.load_api_credentials() or ApiCredentials()
        credentials = existing
        request_id = access.request_id
        if access.activation_status != 3:
            preproduction_timeout = self._remaining_timeout(deadline, monotonic)
            grouped = (
                self._api.get_preproduction_access_infos(jwt)
                if preproduction_timeout is None
                else self._api.get_preproduction_access_infos(jwt, timeout_seconds=preproduction_timeout)
            )
            credentials = existing.model_copy(
                update={
                    "pre": self._first_complete(grouped.pre) or existing.pre,
                    "sandbox": self._first_complete(grouped.sandbox) or existing.sandbox,
                }
            )
            request_id = grouped.request_id or request_id
        else:
            production_timeout = self._remaining_timeout(deadline, monotonic)
            production = (
                self._api.get_or_create_production_access_infos(jwt)
                if production_timeout is None
                else self._api.get_or_create_production_access_infos(jwt, timeout_seconds=production_timeout)
            )
            credentials = credentials.model_copy(
                update={
                    "production": self._first_complete(production.prd) or existing.production,
                    "sandbox": self._first_complete(production.sandbox) or existing.sandbox,
                }
            )
            request_id = production.request_id or request_id

        route = self._resolver.resolve_search(
            activation_status=access.activation_status,
            top_up_completed=access.top_up_completed,
            mode=mode,
        )
        if validate_search_credential and self._credential_for_route(credentials, route) is None:
            self._raise_invalid_response(request_id)

        self._secrets.save_api_credentials(credentials)
        snapshot = AccessSnapshot(
            activation_status=access.activation_status,
            top_up_completed=access.top_up_completed,
            search_available=True,
            ticketing_available=ticketing_available(
                access.activation_status,
                access.top_up_completed,
            )
            or mode is CustomerMode.SANDBOX,
            request_id=request_id,
        )
        return snapshot, credentials

    @staticmethod
    def _remaining_timeout(deadline: float | None, monotonic: Callable[[], float]) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise AccessManagerError(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Service temporarily unavailable",
                retryable=True,
            )
        return remaining

    @staticmethod
    def _first_complete(records: list[AccessCredentialRecord]) -> ApiCredential | None:
        for record in records:
            if record.ak.strip() and record.sk.strip():
                return ApiCredential(
                    client_code=record.client_code,
                    ak=record.ak,
                    sk=record.sk,
                )
        return None

    @staticmethod
    def _credential_for_route(
        credentials: ApiCredentials,
        route: SearchRoute | BusinessRoute,
    ) -> ApiCredential | None:
        if route.credential_slot is CredentialSlot.PRE:
            return credentials.pre
        if route.credential_slot is CredentialSlot.SANDBOX:
            return credentials.sandbox
        return credentials.production

    @staticmethod
    def _raise_invalid_response(request_id: str | None) -> NoReturn:
        raise AccessManagerError(
            code="SERVICE_RESPONSE_INVALID",
            message="Service response could not be processed",
            request_id=request_id,
        )
