"""Normalize Atlas search responses into the bounded public search contract."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, NoReturn, Protocol
from uuid import uuid4

from atlas_cli.business_client import AtlasBusinessClient, BusinessResponse
from atlas_cli.endpoints import SearchProvider, SearchRoute
from atlas_cli.routing_normalizer import RoutingNormalizer, RoutingRejected
from atlas_cli.search_models import (
    NormalizedSearch,
    SearchRequest,
)
from atlas_cli.secure_store import ApiCredential


class BusinessClient(Protocol):
    def post(
        self,
        route: SearchRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse: ...


class SearchAdapterError(RuntimeError):
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


def _new_request_id() -> str:
    return uuid4().hex


class BaseSearchAdapter:
    def __init__(
        self,
        business: BusinessClient | AtlasBusinessClient,
        *,
        request_id_factory: Callable[[], str] = _new_request_id,
    ) -> None:
        self._business = business
        self._request_id_factory = request_id_factory
        self._normalizer = RoutingNormalizer()

    def _search(
        self,
        route: SearchRoute,
        credential: ApiCredential,
        request: SearchRequest,
        *,
        fare_compare: bool,
    ) -> NormalizedSearch:
        expected_provider = SearchProvider.FARE_COMPARE if fare_compare else SearchProvider.STANDARD
        if route.provider is not expected_provider:
            self._raise_invalid_response(None)
        payload = request.to_upstream_payload(self._request_id_factory())
        if fare_compare:
            payload.pop("requestId", None)
        response = self._business.post(route, credential, payload)
        self._check_status(response)

        raw_routings = response.data.get("routings")
        if not isinstance(raw_routings, list):
            self._raise_invalid_response(response.request_id)
        if not raw_routings:
            return self._empty_search(response, fare_compare=fare_compare)

        offers = []
        for item in raw_routings:
            try:
                offers.append(
                    self._normalizer.normalize(
                        item,
                        request,
                        bookable=False if fare_compare else route.bookable,
                        price_status="reference" if fare_compare else "current",
                        request_id=response.request_id,
                        require_routing_identifier=not fare_compare,
                    )
                )
            except RoutingRejected:
                continue
            except ValueError:
                self._raise_invalid_response(response.request_id)
        return NormalizedSearch(offers=offers, request_id=response.request_id)

    def _empty_search(self, response: BusinessResponse, *, fare_compare: bool) -> NormalizedSearch:
        reason_value = response.data.get("noResultReason")
        if reason_value is None:
            return NormalizedSearch(offers=[], request_id=response.request_id)
        if not fare_compare or not isinstance(reason_value, dict):
            self._raise_invalid_response(response.request_id)
        code = reason_value.get("code")
        if code == "PRICE_FETCH_FAILED":
            self._raise(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Flight search temporarily unavailable",
                retryable=True,
                request_id=response.request_id,
            )
        reason: Literal["route_not_supported", "no_flight", "sold_out"]
        if code == "ROUTE_NOT_SUPPORTED":
            reason = "route_not_supported"
        elif code == "AIRLINE_NO_FLIGHT":
            reason = "no_flight"
        elif code == "FLIGHT_SOLD_OUT":
            reason = "sold_out"
        else:
            self._raise_invalid_response(response.request_id)
        recent = self._recent_dates(reason_value.get("recentFlightDates"))
        return NormalizedSearch(
            offers=[],
            reason=reason,
            recent_flight_dates=recent,
            request_id=response.request_id,
        )

    @staticmethod
    def _recent_dates(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        for item in value[:7]:
            if isinstance(item, str) and len(item) == 8 and item.isdigit():
                normalized.append(f"{item[:4]}-{item[4:6]}-{item[6:8]}")
        return normalized

    @classmethod
    def _check_status(cls, response: BusinessResponse) -> None:
        if response.status == 0:
            return
        if response.status == 109:
            cls._raise(
                code="SEARCH_LIMIT_REACHED",
                message="Flight search limit reached",
                retryable=False,
                request_id=response.request_id,
            )
        if response.status in {110, 112, 9999}:
            cls._raise(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Flight search temporarily unavailable",
                retryable=True,
                request_id=response.request_id,
            )
        cls._raise(
            code="SEARCH_REQUEST_REJECTED",
            message="Flight search could not be completed",
            retryable=False,
            request_id=response.request_id,
        )

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
        request_id: str | None,
    ) -> NoReturn:
        raise SearchAdapterError(
            code=code,
            message=message,
            retryable=retryable,
            request_id=request_id,
        )


class FareSearchAdapter(BaseSearchAdapter):
    def search(
        self,
        route: SearchRoute,
        credential: ApiCredential,
        request: SearchRequest,
    ) -> NormalizedSearch:
        return self._search(route, credential, request, fare_compare=True)


class BookingSearchAdapter(BaseSearchAdapter):
    def search(
        self,
        route: SearchRoute,
        credential: ApiCredential,
        request: SearchRequest,
    ) -> NormalizedSearch:
        return self._search(route, credential, request, fare_compare=False)
