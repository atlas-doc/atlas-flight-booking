"""Read-only Atlas order status queries with safely bounded ticket polling."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict

from atlas_cli.access import AccessManager, AccessManagerError, TransactionAccess
from atlas_cli.api_client import ApiClientError
from atlas_cli.booking_models import OrderState, PaymentState, TicketingState
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.business_client import AtlasBusinessClient, BusinessApiError, BusinessResponse
from atlas_cli.business_status import BookingApiError, BusinessStage, booking_error_result, map_business_status
from atlas_cli.endpoints import BusinessOperation, BusinessRoute
from atlas_cli.models import CommandResult, action_required_result, success_result, terminal_error_result
from atlas_cli.secure_store import ApiCredential, Credentials, SecureStoreError

POLL_INTERVALS_SECONDS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 30.0)
MAX_POLL_SECONDS = 120.0
_POLL_TRANSIENT_CODES = {"ORDER_STATUS_UNAVAILABLE", "SERVICE_TEMPORARILY_UNAVAILABLE"}


class OrderDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_no: str
    order_status: Literal["0", "1", "2", "-3"]
    ticket_status: Literal["0", "1"]
    airline_pnrs: tuple[str, ...] = ()
    ticket_numbers: tuple[str, ...] = ()

    @classmethod
    def from_response(cls, data: dict[str, object], *, request_id: str | None) -> OrderDetails:
        return normalize_order_details_without_persisting_raw_pii(data, request_id=request_id)


def require_query_string(data: dict[str, object], key: str, request_id: str | None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BookingApiError.invalid_response(request_id)
    return value


def require_query_enum(data: dict[str, object], key: str, values: set[str], request_id: str | None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value not in values:
        raise BookingApiError.invalid_response(request_id)
    return value


def safe_string_list(value: object, request_id: str | None) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise BookingApiError.invalid_response(request_id)
    return [item.strip() for item in cast(list[str], value)]


def normalize_order_details_without_persisting_raw_pii(
    data: dict[str, object], *, request_id: str | None
) -> OrderDetails:
    order_no = require_query_string(data, "orderNo", request_id)
    order_status = require_query_enum(data, "orderStatus", {"0", "1", "2", "-3"}, request_id)
    ticket_status = require_query_enum(data, "ticketStatus", {"0", "1"}, request_id)
    raw_passengers = data.get("paxTicketInfos")
    if not isinstance(raw_passengers, list):
        raise BookingApiError.invalid_response(request_id)
    airline_pnrs: list[str] = []
    ticket_numbers: list[str] = []
    for raw_passenger in raw_passengers:
        if not isinstance(raw_passenger, dict):
            raise BookingApiError.invalid_response(request_id)
        airline_pnrs.extend(safe_string_list(raw_passenger.get("airlinePNRs"), request_id))
        ticket_numbers.extend(safe_string_list(raw_passenger.get("ticketNos"), request_id))
    return OrderDetails(
        order_no=order_no,
        order_status=cast(Literal["0", "1", "2", "-3"], order_status),
        ticket_status=cast(Literal["0", "1"], ticket_status),
        airline_pnrs=tuple(airline_pnrs),
        ticket_numbers=tuple(ticket_numbers),
    )


class BusinessClient(Protocol):
    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
        *,
        request_timeout_seconds: float | None = None,
    ) -> BusinessResponse: ...


class QueryOrderAdapter:
    def __init__(self, business: BusinessClient | AtlasBusinessClient) -> None:
        self._business = business

    def query(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        order_no: str,
        *,
        request_timeout_seconds: float | None,
    ) -> OrderDetails:
        response = self._business.post(
            route, credential, {"orderNo": order_no}, request_timeout_seconds=request_timeout_seconds
        )
        meaning = map_business_status(BusinessStage.QUERY, response.status)
        if meaning is not None:
            raise BookingApiError.from_meaning(meaning, request_id=response.request_id, upstream_status=response.status)
        details = OrderDetails.from_response(response.data, request_id=response.request_id)
        if details.order_no != order_no:
            raise BookingApiError.invalid_response(response.request_id)
        return details


class ControlCredentialStore(Protocol):
    def load_credentials(self) -> Credentials | None: ...


class TransactionAccessResolver(Protocol):
    def resolve_transaction_access(
        self,
        jwt: str,
        operation: BusinessOperation,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> TransactionAccess: ...


class OrderQueryGateway(Protocol):
    def query(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        order_no: str,
        *,
        request_timeout_seconds: float | None,
    ) -> OrderDetails: ...


class TicketingStore(Protocol):
    def load_order(self, order_no: str) -> OrderState: ...

    def update_payment(self, order_no: str, state: PaymentState) -> OrderState: ...

    def update_ticketing(
        self,
        order_no: str,
        state: TicketingState,
        *,
        airline_pnrs: tuple[str, ...] = (),
        ticket_numbers: tuple[str, ...] = (),
    ) -> OrderState: ...


class TicketingService:
    def __init__(
        self,
        *,
        secrets: ControlCredentialStore,
        access: TransactionAccessResolver | AccessManager,
        adapter: OrderQueryGateway | QueryOrderAdapter,
        booking_store: TicketingStore | BookingStore,
        order_url: Callable[[str], str | None] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._secrets = secrets
        self._access = access
        self._adapter = adapter
        self._booking_store = booking_store
        self._order_url = order_url or self._order_url_from_access(access)
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep

    @staticmethod
    def _order_url_from_access(access: object) -> Callable[[str], str | None]:
        candidate = getattr(access, "order_url", None)
        if callable(candidate):
            return cast(Callable[[str], str | None], candidate)
        return lambda order_no: f"https://www.atriptech.com/#/order/detail/{order_no}/en"

    def query_once(
        self,
        order_no: str,
        *,
        request_timeout_seconds: float | None = None,
    ) -> CommandResult:
        local_order = self._load_local_order(order_no)
        locator = self._locator(order_no)
        access, error_result = self._resolve_access(order_no, locator)
        if error_result is not None:
            return error_result
        assert access is not None
        return self._query_with_access(
            access,
            order_no,
            local_order,
            locator,
            request_timeout_seconds=request_timeout_seconds,
        )

    def poll(self, order_no: str, *, timeout_seconds: float) -> CommandResult:
        deadline = self._monotonic() + min(max(timeout_seconds, 0.0), MAX_POLL_SECONDS)
        local_order = self._load_local_order(order_no)
        locator = self._locator(order_no)
        if deadline - self._monotonic() <= 0:
            return self._pending_result(order_no)
        access, error_result = self._resolve_access(order_no, locator, deadline=deadline)
        if error_result is not None:
            return self._pending_result(order_no) if deadline - self._monotonic() <= 0 else error_result
        assert access is not None
        attempt = 0
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._pending_result(order_no)
            result = self._query_with_access(
                access,
                order_no,
                local_order,
                locator,
                request_timeout_seconds=remaining,
                deadline=deadline,
            )
            if not self._should_continue_polling(result):
                return result
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._pending_result(order_no)
            interval = POLL_INTERVALS_SECONDS[min(attempt, len(POLL_INTERVALS_SECONDS) - 1)]
            attempt += 1
            self._sleep(min(interval, remaining))

    def _resolve_access(
        self,
        order_no: str,
        locator: dict[str, object],
        *,
        deadline: float | None = None,
    ) -> tuple[TransactionAccess | None, CommandResult | None]:
        try:
            credentials = self._secrets.load_credentials()
            if credentials is None or not credentials.jwt.strip():
                raise AccessManagerError(code="AUTHORIZATION_REQUIRED", message="Authorization required")
            access = self._access.resolve_transaction_access(
                credentials.jwt,
                BusinessOperation.QUERY_ORDER,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        except (AccessManagerError, ApiClientError, SecureStoreError) as error:
            return None, self._error_result(error, locator)
        return access, None

    def _query_with_access(
        self,
        access: TransactionAccess,
        order_no: str,
        local_order: OrderState | None,
        locator: dict[str, object],
        *,
        request_timeout_seconds: float | None,
        deadline: float | None = None,
    ) -> CommandResult:
        try:
            if deadline is not None:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return self._pending_result(order_no)
                request_timeout_seconds = remaining
            details = self._adapter.query(
                access.route, access.credential, order_no, request_timeout_seconds=request_timeout_seconds
            )
        except (BookingApiError, BusinessApiError) as error:
            return self._error_result(error, locator)
        return self._result_for_details(details, local_order, locator)

    def _load_local_order(self, order_no: str) -> OrderState | None:
        try:
            return self._booking_store.load_order(order_no)
        except BookingStoreError:
            return None

    def _result_for_details(
        self, details: OrderDetails, local_order: OrderState | None, locator: dict[str, object]
    ) -> CommandResult:
        airline_pnrs = tuple(dict.fromkeys(details.airline_pnrs))
        ticket_numbers = tuple(dict.fromkeys(details.ticket_numbers))
        data: dict[str, object] = {
            **locator,
            "airline_pnrs": list(airline_pnrs),
            "ticket_numbers": list(ticket_numbers),
        }
        if local_order is not None:
            data["passengers"] = [item.model_dump(mode="json") for item in local_order.summary.passengers]

        if details.order_status == "-3":
            self._persist_ticketing(
                local_order, details.order_no, TicketingState.CANCELLED, airline_pnrs, ticket_numbers
            )
            return terminal_error_result("ORDER_CANCELLED", "Order was cancelled", data=locator)
        if details.order_status == "0":
            return self._unpaid_result(local_order, details.order_no, locator, airline_pnrs, ticket_numbers)

        required_ticket_count = len(local_order.summary.passengers) if local_order is not None else 0
        ticketed = (
            details.order_status == "2"
            and details.ticket_status == "1"
            and len(ticket_numbers) >= required_ticket_count
        )
        if ticketed:
            if local_order is not None:
                self._persist_payment(details.order_no, PaymentState.PAID)
            self._persist_ticketing(
                local_order, details.order_no, TicketingState.TICKETED, airline_pnrs, ticket_numbers
            )
            return success_result("TICKETED", "Tickets have been issued", data=data)

        if local_order is not None:
            self._persist_payment(details.order_no, PaymentState.PROCESSING)
        self._persist_ticketing(local_order, details.order_no, TicketingState.PENDING, airline_pnrs, ticket_numbers)
        return success_result("TICKETING_PENDING", "Ticketing is still pending", data=data)

    def _unpaid_result(
        self,
        local_order: OrderState | None,
        order_no: str,
        locator: dict[str, object],
        airline_pnrs: tuple[str, ...],
        ticket_numbers: tuple[str, ...],
    ) -> CommandResult:
        self._persist_ticketing(local_order, order_no, TicketingState.PENDING, airline_pnrs, ticket_numbers)
        state = local_order.payment_state if local_order is not None else None
        if state is PaymentState.UNAVAILABLE:
            return terminal_error_result(
                "PAYMENT_METHOD_UNAVAILABLE", "Balance payment is unavailable for this order", data=locator
            )
        if state is PaymentState.AWAITING_CONFIRMATION:
            return action_required_result(
                "PAYMENT_CONFIRMATION_REQUIRED", "Review the current payment summary before paying", data=locator
            )
        if local_order is not None:
            self._persist_payment(order_no, PaymentState.UNKNOWN)
        return action_required_result("PAYMENT_STATUS_UNKNOWN", "Payment status could not be confirmed", data=locator)

    def _persist_payment(self, order_no: str, state: PaymentState) -> None:
        with suppress(BookingStoreError):
            self._booking_store.update_payment(order_no, state)

    def _persist_ticketing(
        self,
        local_order: OrderState | None,
        order_no: str,
        state: TicketingState,
        airline_pnrs: tuple[str, ...],
        ticket_numbers: tuple[str, ...],
    ) -> None:
        if local_order is None:
            return
        with suppress(BookingStoreError):
            self._booking_store.update_ticketing(
                order_no, state, airline_pnrs=airline_pnrs, ticket_numbers=ticket_numbers
            )

    def _error_result(self, error: Exception, locator: dict[str, object]) -> CommandResult:
        if isinstance(error, SecureStoreError):
            error = AccessManagerError(
                code="SECURE_STORE_UNAVAILABLE", message="Secure credential storage is unavailable"
            )
        return booking_error_result(error, data=locator)

    def _pending_result(self, order_no: str) -> CommandResult:
        return success_result(
            "TICKETING_PENDING",
            "Ticketing is still pending",
            data=self._locator(order_no),
        )

    @staticmethod
    def _should_continue_polling(result: CommandResult) -> bool:
        return result.code == "TICKETING_PENDING" or result.code in _POLL_TRANSIENT_CODES

    def _locator(self, order_no: str) -> dict[str, object]:
        locator: dict[str, object] = {"order_no": order_no}
        order_url = self._order_url(order_no)
        if order_url is not None:
            locator["order_url"] = order_url
        return locator
