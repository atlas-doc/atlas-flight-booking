"""Endpoint-specific booking-status meanings and safe public error rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from atlas_cli.models import CommandResult, CommandStatus


class BusinessStage(StrEnum):
    VERIFY = "verify"
    BAGGAGE = "baggage"
    SEAT = "seat"
    ORDER = "order"
    PAY = "pay"
    QUERY = "query"


@dataclass(frozen=True)
class StatusMeaning:
    code: str
    message: str
    status: CommandStatus
    retryable: bool = False
    side_effect_uncertain: bool = False


PUBLIC_MESSAGES: dict[str, str] = {
    "OFFER_EXPIRED": "Offer expired",
    "FLIGHT_UNAVAILABLE": "Flight is currently unavailable",
    "PRICE_VERIFICATION_UNAVAILABLE": "Price verification is temporarily unavailable",
    "BOOKING_INPUT_INVALID": "Booking information could not be accepted",
    "BOOKING_EXPIRED": "Booking context expired",
    "SEAT_UNAVAILABLE": "Seat selection is unavailable",
    "BAGGAGE_UNAVAILABLE": "Baggage selection is unavailable",
    "ORDER_CREATION_UNAVAILABLE": "Order could not be created",
    "ORDER_CREATION_UNKNOWN": "Order creation status could not be confirmed",
    "PRICE_CHANGED": "Price changed",
    "ANCILLARY_SELECTION_INVALID": "Selected optional service is no longer available",
    "PASSENGER_INFO_INVALID": "Passenger information could not be accepted",
    "PASSENGER_COMBINATION_UNSUPPORTED": "Passenger combination is unavailable",
    "DUPLICATE_BOOKING_SUSPECTED": "An existing booking may already exist",
    "PAYMENT_METHOD_UNAVAILABLE": "Balance payment is unavailable for this order",
    "PAYMENT_DEADLINE_EXPIRED": "Payment deadline expired",
    "PAYMENT_STATUS_UNKNOWN": "Payment status could not be confirmed",
    "PAYMENT_PROCESSING": "Payment is processing",
    "ORDER_NOT_FOUND": "Order could not be found",
    "ORDER_STATUS_UNAVAILABLE": "Order status is temporarily unavailable",
    "UNSUPPORTED_BOOKING_FLOW": "This booking flow is unavailable",
    "SERVICE_REQUEST_FAILED": "Service request could not be completed",
    "SERVICE_RESPONSE_INVALID": "Service response could not be processed",
}


def terminal(code: str) -> StatusMeaning:
    return StatusMeaning(code, PUBLIC_MESSAGES[code], CommandStatus.TERMINAL_ERROR)


def action(code: str, *, side_effect_uncertain: bool = False) -> StatusMeaning:
    return StatusMeaning(
        code,
        PUBLIC_MESSAGES[code],
        CommandStatus.ACTION_REQUIRED,
        side_effect_uncertain=side_effect_uncertain,
    )


def success(code: str) -> StatusMeaning:
    return StatusMeaning(code, PUBLIC_MESSAGES[code], CommandStatus.SUCCESS)


def retryable(code: str) -> StatusMeaning:
    return StatusMeaning(code, PUBLIC_MESSAGES[code], CommandStatus.RETRYABLE_ERROR, retryable=True)


STATUS_MEANINGS: dict[tuple[BusinessStage, int], StatusMeaning] = {
    (BusinessStage.VERIFY, 200): terminal("OFFER_EXPIRED"),
    (BusinessStage.VERIFY, 201): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.VERIFY, 202): terminal("OFFER_EXPIRED"),
    (BusinessStage.VERIFY, 203): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.VERIFY, 205): retryable("PRICE_VERIFICATION_UNAVAILABLE"),
    (BusinessStage.VERIFY, 207): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.VERIFY, 210): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.VERIFY, 212): terminal("BOOKING_INPUT_INVALID"),
    (BusinessStage.VERIFY, 213): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.VERIFY, 222): retryable("PRICE_VERIFICATION_UNAVAILABLE"),
    (BusinessStage.VERIFY, 299): retryable("PRICE_VERIFICATION_UNAVAILABLE"),
    (BusinessStage.VERIFY, 429): retryable("PRICE_VERIFICATION_UNAVAILABLE"),
    (BusinessStage.SEAT, 214): terminal("BOOKING_EXPIRED"),
    (BusinessStage.SEAT, 215): terminal("SERVICE_REQUEST_FAILED"),
    (BusinessStage.SEAT, 216): retryable("SEAT_UNAVAILABLE"),
    (BusinessStage.SEAT, 217): retryable("SEAT_UNAVAILABLE"),
    (BusinessStage.SEAT, 218): success("SEAT_UNAVAILABLE"),
    (BusinessStage.SEAT, 219): success("SEAT_UNAVAILABLE"),
    (BusinessStage.SEAT, 220): terminal("SERVICE_REQUEST_FAILED"),
    (BusinessStage.SEAT, 221): success("SEAT_UNAVAILABLE"),
    (BusinessStage.SEAT, 223): success("SEAT_UNAVAILABLE"),
    (BusinessStage.SEAT, 429): retryable("SEAT_UNAVAILABLE"),
    (BusinessStage.BAGGAGE, 205): retryable("BAGGAGE_UNAVAILABLE"),
    (BusinessStage.BAGGAGE, 212): terminal("SERVICE_REQUEST_FAILED"),
    (BusinessStage.BAGGAGE, 214): success("BAGGAGE_UNAVAILABLE"),
    (BusinessStage.BAGGAGE, 299): retryable("BAGGAGE_UNAVAILABLE"),
    (BusinessStage.BAGGAGE, 429): retryable("BAGGAGE_UNAVAILABLE"),
    (BusinessStage.BAGGAGE, 9999): retryable("BAGGAGE_UNAVAILABLE"),
    (BusinessStage.ORDER, 301): terminal("BOOKING_EXPIRED"),
    (BusinessStage.ORDER, 302): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.ORDER, 303): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.ORDER, 304): terminal("ORDER_CREATION_UNAVAILABLE"),
    (BusinessStage.ORDER, 305): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.ORDER, 307): terminal("BOOKING_INPUT_INVALID"),
    (BusinessStage.ORDER, 308): action("PRICE_CHANGED"),
    (BusinessStage.ORDER, 309): action("ANCILLARY_SELECTION_INVALID"),
    (BusinessStage.ORDER, 310): terminal("PASSENGER_COMBINATION_UNSUPPORTED"),
    (BusinessStage.ORDER, 312): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.ORDER, 313): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.ORDER, 315): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.ORDER, 316): terminal("ORDER_CREATION_UNAVAILABLE"),
    (BusinessStage.ORDER, 317): terminal("ORDER_CREATION_UNAVAILABLE"),
    (BusinessStage.ORDER, 318): action("DUPLICATE_BOOKING_SUSPECTED", side_effect_uncertain=True),
    (BusinessStage.ORDER, 319): terminal("FLIGHT_UNAVAILABLE"),
    (BusinessStage.ORDER, 320): action("ANCILLARY_SELECTION_INVALID"),
    (BusinessStage.ORDER, 321): terminal("ORDER_CREATION_UNAVAILABLE"),
    (BusinessStage.ORDER, 322): action("ANCILLARY_SELECTION_INVALID"),
    (BusinessStage.ORDER, 323): action("PASSENGER_INFO_INVALID"),
    (BusinessStage.ORDER, 324): terminal("ORDER_CREATION_UNAVAILABLE"),
    (BusinessStage.ORDER, 325): terminal("PASSENGER_COMBINATION_UNSUPPORTED"),
    (BusinessStage.ORDER, 326): terminal("UNSUPPORTED_BOOKING_FLOW"),
    (BusinessStage.ORDER, 327): action("PASSENGER_INFO_INVALID"),
    (BusinessStage.ORDER, 328): action("ANCILLARY_SELECTION_INVALID"),
    (BusinessStage.ORDER, 329): terminal("PAYMENT_METHOD_UNAVAILABLE"),
    (BusinessStage.ORDER, 330): action("ORDER_CREATION_UNKNOWN", side_effect_uncertain=True),
    (BusinessStage.ORDER, 407): action("PASSENGER_INFO_INVALID"),
    (BusinessStage.ORDER, 408): action("PASSENGER_COMBINATION_UNSUPPORTED"),
    (BusinessStage.ORDER, 409): action("ANCILLARY_SELECTION_INVALID"),
    (BusinessStage.ORDER, 410): action("PASSENGER_INFO_INVALID"),
    (BusinessStage.PAY, 400): terminal("BOOKING_INPUT_INVALID"),
    (BusinessStage.PAY, 401): terminal("PAYMENT_DEADLINE_EXPIRED"),
    (BusinessStage.PAY, 402): action("PAYMENT_STATUS_UNKNOWN"),
    (BusinessStage.PAY, 403): terminal("PAYMENT_METHOD_UNAVAILABLE"),
    (BusinessStage.PAY, 404): action("PAYMENT_STATUS_UNKNOWN"),
    (BusinessStage.PAY, 406): success("PAYMENT_PROCESSING"),
    (BusinessStage.PAY, 407): terminal("BOOKING_INPUT_INVALID"),
    (BusinessStage.PAY, 408): terminal("PASSENGER_COMBINATION_UNSUPPORTED"),
    (BusinessStage.PAY, 409): terminal("BOOKING_INPUT_INVALID"),
    (BusinessStage.PAY, 410): terminal("BOOKING_INPUT_INVALID"),
    (BusinessStage.PAY, 411): action("PAYMENT_STATUS_UNKNOWN"),
    (BusinessStage.PAY, 412): terminal("PAYMENT_METHOD_UNAVAILABLE"),
    (BusinessStage.PAY, 413): terminal("UNSUPPORTED_BOOKING_FLOW"),
    (BusinessStage.PAY, 414): terminal("UNSUPPORTED_BOOKING_FLOW"),
    (BusinessStage.PAY, 415): terminal("UNSUPPORTED_BOOKING_FLOW"),
    (BusinessStage.PAY, 615): success("PAYMENT_PROCESSING"),
    (BusinessStage.QUERY, 701): terminal("SERVICE_RESPONSE_INVALID"),
    (BusinessStage.QUERY, 702): terminal("SERVICE_RESPONSE_INVALID"),
    (BusinessStage.QUERY, 703): terminal("ORDER_NOT_FOUND"),
    (BusinessStage.QUERY, 704): terminal("SERVICE_RESPONSE_INVALID"),
    (BusinessStage.QUERY, 705): retryable("ORDER_STATUS_UNAVAILABLE"),
    (BusinessStage.QUERY, 800): terminal("ORDER_NOT_FOUND"),
}


class BookingApiError(RuntimeError):
    def __init__(
        self,
        meaning: StatusMeaning,
        *,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(meaning.message)
        self.code = meaning.code
        self.message = meaning.message
        self.status = meaning.status
        self.retryable = meaning.retryable
        self.side_effect_uncertain = meaning.side_effect_uncertain
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        self.upstream_status = upstream_status

    @classmethod
    def from_meaning(
        cls,
        meaning: StatusMeaning,
        *,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
        upstream_status: int | None = None,
    ) -> BookingApiError:
        return cls(
            meaning,
            request_id=request_id,
            retry_after_seconds=retry_after_seconds,
            upstream_status=upstream_status,
        )

    @classmethod
    def invalid_response(cls, request_id: str | None = None) -> BookingApiError:
        return cls(
            StatusMeaning(
                "SERVICE_RESPONSE_INVALID",
                "Service response could not be processed",
                CommandStatus.TERMINAL_ERROR,
            ),
            request_id=request_id,
        )


def map_business_status(stage: BusinessStage, upstream_status: int) -> StatusMeaning | None:
    if upstream_status == 0:
        return None
    known = STATUS_MEANINGS.get((stage, upstream_status))
    if known is not None:
        return known
    if stage is BusinessStage.ORDER:
        return StatusMeaning(
            "ORDER_CREATION_UNKNOWN",
            "Order creation status could not be confirmed",
            CommandStatus.ACTION_REQUIRED,
            side_effect_uncertain=True,
        )
    if stage is BusinessStage.PAY:
        return StatusMeaning(
            "PAYMENT_STATUS_UNKNOWN",
            "Payment status could not be confirmed",
            CommandStatus.ACTION_REQUIRED,
            side_effect_uncertain=True,
        )
    return StatusMeaning(
        "SERVICE_REQUEST_FAILED",
        "Service request could not be completed",
        CommandStatus.TERMINAL_ERROR,
    )


ACTION_REQUIRED_CODES = {
    "AUTHORIZATION_REQUIRED",
    "SUBSCRIPTION_REQUIRED",
    "PRICE_CONFIRMATION_REQUIRED",
    "PAYMENT_CONFIRMATION_REQUIRED",
    "PASSENGER_INFO_REQUIRED",
    "PRICE_CHANGED",
    "ANCILLARY_SELECTION_INVALID",
    "PASSENGER_INFO_INVALID",
    "DUPLICATE_BOOKING_SUSPECTED",
    "ORDER_CREATION_UNKNOWN",
    "PAYMENT_STATUS_UNKNOWN",
}


def booking_error_result(
    error: Exception,
    *,
    data: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
) -> CommandResult:
    code = str(getattr(error, "code", "SERVICE_REQUEST_FAILED"))
    message = str(getattr(error, "message", "Service request could not be completed"))
    request_id = getattr(error, "request_id", None)
    status = getattr(error, "status", None)
    retryable_error = bool(getattr(error, "retryable", False))
    if not isinstance(status, CommandStatus):
        if code in ACTION_REQUIRED_CODES:
            status = CommandStatus.ACTION_REQUIRED
        elif retryable_error:
            status = CommandStatus.RETRYABLE_ERROR
        else:
            status = CommandStatus.TERMINAL_ERROR
    return CommandResult(
        status=status,
        code=code,
        message=message,
        retryable=retryable_error,
        request_id=request_id if isinstance(request_id, str) else None,
        data=data or {},
        details=details or {},
    )
