"""Internal Atlas endpoint and credential routing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from atlas_cli.config import InternalSettings


class CustomerMode(StrEnum):
    PROD = "prod"
    SANDBOX = "sandbox"


class CredentialSlot(StrEnum):
    PRE = "pre"
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class SearchProvider(StrEnum):
    FARE_COMPARE = "fare_compare"
    STANDARD = "standard"


class BusinessOperation(StrEnum):
    VERIFY = "verify"
    BAGGAGE = "baggage"
    SEAT = "seat"
    ORDER = "order"
    PAY = "pay"
    QUERY_ORDER = "query_order"


BUSINESS_PATHS: dict[BusinessOperation, str] = {
    BusinessOperation.VERIFY: "/verify.do",
    BusinessOperation.BAGGAGE: "/getLuggage.do",
    BusinessOperation.SEAT: "/seatAvailability.do",
    BusinessOperation.ORDER: "/order.do",
    BusinessOperation.PAY: "/pay.do",
    BusinessOperation.QUERY_ORDER: "/queryOrderDetails.do",
}


@dataclass(frozen=True)
class SearchRoute:
    base_url: str
    path: str
    provider: SearchProvider
    credential_slot: CredentialSlot
    bookable: bool
    generation: str


@dataclass(frozen=True)
class BusinessRoute:
    base_url: str
    path: str
    operation: BusinessOperation
    credential_slot: CredentialSlot
    generation: str


class EndpointResolver:
    def __init__(self, settings: InternalSettings) -> None:
        self._settings = settings

    def resolve_search(
        self,
        *,
        activation_status: int,
        top_up_completed: bool,
        mode: CustomerMode,
    ) -> SearchRoute:
        if mode is CustomerMode.SANDBOX:
            base_url = self._settings.sandbox_api_base_url.rstrip("/")
            path = "/search.do"
            provider = SearchProvider.STANDARD
            credential_slot = CredentialSlot.SANDBOX
            bookable = True
        elif activation_status != 3:
            base_url = self._settings.prod_api_base_url.rstrip("/")
            path = "/priceCompareSearch.do"
            provider = SearchProvider.FARE_COMPARE
            credential_slot = CredentialSlot.PRE
            bookable = False
        else:
            base_url = self._settings.prod_api_base_url.rstrip("/")
            path = "/search.do"
            provider = SearchProvider.STANDARD
            credential_slot = CredentialSlot.PRODUCTION
            bookable = activation_status == 3 and top_up_completed

        generation = self._generation(
            activation_status=activation_status,
            top_up_completed=top_up_completed,
            mode=mode,
            base_url=base_url,
            path=path,
            credential_slot=credential_slot,
        )
        return SearchRoute(
            base_url=base_url,
            path=path,
            provider=provider,
            credential_slot=credential_slot,
            bookable=bookable,
            generation=generation,
        )

    def resolve_business(
        self,
        *,
        operation: BusinessOperation,
        activation_status: int,
        top_up_completed: bool,
        mode: CustomerMode,
    ) -> BusinessRoute:
        if mode is CustomerMode.PROD and (activation_status != 3 or not top_up_completed):
            raise ValueError

        search_route = self.resolve_search(
            activation_status=activation_status,
            top_up_completed=top_up_completed,
            mode=mode,
        )
        return BusinessRoute(
            base_url=search_route.base_url,
            path=BUSINESS_PATHS[operation],
            operation=operation,
            credential_slot=search_route.credential_slot,
            generation=search_route.generation,
        )

    @property
    def subscription_url(self) -> str:
        return self._settings.subscription_page_url

    def order_url(self, order_no: str) -> str:
        return self._settings.order_detail_url_template.format(order_no=quote(order_no, safe=""))

    @staticmethod
    def _generation(
        *,
        activation_status: int,
        top_up_completed: bool,
        mode: CustomerMode,
        base_url: str,
        path: str,
        credential_slot: CredentialSlot,
    ) -> str:
        canonical = json.dumps(
            {
                "activation_status": activation_status,
                "base_url": base_url,
                "credential_slot": credential_slot.value,
                "mode": mode.value,
                "path": path,
                "top_up_completed": top_up_completed,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]
