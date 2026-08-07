"""One-shot Atlas order creation and payment-confirmation issuance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from atlas_cli.access import AccessManager, AccessManagerError, TransactionAccess
from atlas_cli.booking_models import (
    AncillaryKind,
    BaggageOption,
    BookingContext,
    OrderState,
    PaymentConfirmationSeed,
    PaymentState,
    PaymentSummary,
    SeatOption,
    SelectedAncillarySummary,
)
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.business_client import AtlasBusinessClient, BusinessApiError, BusinessResponse
from atlas_cli.business_status import BookingApiError, BusinessStage, booking_error_result, map_business_status
from atlas_cli.endpoints import BusinessOperation, BusinessRoute
from atlas_cli.models import CommandResult, action_required_result, terminal_error_result
from atlas_cli.passengers import (
    PassengerInput,
    PassengerInputError,
    PassengerSource,
    load_passenger_input,
    masked_summary,
    to_order_payload,
    validate_requirements,
)
from atlas_cli.secure_store import ApiCredential, Credentials, SecureStoreError

SEAT_POLICIES: dict[str, str] = {
    "continue-without-seat": "STOP_SEAT",
    "cancel-order": "STOP_TICKET",
    "accept-similar-seat": "SIMILAR_SEAT",
}


def _now() -> datetime:
    return datetime.now(UTC)


class CreatedOrder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_no: str
    total_price: float
    transaction_fee: float
    currency: str
    payment_deadline: datetime
    balance_payment_available: bool

    @classmethod
    def from_response(cls, response: BusinessResponse) -> CreatedOrder:
        return cls(
            order_no=required_order_string(response.data, "orderNo", response.request_id),
            total_price=required_nonnegative_amount(response.data, "totalPrice", response.request_id),
            transaction_fee=required_nonnegative_amount(response.data, "totalTransactionFee", response.request_id),
            currency=required_order_string(response.data, "currency", response.request_id),
            payment_deadline=parse_sgt_deadline(response.data.get("tktLimitTime"), response.request_id),
            balance_payment_available=has_balance_payment(response.data.get("paymentOptions")),
        )


def required_order_string(data: dict[str, object], key: str, request_id: str | None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BookingApiError.invalid_response(request_id)
    return value


def required_nonnegative_amount(data: dict[str, object], key: str, request_id: str | None) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise BookingApiError.invalid_response(request_id)
    return float(value)


def parse_sgt_deadline(value: object, request_id: str | None) -> datetime:
    if not isinstance(value, str):
        raise BookingApiError.invalid_response(request_id)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise BookingApiError.invalid_response(request_id) from error
    return parsed.replace(tzinfo=ZoneInfo("Asia/Singapore"))


def has_balance_payment(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict) and item.get("paymentMethod") == 1 and not isinstance(item.get("paymentMethod"), bool)
        for item in value
    )


class BusinessClient(Protocol):
    def post(self, route: BusinessRoute, credential: ApiCredential, payload: dict[str, object]) -> BusinessResponse: ...


class OrderAdapter:
    def __init__(self, business: BusinessClient | AtlasBusinessClient) -> None:
        self._business = business

    def create(self, route: BusinessRoute, credential: ApiCredential, payload: dict[str, object]) -> CreatedOrder:
        response = self._business.post(route, credential, payload)
        meaning = map_business_status(BusinessStage.ORDER, response.status)
        if meaning is not None:
            raise BookingApiError.from_meaning(
                meaning,
                request_id=response.request_id,
                upstream_status=response.status,
            )
        return CreatedOrder.from_response(response)


class ControlCredentialStore(Protocol):
    def load_credentials(self) -> Credentials | None: ...


class TransactionAccessResolver(Protocol):
    def resolve_transaction_access(self, jwt: str, operation: BusinessOperation) -> TransactionAccess: ...


class OrderGateway(Protocol):
    def create(self, route: BusinessRoute, credential: ApiCredential, payload: dict[str, object]) -> CreatedOrder: ...


class OrderContextStore(Protocol):
    def load(self, booking_id: str, *, generation: str) -> BookingContext: ...

    def begin_order(self, booking_id: str, *, generation: str) -> BookingContext: ...

    def reset_order_attempt(self, booking_id: str, *, generation: str) -> BookingContext: ...

    def mark_order_unknown(self, booking_id: str, *, generation: str) -> BookingContext: ...

    def expire_context(self, booking_id: str, *, generation: str) -> BookingContext: ...

    def save_order(self, booking_id: str, order: OrderState, *, generation: str) -> BookingContext: ...

    def save_order_with_confirmation(
        self, booking_id: str, order: OrderState, seed: PaymentConfirmationSeed, *, generation: str
    ) -> tuple[BookingContext, object]: ...


class OrderService:
    def __init__(
        self,
        *,
        secrets: ControlCredentialStore,
        access: TransactionAccessResolver | AccessManager,
        adapter: OrderGateway | OrderAdapter,
        booking_store: OrderContextStore | BookingStore,
        order_url: Callable[[str], str | None] | None = None,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self._secrets = secrets
        self._access = access
        self._adapter = adapter
        self._booking_store = booking_store
        self._order_url = order_url or self._order_url_from_access(access)
        self._now = now

    def create(self, booking_id: str, source: PassengerSource, seat_policy: str | None) -> CommandResult:
        access: TransactionAccess | None = None
        began = False
        order_call_started = False
        created_order_state: OrderState | None = None
        try:
            access, context = self._access_context(booking_id)
            self._reject_fr(context)
            if context.price_change == "increased" and not context.increased_price_confirmed:
                raise BookingStoreError(
                    code="PRICE_CONFIRMATION_REQUIRED",
                    message="Confirm the increased price before creating an order",
                )
            passenger_input = load_passenger_input(source)
            validate_requirements(passenger_input, context.requirements, context.travelers, today=self._now().date())
            if self._has_selected_seat(context) and seat_policy not in SEAT_POLICIES:
                raise PassengerInputError(
                    code="INVALID_ARGUMENT",
                    message="A valid seat policy is required when a seat is selected",
                    fields=("seat_policy",),
                )
            context = self._booking_store.begin_order(booking_id, generation=access.route.generation)
            began = True
            self._validate_locked_context(context, passenger_input, seat_policy)
            payload = self._payload(context, passenger_input, seat_policy)
            order_call_started = True
            created = self._adapter.create(access.route, access.credential, payload)
            order = self._order_state(context, passenger_input, created)
            created_order_state = order
            if order.payment_deadline <= self._now():
                expired_order = order.model_copy(update={"payment_state": PaymentState.UNAVAILABLE})
                self._booking_store.save_order(booking_id, expired_order, generation=access.route.generation)
                return terminal_error_result(
                    "PAYMENT_DEADLINE_EXPIRED",
                    "Payment deadline expired",
                    data=self._order_locator(expired_order),
                )
            if not created.balance_payment_available:
                self._booking_store.save_order(booking_id, order, generation=access.route.generation)
                return terminal_error_result(
                    "PAYMENT_METHOD_UNAVAILABLE",
                    "Balance payment is unavailable for this order",
                    data=self._order_locator(order),
                )
            _, confirmation = self._booking_store.save_order_with_confirmation(
                booking_id,
                order,
                PaymentConfirmationSeed(
                    order_no=order.order_no,
                    summary_digest=order.summary_digest,
                    expires_at=order.payment_deadline,
                ),
                generation=access.route.generation,
            )
            confirmation_id = getattr(confirmation, "confirmation_id", None)
            if not isinstance(confirmation_id, str):
                raise BookingStoreError(
                    code="BOOKING_STATE_INVALID", message="Saved booking state could not be processed"
                )
            return action_required_result(
                "PAYMENT_CONFIRMATION_REQUIRED",
                "Review the current payment summary before paying",
                data={**self._public_order(order), "payment_confirmation_id": confirmation_id},
            )
        except (AccessManagerError, BookingStoreError, PassengerInputError, SecureStoreError) as error:
            if began and not order_call_started and access is not None:
                self._booking_store.reset_order_attempt(booking_id, generation=access.route.generation)
                return self._error_result(error)
            if began and access is not None and self._is_local_save_failure(error):
                data = self._order_locator(created_order_state) if created_order_state is not None else None
                return self._unknown_result(error, access, booking_id, data=data)
            return self._error_result(error)
        except (BookingApiError, BusinessApiError) as error:
            if access is None or not began:
                return self._error_result(error)
            return self._handle_post_attempt_error(error, access, booking_id)

    @staticmethod
    def _order_url_from_access(access: object) -> Callable[[str], str | None]:
        candidate = getattr(access, "order_url", None)
        if callable(candidate):
            return cast(Callable[[str], str | None], candidate)
        return lambda order_no: f"https://www.atriptech.com/#/order/detail/{order_no}/en"

    def _access_context(self, booking_id: str) -> tuple[TransactionAccess, BookingContext]:
        credentials = self._secrets.load_credentials()
        if credentials is None or not credentials.jwt.strip():
            raise AccessManagerError(code="AUTHORIZATION_REQUIRED", message="Authorization required")
        access = self._access.resolve_transaction_access(credentials.jwt, BusinessOperation.ORDER)
        return access, self._booking_store.load(booking_id, generation=access.route.generation)

    @staticmethod
    def _reject_fr(context: BookingContext) -> None:
        if any(item.segment.carrier == "FR" or item.segment.operating_carrier == "FR" for item in context.segments):
            raise BookingStoreError(code="OFFER_EXPIRED", message="Offer expired; search again")

    @staticmethod
    def _has_selected_seat(context: BookingContext) -> bool:
        return any(item.kind == AncillaryKind.SEAT for item in context.selections)

    def _validate_locked_context(
        self,
        context: BookingContext,
        passenger_input: PassengerInput,
        seat_policy: str | None,
    ) -> None:
        self._reject_fr(context)
        if context.price_change == "increased" and not context.increased_price_confirmed:
            raise BookingStoreError(
                code="PRICE_CONFIRMATION_REQUIRED",
                message="Confirm the increased price before creating an order",
            )
        validate_requirements(passenger_input, context.requirements, context.travelers, today=self._now().date())
        if self._has_selected_seat(context) and seat_policy not in SEAT_POLICIES:
            raise PassengerInputError(
                code="INVALID_ARGUMENT",
                message="A valid seat policy is required when a seat is selected",
                fields=("seat_policy",),
            )

    def _payload(
        self,
        context: BookingContext,
        passenger_input: PassengerInput,
        seat_policy: str | None,
    ) -> dict[str, object]:
        payload = {"sessionId": context.session_id, **to_order_payload(passenger_input, context)}
        if self._has_selected_seat(context):
            assert seat_policy is not None
            payload["ifSeatOccupied"] = SEAT_POLICIES[seat_policy]
        return payload

    def _order_state(
        self,
        context: BookingContext,
        passenger_input: PassengerInput,
        created: CreatedOrder,
    ) -> OrderState:
        summary = self._summary(context, passenger_input, created)
        digest = hashlib.sha256(self._canonical_json(summary).encode("utf-8")).hexdigest()
        return OrderState(
            order_no=created.order_no,
            order_url=self._order_url(created.order_no),
            total_price=created.total_price,
            transaction_fee=created.transaction_fee,
            currency=created.currency,
            payment_deadline=created.payment_deadline,
            summary=summary,
            summary_digest=digest,
            payment_state=(
                PaymentState.AWAITING_CONFIRMATION if created.balance_payment_available else PaymentState.UNAVAILABLE
            ),
        )

    def _summary(
        self,
        context: BookingContext,
        passenger_input: PassengerInput,
        created: CreatedOrder,
    ) -> PaymentSummary:
        selected = self._selected_summaries(context)
        baggage_total = sum(item.price for item in selected if item.kind == AncillaryKind.BAGGAGE)
        seat_total = sum(item.price for item in selected if item.kind == AncillaryKind.SEAT)

        return PaymentSummary(
            ticket_price=created.total_price - baggage_total - seat_total,
            baggage_total=baggage_total,
            seat_total=seat_total,
            total_price=created.total_price,
            currency=created.currency,
            passengers=masked_summary(passenger_input).passengers,
            ancillaries=selected,
            price_change=context.price_change,
            previous_offer_total=context.searched_offer.total_price,
            current_offer_total=context.verified_offer.total_price,
        )

    @staticmethod
    def _selected_summaries(context: BookingContext) -> tuple[SelectedAncillarySummary, ...]:
        selected: list[SelectedAncillarySummary] = []
        baggage = {item.baggage_id: item for item in context.baggage_options}
        seats = {item.seat_id: item for item in context.seat_options}
        for selection in context.selections:
            option: BaggageOption | SeatOption | None = (
                baggage.get(selection.option_id)
                if selection.kind is AncillaryKind.BAGGAGE
                else seats.get(selection.option_id)
            )
            if option is None:
                raise BookingStoreError(
                    code="ANCILLARY_SELECTION_INVALID", message="Selected optional service is no longer available"
                )
            description = (
                f"Baggage: {option.piece} piece, {option.weight_kg} kg"
                if isinstance(option, BaggageOption)
                else f"Seat {option.row}{option.column}"
            )
            selected.append(
                SelectedAncillarySummary(
                    kind=selection.kind,
                    traveler_id=selection.traveler_id,
                    segment_id=selection.segment_id,
                    description=description,
                    price=option.price,
                    currency=option.currency,
                )
            )
        return tuple(selected)

    @staticmethod
    def _canonical_json(summary: PaymentSummary) -> str:
        return json.dumps(summary.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _order_locator(order: OrderState) -> dict[str, object]:
        locator: dict[str, object] = {"order_no": order.order_no}
        if order.order_url is not None:
            locator["order_url"] = order.order_url
        return locator

    def _public_order(self, order: OrderState) -> dict[str, object]:
        return {
            **self._order_locator(order),
            "total_price": order.total_price,
            "transaction_fee": order.transaction_fee,
            "currency": order.currency,
            "payment_deadline": order.payment_deadline.isoformat(),
            "payment_summary": order.summary.model_dump(mode="json"),
        }

    def _handle_post_attempt_error(self, error: Exception, access: TransactionAccess, booking_id: str) -> CommandResult:
        if getattr(error, "upstream_status", None) == 308:
            self._booking_store.expire_context(booking_id, generation=access.route.generation)
            return self._error_result(error)
        uncertain = (
            isinstance(error, BusinessApiError)
            or getattr(error, "side_effect_uncertain", False)
            or getattr(error, "code", None) == "SERVICE_RESPONSE_INVALID"
        )
        if uncertain:
            return self._unknown_result(error, access, booking_id)
        self._booking_store.reset_order_attempt(booking_id, generation=access.route.generation)
        return self._error_result(error)

    def _unknown_result(
        self,
        error: Exception,
        access: TransactionAccess,
        booking_id: str,
        *,
        data: dict[str, object] | None = None,
    ) -> CommandResult:
        self._booking_store.mark_order_unknown(booking_id, generation=access.route.generation)
        if isinstance(error, BookingApiError) and error.side_effect_uncertain:
            return self._error_result(error)
        meaning = map_business_status(BusinessStage.ORDER, 330)
        assert meaning is not None
        return booking_error_result(
            BookingApiError.from_meaning(meaning, request_id=getattr(error, "request_id", None)),
            data=data,
        )

    @staticmethod
    def _is_local_save_failure(error: Exception) -> bool:
        return isinstance(error, BookingStoreError) and error.code not in {
            "PRICE_CONFIRMATION_REQUIRED",
            "OFFER_EXPIRED",
        }

    @staticmethod
    def _error_result(error: Exception) -> CommandResult:
        if isinstance(error, SecureStoreError):
            error = AccessManagerError(
                code="SECURE_STORE_UNAVAILABLE", message="Secure credential storage is unavailable"
            )
        details: dict[str, object] = {}
        if isinstance(error, PassengerInputError):
            details["fields"] = list(error.fields)
        if isinstance(error, BookingApiError):
            fields = None
            if error.upstream_status == 323:
                fields = ["contact.email"]
            elif error.upstream_status == 410:
                fields = ["contact"]
            if fields is not None:
                details["fields"] = fields
        if isinstance(error, AccessManagerError):
            url = error.details.get("url")
            if isinstance(url, str) and url:
                details["url"] = url
        return booking_error_result(error, details=details)
