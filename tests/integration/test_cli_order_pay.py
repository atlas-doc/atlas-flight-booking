from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.cli import app
from atlas_cli.models import success_result

runner = CliRunner()


class Empty:
    pass


class FakePayments:
    def __init__(self) -> None:
        self.confirmation_ids: list[str] = []

    def pay(self, confirmation_id: str):
        self.confirmation_ids.append(confirmation_id)
        return success_result(
            "TICKETED",
            "Tickets have been issued",
            data={
                "order_no": "ATAXA20260721085144583",
                "order_url": "https://www.atriptech.com/#/order/detail/ATAXA20260721085144583/en",
            },
        )


def test_order_pay_accepts_only_confirmation_id_and_emits_one_safe_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payments = FakePayments()
    runtime = BookingRuntime(
        verify=Empty(),
        ancillaries=Empty(),
        orders=Empty(),
        ticketing=Empty(),
        payments=payments,
    )  # type: ignore[arg-type]
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    result = runner.invoke(app, ["order", "pay", "--confirmation-id", "paycfm_opaque", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "TICKETED"
    assert payload["data"]["order_url"].startswith("https://www.atriptech.com/")
    assert "paycfm_opaque" not in result.stdout
    assert payments.confirmation_ids == ["paycfm_opaque"]


def test_order_pay_requires_confirmation_id_as_stable_json_error() -> None:
    result = runner.invoke(app, ["order", "pay", "--json"])

    assert result.exit_code == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_order_pay_rejects_unsupported_payment_options(monkeypatch: pytest.MonkeyPatch) -> None:
    payments = FakePayments()
    runtime = BookingRuntime(
        verify=Empty(),
        ancillaries=Empty(),
        orders=Empty(),
        ticketing=Empty(),
        payments=payments,
    )  # type: ignore[arg-type]
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    result = runner.invoke(
        app,
        ["order", "pay", "--confirmation-id", "paycfm_opaque", "--payment-method", "card", "--json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"
    assert payments.confirmation_ids == []
