from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.cli import app
from atlas_cli.models import success_result

runner = CliRunner()


@dataclass
class FakeAncillaryService:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def list_baggage(self, booking_id: str):
        self.calls.append(("list_baggage", booking_id))
        return success_result("BAGGAGE_OPTIONS_LISTED", "Baggage options listed", data={"booking_id": booking_id})

    def select_baggage(self, booking_id: str, traveler_id: str, segment_id: str, baggage_id: str):
        self.calls.append(("select_baggage", booking_id, traveler_id, segment_id, baggage_id))
        return success_result("BAGGAGE_SELECTED", "Baggage selected", data={"booking_id": booking_id})

    def remove_baggage(self, booking_id: str, traveler_id: str, segment_id: str):
        self.calls.append(("remove_baggage", booking_id, traveler_id, segment_id))
        return success_result("BAGGAGE_REMOVED", "Baggage removed", data={"booking_id": booking_id})

    def list_seats(self, booking_id: str):
        self.calls.append(("list_seats", booking_id))
        return success_result("SEAT_OPTIONS_LISTED", "Seat options listed", data={"booking_id": booking_id})

    def select_seat(self, booking_id: str, traveler_id: str, segment_id: str, seat_id: str):
        self.calls.append(("select_seat", booking_id, traveler_id, segment_id, seat_id))
        return success_result("SEAT_SELECTED", "Seat selected", data={"booking_id": booking_id})

    def remove_seat(self, booking_id: str, traveler_id: str, segment_id: str):
        self.calls.append(("remove_seat", booking_id, traveler_id, segment_id))
        return success_result("SEAT_REMOVED", "Seat removed", data={"booking_id": booking_id})


class FakeVerifyService:
    pass


class FakeOrderService:
    pass


class FakeTicketingService:
    pass


class FakePaymentService:
    pass


@pytest.mark.parametrize(
    ("args", "expected_code", "expected_call"),
    [
        (
            ("booking", "baggage", "list", "--booking-id", "book_1", "--json"),
            "BAGGAGE_OPTIONS_LISTED",
            ("list_baggage", "book_1"),
        ),
        (
            (
                "booking",
                "baggage",
                "select",
                "--booking-id",
                "book_1",
                "--traveler-id",
                "trav_1",
                "--segment-id",
                "seg_1",
                "--baggage-id",
                "bag_1",
                "--json",
            ),
            "BAGGAGE_SELECTED",
            ("select_baggage", "book_1", "trav_1", "seg_1", "bag_1"),
        ),
        (
            (
                "booking",
                "baggage",
                "remove",
                "--booking-id",
                "book_1",
                "--traveler-id",
                "trav_1",
                "--segment-id",
                "seg_1",
                "--json",
            ),
            "BAGGAGE_REMOVED",
            ("remove_baggage", "book_1", "trav_1", "seg_1"),
        ),
        (
            ("booking", "seat", "list", "--booking-id", "book_1", "--json"),
            "SEAT_OPTIONS_LISTED",
            ("list_seats", "book_1"),
        ),
        (
            (
                "booking",
                "seat",
                "select",
                "--booking-id",
                "book_1",
                "--traveler-id",
                "trav_1",
                "--segment-id",
                "seg_1",
                "--seat-id",
                "seat_1",
                "--json",
            ),
            "SEAT_SELECTED",
            ("select_seat", "book_1", "trav_1", "seg_1", "seat_1"),
        ),
        (
            (
                "booking",
                "seat",
                "remove",
                "--booking-id",
                "book_1",
                "--traveler-id",
                "trav_1",
                "--segment-id",
                "seg_1",
                "--json",
            ),
            "SEAT_REMOVED",
            ("remove_seat", "book_1", "trav_1", "seg_1"),
        ),
    ],
)
def test_exact_nested_ancillary_commands_emit_one_safe_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    expected_code: str,
    expected_call: tuple[object, ...],
) -> None:
    ancillaries = FakeAncillaryService()
    runtime = BookingRuntime(
        verify=FakeVerifyService(),
        ancillaries=ancillaries,
        orders=FakeOrderService(),
        ticketing=FakeTicketingService(),
        payments=FakePaymentService(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    result = runner.invoke(app, list(args))

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == expected_code
    assert payload["data"]["booking_id"] == "book_1"
    assert ancillaries.calls == [expected_call]
    assert "productCode" not in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("booking", "baggage", "list", "--json"),
        ("booking", "baggage", "select", "--booking-id", "book_1", "--json"),
        ("booking", "baggage", "remove", "--booking-id", "book_1", "--json"),
        ("booking", "seat", "list", "--json"),
        ("booking", "seat", "select", "--booking-id", "book_1", "--json"),
        ("booking", "seat", "remove", "--booking-id", "book_1", "--json"),
    ],
)
def test_ancillary_missing_required_identifier_is_stable_json(args: tuple[str, ...]) -> None:
    result = runner.invoke(app, list(args))

    assert result.exit_code == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("kind", ["baggage", "seat"])
def test_ancillary_commands_have_no_top_level_alias(kind: str) -> None:
    result = runner.invoke(app, [kind, "list", "--booking-id", "book_1", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"
