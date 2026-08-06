from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.cli import app
from atlas_cli.models import success_result

runner = CliRunner()


class FakeTicketingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def poll(self, order_no: str, *, timeout_seconds: float):
        self.calls.append((order_no, timeout_seconds))
        return success_result("TICKETING_PENDING", "Ticketing is still pending", data={"order_no": order_no})


class Empty:
    pass


def test_order_status_uses_fixed_budget_and_emits_one_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    ticketing = FakeTicketingService()
    runtime = BookingRuntime(  # type: ignore[arg-type]
        verify=Empty(), ancillaries=Empty(), orders=Empty(), ticketing=ticketing, payments=Empty()
    )
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    result = runner.invoke(app, ["order", "status", "--order-no", "ATAXA20260721085144583", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "TICKETING_PENDING"
    assert ticketing.calls == [("ATAXA20260721085144583", 120.0)]


def test_order_status_does_not_accept_caller_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas_cli.cli.build_booking_runtime",
        lambda: BookingRuntime(  # type: ignore[arg-type]
            verify=Empty(),
            ancillaries=Empty(),
            orders=Empty(),
            ticketing=FakeTicketingService(),
            payments=Empty(),
        ),
    )

    result = runner.invoke(app, ["order", "status", "--order-no", "order", "--timeout", "1", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"
