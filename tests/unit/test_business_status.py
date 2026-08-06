from __future__ import annotations

import pytest

from atlas_cli.access import AccessManagerError
from atlas_cli.api_client import ApiClientError
from atlas_cli.business_status import (
    BookingApiError,
    BusinessStage,
    booking_error_result,
    map_business_status,
)
from atlas_cli.models import CommandStatus

EXPECTED: dict[tuple[BusinessStage, int], tuple[str, CommandStatus, bool]] = {
    (BusinessStage.VERIFY, 200): ("OFFER_EXPIRED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 201): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 202): ("OFFER_EXPIRED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 203): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 205): ("PRICE_VERIFICATION_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.VERIFY, 207): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 210): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 212): ("BOOKING_INPUT_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 213): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.VERIFY, 222): ("PRICE_VERIFICATION_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.VERIFY, 299): ("PRICE_VERIFICATION_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.VERIFY, 429): ("PRICE_VERIFICATION_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.SEAT, 214): ("BOOKING_EXPIRED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.SEAT, 215): ("SERVICE_REQUEST_FAILED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.SEAT, 216): ("SEAT_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.SEAT, 217): ("SEAT_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.SEAT, 218): ("SEAT_UNAVAILABLE", CommandStatus.SUCCESS, False),
    (BusinessStage.SEAT, 219): ("SEAT_UNAVAILABLE", CommandStatus.SUCCESS, False),
    (BusinessStage.SEAT, 220): ("SERVICE_REQUEST_FAILED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.SEAT, 221): ("SEAT_UNAVAILABLE", CommandStatus.SUCCESS, False),
    (BusinessStage.SEAT, 223): ("SEAT_UNAVAILABLE", CommandStatus.SUCCESS, False),
    (BusinessStage.SEAT, 225): ("SEAT_UNAVAILABLE", CommandStatus.SUCCESS, False),
    (BusinessStage.SEAT, 429): ("SEAT_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.BAGGAGE, 205): ("BAGGAGE_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.BAGGAGE, 212): ("SERVICE_REQUEST_FAILED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.BAGGAGE, 214): ("BAGGAGE_UNAVAILABLE", CommandStatus.SUCCESS, False),
    (BusinessStage.BAGGAGE, 299): ("BAGGAGE_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.BAGGAGE, 429): ("BAGGAGE_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.BAGGAGE, 9999): ("BAGGAGE_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.ORDER, 301): ("BOOKING_EXPIRED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 302): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 303): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 304): ("ORDER_CREATION_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 305): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 307): ("BOOKING_INPUT_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 308): ("PRICE_CHANGED", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 309): ("ANCILLARY_SELECTION_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 310): ("PASSENGER_COMBINATION_UNSUPPORTED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 312): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 313): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 315): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 316): ("ORDER_CREATION_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 317): ("ORDER_CREATION_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 318): ("DUPLICATE_BOOKING_SUSPECTED", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 319): ("FLIGHT_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 320): ("ANCILLARY_SELECTION_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 321): ("ORDER_CREATION_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 322): ("ANCILLARY_SELECTION_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 323): ("CONTACT_INFO_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 324): ("ORDER_CREATION_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 325): ("PASSENGER_COMBINATION_UNSUPPORTED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 326): ("UNSUPPORTED_BOOKING_FLOW", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 327): ("PASSENGER_INFO_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 328): ("ANCILLARY_SELECTION_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 329): ("PAYMENT_METHOD_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.ORDER, 330): ("ORDER_CREATION_UNKNOWN", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 407): ("PASSENGER_INFO_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 408): ("PASSENGER_COMBINATION_UNSUPPORTED", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 409): ("ANCILLARY_SELECTION_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.ORDER, 410): ("CONTACT_INFO_INVALID", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.PAY, 400): ("BOOKING_INPUT_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 401): ("PAYMENT_DEADLINE_EXPIRED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 402): ("PAYMENT_STATUS_UNKNOWN", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.PAY, 403): ("PAYMENT_METHOD_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 404): ("PAYMENT_STATUS_UNKNOWN", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.PAY, 406): ("PAYMENT_PROCESSING", CommandStatus.SUCCESS, False),
    (BusinessStage.PAY, 407): ("BOOKING_INPUT_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 408): ("PASSENGER_COMBINATION_UNSUPPORTED", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 409): ("BOOKING_INPUT_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 410): ("BOOKING_INPUT_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 411): ("PAYMENT_STATUS_UNKNOWN", CommandStatus.ACTION_REQUIRED, False),
    (BusinessStage.PAY, 412): ("PAYMENT_METHOD_UNAVAILABLE", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 413): ("UNSUPPORTED_BOOKING_FLOW", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 414): ("UNSUPPORTED_BOOKING_FLOW", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 415): ("UNSUPPORTED_BOOKING_FLOW", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.PAY, 615): ("PAYMENT_PROCESSING", CommandStatus.SUCCESS, False),
    (BusinessStage.QUERY, 701): ("SERVICE_RESPONSE_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.QUERY, 702): ("SERVICE_RESPONSE_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.QUERY, 703): ("ORDER_NOT_FOUND", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.QUERY, 704): ("SERVICE_RESPONSE_INVALID", CommandStatus.TERMINAL_ERROR, False),
    (BusinessStage.QUERY, 705): ("ORDER_STATUS_UNAVAILABLE", CommandStatus.RETRYABLE_ERROR, True),
    (BusinessStage.QUERY, 800): ("ORDER_NOT_FOUND", CommandStatus.TERMINAL_ERROR, False),
}

EXPECTED_MESSAGES = {
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
    "CONTACT_INFO_INVALID": "Contact information could not be accepted",
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


@pytest.mark.parametrize(("stage", "upstream_status", "expected"), [(*key, value) for key, value in EXPECTED.items()])
def test_documented_business_statuses_map_to_public_meanings(
    stage: BusinessStage,
    upstream_status: int,
    expected: tuple[str, CommandStatus, bool],
) -> None:
    """Changing a documented status branch must change its public meaning."""
    meaning = map_business_status(stage, upstream_status)

    assert meaning is not None
    assert (meaning.code, meaning.status, meaning.retryable) == expected
    assert meaning.message == EXPECTED_MESSAGES[expected[0]]


@pytest.mark.parametrize("stage", list(BusinessStage))
def test_zero_business_status_alone_means_success(stage: BusinessStage) -> None:
    assert map_business_status(stage, 0) is None


@pytest.mark.parametrize("stage", [BusinessStage.VERIFY, BusinessStage.QUERY])
def test_unknown_verify_and_query_statuses_are_terminal_service_failures(stage: BusinessStage) -> None:
    meaning = map_business_status(stage, 999)

    assert meaning is not None
    assert (meaning.code, meaning.status, meaning.retryable, meaning.side_effect_uncertain) == (
        "SERVICE_REQUEST_FAILED",
        CommandStatus.TERMINAL_ERROR,
        False,
        False,
    )


@pytest.mark.parametrize(
    ("stage", "expected_code"),
    [(BusinessStage.ORDER, "ORDER_CREATION_UNKNOWN"), (BusinessStage.PAY, "PAYMENT_STATUS_UNKNOWN")],
)
def test_unknown_side_effecting_statuses_require_confirmation(stage: BusinessStage, expected_code: str) -> None:
    meaning = map_business_status(stage, 999)

    assert meaning is not None
    assert (meaning.code, meaning.status, meaning.retryable, meaning.side_effect_uncertain) == (
        expected_code,
        CommandStatus.ACTION_REQUIRED,
        False,
        True,
    )


@pytest.mark.parametrize("upstream_status", [318, 330])
def test_known_uncertain_order_statuses_require_confirmation(upstream_status: int) -> None:
    meaning = map_business_status(BusinessStage.ORDER, upstream_status)

    assert meaning is not None
    assert meaning.side_effect_uncertain is True


def test_booking_error_result_renders_access_requirements_as_action_required() -> None:
    result = booking_error_result(
        AccessManagerError(code="SUBSCRIPTION_REQUIRED", message="Subscription required"),
    )

    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.code == "SUBSCRIPTION_REQUIRED"
    assert result.message == "Subscription required"


def test_booking_error_result_renders_retryable_transport_error() -> None:
    result = booking_error_result(
        ApiClientError(code="TRANSPORT_UNAVAILABLE", message="Service unavailable", retryable=True),
    )

    assert result.status is CommandStatus.RETRYABLE_ERROR
    assert result.retryable is True


def test_booking_error_result_preserves_mapped_booking_status() -> None:
    meaning = map_business_status(BusinessStage.PAY, 406)
    assert meaning is not None

    result = booking_error_result(BookingApiError.from_meaning(meaning, upstream_status=406))

    assert result.status is CommandStatus.SUCCESS
    assert result.code == "PAYMENT_PROCESSING"
    assert result.data == {}
    assert result.details == {}


def test_booking_error_result_does_not_copy_arbitrary_exception_data_or_details() -> None:
    error = RuntimeError("private service response")
    error.code = "PRIVATE_ERROR"  # type: ignore[attr-defined]
    error.message = "Safe public message"  # type: ignore[attr-defined]
    error.upstream_status = 500  # type: ignore[attr-defined]
    error.details = {"private": "upstream payload"}  # type: ignore[attr-defined]
    error.data = {"private": "upstream payload"}  # type: ignore[attr-defined]

    result = booking_error_result(error)

    assert result.data == {}
    assert result.details == {}
    assert "upstream_status" not in result.data
    assert "upstream_status" not in result.details
