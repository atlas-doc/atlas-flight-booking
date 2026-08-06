from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.cli import app
from atlas_cli.models import action_required_result, success_result

runner = CliRunner()


class FakeVerifyService:
    def verify(self, offer_id: str):
        return success_result(
            "OFFER_VERIFIED",
            "Offer verified",
            data={"booking_id": f"book_{offer_id}", "price_change": "unchanged"},
        )

    def confirm_price(self, booking_id: str):
        return success_result(
            "PRICE_CONFIRMED",
            "Price confirmed",
            data={"booking_id": booking_id, "price_change": "increased"},
        )


class FakeAncillaryService:
    pass


class FakeOrderService:
    pass


class FakeTicketingService:
    pass


class FakePaymentService:
    pass


def runtime() -> BookingRuntime:
    return BookingRuntime(
        verify=FakeVerifyService(),  # type: ignore[arg-type]
        ancillaries=FakeAncillaryService(),  # type: ignore[arg-type]
        orders=FakeOrderService(),  # type: ignore[arg-type]
        ticketing=FakeTicketingService(),  # type: ignore[arg-type]
        payments=FakePaymentService(),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("args", "code", "booking_id"),
    [
        (("offer", "verify", "--offer-id", "opaque", "--json"), "OFFER_VERIFIED", "book_opaque"),
        (("booking", "confirm-price", "--booking-id", "book_opaque", "--json"), "PRICE_CONFIRMED", "book_opaque"),
    ],
)
def test_booking_cli_commands_emit_one_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    code: str,
    booking_id: str,
) -> None:
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", runtime)

    result = runner.invoke(app, list(args))

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == code
    assert payload["data"]["booking_id"] == booking_id


@pytest.mark.parametrize(
    "args",
    [
        ("offer", "verify", "--json"),
        ("booking", "confirm-price", "--json"),
    ],
)
def test_booking_cli_missing_required_identifier_is_stable_json(args: tuple[str, ...]) -> None:
    result = runner.invoke(app, list(args))

    assert result.exit_code == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_booking_cli_expected_service_error_does_not_use_internal_error_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpectedErrorService(FakeVerifyService):
        def verify(self, offer_id: str):
            del offer_id
            return action_required_result(
                "SUBSCRIPTION_REQUIRED",
                "Subscription required",
                details={"url": "https://subscribe.example.invalid"},
            )

    monkeypatch.setattr(
        "atlas_cli.cli.build_booking_runtime",
        lambda: BookingRuntime(
            verify=ExpectedErrorService(),  # type: ignore[arg-type]
            ancillaries=FakeAncillaryService(),  # type: ignore[arg-type]
            orders=FakeOrderService(),  # type: ignore[arg-type]
            ticketing=FakeTicketingService(),  # type: ignore[arg-type]
            payments=FakePaymentService(),  # type: ignore[arg-type]
        ),
    )

    result = runner.invoke(app, ["offer", "verify", "--offer-id", "opaque", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["code"] == "SUBSCRIPTION_REQUIRED"
    assert payload["details"] == {"url": "https://subscribe.example.invalid"}
