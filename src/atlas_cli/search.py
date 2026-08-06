"""Application service for Agent-safe Atlas flight search and offer listing."""

from __future__ import annotations

from typing import Protocol

from atlas_cli.access import AccessManager, AccessManagerError, SearchAccess
from atlas_cli.api_client import ApiClientError
from atlas_cli.business_client import BusinessApiError
from atlas_cli.endpoints import SearchProvider, SearchRoute
from atlas_cli.models import (
    CommandResult,
    action_required_result,
    retryable_error_result,
    success_result,
    terminal_error_result,
)
from atlas_cli.search_adapters import SearchAdapterError
from atlas_cli.search_models import NormalizedSearch, SearchRequest
from atlas_cli.search_store import SearchStore, SearchStoreError, StoredOffer, StoredSearch
from atlas_cli.secure_store import ApiCredential, Credentials, SecureStoreError


class ControlCredentialStore(Protocol):
    def load_credentials(self) -> Credentials | None: ...

    def clear_credentials(self) -> None: ...


class SearchAccessResolver(Protocol):
    def resolve_search_access(self, jwt: str) -> SearchAccess: ...


class SearchAdapter(Protocol):
    def search(
        self,
        route: SearchRoute,
        credential: ApiCredential,
        request: SearchRequest,
    ) -> NormalizedSearch: ...


class SearchService:
    def __init__(
        self,
        *,
        secrets: ControlCredentialStore,
        access: SearchAccessResolver | AccessManager,
        fare_adapter: SearchAdapter,
        booking_adapter: SearchAdapter,
        store: SearchStore,
        public_offer_limit: int = 20,
    ) -> None:
        self._secrets = secrets
        self._access = access
        self._fare_adapter = fare_adapter
        self._booking_adapter = booking_adapter
        self._store = store
        self._public_offer_limit = max(1, public_offer_limit)

    def search(self, request: SearchRequest | None) -> CommandResult:
        try:
            selected_request = request or self._store.replay_request()
            credentials = self._secrets.load_credentials()
            if credentials is None:
                return self._authorization_required()
            access = self._access.resolve_search_access(credentials.jwt)
        except (
            AccessManagerError,
            ApiClientError,
            SearchStoreError,
            SecureStoreError,
        ) as error:
            return self._error_result(error)

        adapter = self._adapter_for(access)
        try:
            normalized = adapter.search(access.route, access.credential, selected_request)
        except BusinessApiError as error:
            if error.code != "CREDENTIAL_REJECTED":
                return self._error_result(error)
            retry_result = self._retry_after_credential_refresh(credentials.jwt, selected_request)
            if isinstance(retry_result, CommandResult):
                return retry_result
            access, normalized = retry_result
        except SearchAdapterError as error:
            return self._error_result(error)

        try:
            stored = self._store.save(selected_request, normalized, access.route.generation)
        except (SearchStoreError, SecureStoreError) as error:
            return self._error_result(error)

        code = "FLIGHT_SEARCHED" if stored.offers else "SEARCH_NO_RESULTS"
        message = "Flight search completed" if stored.offers else "Flight search completed with no results"
        return success_result(
            code,
            message,
            request_id=stored.request_id,
            data=self._search_data(stored),
        )

    def list_offers(self, search_id: str) -> CommandResult:
        try:
            credentials = self._secrets.load_credentials()
            if credentials is None:
                return self._authorization_required()
            access = self._access.resolve_search_access(credentials.jwt)
            stored = self._store.load_search(search_id, generation=access.route.generation)
        except (
            AccessManagerError,
            ApiClientError,
            BusinessApiError,
            SearchAdapterError,
            SearchStoreError,
            SecureStoreError,
        ) as error:
            return self._error_result(error)

        return success_result(
            "OFFERS_LISTED",
            "Offers listed",
            request_id=stored.request_id,
            data={
                "search_id": stored.search_id,
                "offer_count": len(stored.offers),
                "offers": self._public_offers(stored.offers),
            },
        )

    def _search_data(self, stored: StoredSearch) -> dict[str, object]:
        data: dict[str, object] = {
            "search_id": stored.search_id,
            "offer_count": len(stored.offers),
            "offers": self._public_offers(stored.offers),
        }
        if stored.reason is not None:
            data["reason"] = stored.reason
        if stored.recent_flight_dates:
            data["recent_flight_dates"] = stored.recent_flight_dates
        return data

    def _public_offers(self, offers: list[StoredOffer]) -> list[dict[str, object]]:
        public: list[dict[str, object]] = []
        for stored in offers[: self._public_offer_limit]:
            normalized = stored.offer.model_dump(mode="json", exclude={"upstream_identifier"})
            public.append({"offer_id": stored.offer_id, **normalized})
        return public

    @staticmethod
    def _authorization_required() -> CommandResult:
        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Authorization required",
            data={"authenticated": False},
        )

    def _retry_after_credential_refresh(
        self,
        jwt: str,
        request: SearchRequest,
    ) -> tuple[SearchAccess, NormalizedSearch] | CommandResult:
        try:
            refreshed = self._access.resolve_search_access(jwt)
            adapter = self._adapter_for(refreshed)
            normalized = adapter.search(refreshed.route, refreshed.credential, request)
            return refreshed, normalized
        except BusinessApiError as error:
            if error.code == "CREDENTIAL_REJECTED":
                return terminal_error_result(
                    "CREDENTIAL_REJECTED",
                    "Flight search could not be completed",
                    request_id=error.request_id,
                )
            return self._error_result(error)
        except (AccessManagerError, ApiClientError, SearchAdapterError, SecureStoreError) as error:
            return self._error_result(error)

    def _adapter_for(self, access: SearchAccess) -> SearchAdapter:
        return self._fare_adapter if access.route.provider is SearchProvider.FARE_COMPARE else self._booking_adapter

    def _error_result(self, error: Exception) -> CommandResult:
        if isinstance(error, SecureStoreError):
            return terminal_error_result(
                "SECURE_STORE_UNAVAILABLE",
                "Secure credential storage is unavailable",
            )
        code = getattr(error, "code", "SERVICE_REQUEST_FAILED")
        message = getattr(error, "message", "Service request could not be completed")
        request_id = getattr(error, "request_id", None)
        retryable = bool(getattr(error, "retryable", False))
        if code == "AUTHORIZATION_REQUIRED":
            try:
                self._secrets.clear_credentials()
            except SecureStoreError:
                return terminal_error_result(
                    "SECURE_STORE_UNAVAILABLE",
                    "Secure credential storage is unavailable",
                )
            return action_required_result(code, "Authorization required", request_id=request_id)
        if retryable:
            return retryable_error_result(code, message, request_id=request_id)
        return terminal_error_result(code, message, request_id=request_id)
