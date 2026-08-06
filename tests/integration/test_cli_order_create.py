from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from atlas_cli import booking_runtime as runtime_module
from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.cli import app
from atlas_cli.models import action_required_result, terminal_error_result

runner = CliRunner()


@dataclass
class FakeOrders:
    received: tuple[str, object, str | None] | None = None

    def create(self, booking_id: str, source: object, seat_policy: str | None):
        self.received = (booking_id, source, seat_policy)
        if source.use_stdin == (source.file_path is not None):
            return terminal_error_result("INVALID_ARGUMENT", "Choose exactly one passenger input source")
        return action_required_result("PAYMENT_CONFIRMATION_REQUIRED", "Confirm payment", data={"order_no": "ATAXA1"})


class FakeVerify:
    pass


class FakeAncillaries:
    pass


class FakeTicketing:
    pass


class FakePayments:
    pass


def test_order_create_emits_one_json_object_and_passes_stdin_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders = FakeOrders()
    runtime = BookingRuntime(  # type: ignore[arg-type]
        verify=FakeVerify(),
        ancillaries=FakeAncillaries(),
        orders=orders,
        ticketing=FakeTicketing(),
        payments=FakePayments(),
    )
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)
    monkeypatch.setattr("sys.stdin", io.StringIO("passenger json"))

    result = runner.invoke(app, ["order", "create", "--booking-id", "book_1", "--passengers-stdin", "--json"])

    assert result.exit_code == 0 and result.stderr == "" and len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "PAYMENT_CONFIRMATION_REQUIRED"
    assert orders.received is not None
    assert orders.received[0] == "book_1" and orders.received[2] is None
    assert orders.received[1].use_stdin is True


def test_order_create_missing_booking_id_is_a_stable_json_error() -> None:
    result = runner.invoke(app, ["order", "create", "--passengers-stdin", "--json"])
    assert result.exit_code == 2 and result.stderr == "" and len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("passenger_args", [(), ("--passengers-stdin", "--passengers-file", "/tmp/passengers.json")])
def test_order_create_requires_exactly_one_passenger_source_in_json(
    monkeypatch: pytest.MonkeyPatch,
    passenger_args: tuple[str, ...],
) -> None:
    orders = FakeOrders()
    runtime = BookingRuntime(  # type: ignore[arg-type]
        verify=FakeVerify(),
        ancillaries=FakeAncillaries(),
        orders=orders,
        ticketing=FakeTicketing(),
        payments=FakePayments(),
    )
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    result = runner.invoke(app, ["order", "create", "--booking-id", "book_1", *passenger_args, "--json"])

    assert result.exit_code == 2 and result.stderr == "" and len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_booking_composition_shares_one_secure_store(monkeypatch: pytest.MonkeyPatch) -> None:
    secure_store = object()
    monkeypatch.setattr(runtime_module, "KeyringSecretStore", lambda: secure_store)

    runtime = runtime_module.build_booking_runtime()

    services = (
        runtime.verify,
        runtime.ancillaries,
        runtime.orders,
        runtime.ticketing,
        runtime.payments,
    )
    assert all(service._secrets is secure_store for service in services)
    assert runtime.verify._access._secrets is secure_store
    assert runtime.verify._search_store._secrets is secure_store
    assert runtime.verify._booking_store._secrets is secure_store
    assert all(service._booking_store is runtime.verify._booking_store for service in services)
