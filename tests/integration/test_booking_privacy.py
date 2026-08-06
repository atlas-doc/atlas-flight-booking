from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_booking_flow import ORDER_NO, _runtime, _script
from typer.testing import CliRunner

from atlas_cli.cli import app

RUNNER = CliRunner()


def _privacy_passengers(traveler_id: str) -> str:
    return json.dumps(
        {
            "passengers": [
                {
                    "traveler_id": traveler_id,
                    "name": "LEAK" + "CHECK/PRIVACY",
                    "passenger_type": "adult",
                    "gender": "F",
                    "nationality": "SG",
                    "document": {
                        "type": "PP",
                        "number": "PRIVACY" + "DOC0001",
                        "issuing_country": "SG",
                        "expires": "2099-08-05",
                    },
                }
            ],
            "contact": {
                "name": "LEAK" + "CHECK/PRIVACY",
                "email": "privacy.probe" + "@example.invalid",
                "mobile": "0065-" + "55555555",
            },
        }
    )


def _invoke(args: list[str], *, input: str | None = None):
    result = RUNNER.invoke(app, [*args, "--json"], input=input)
    assert result.exit_code == 0
    assert result.stderr == ""
    return result, json.loads(result.stdout)


def test_booking_commands_never_persist_or_report_raw_passenger_or_internal_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing an output allowlist or PII masking would expose this recognizable passenger probe."""
    script = _script()
    script.responses["/queryOrderDetails.do"].append(script.responses["/queryOrderDetails.do"][0])
    runtime = _runtime(tmp_path, script)
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)
    captures = []

    verify_result, verified = _invoke(["offer", "verify", "--offer-id", "off_3"])
    captures.append(verify_result)
    booking_id = str(verified["data"]["booking_id"])
    traveler_id = str(verified["data"]["travelers"][0]["traveler_id"])
    segment_id = str(verified["data"]["segments"][0]["segment_id"])
    confirmed_result, _ = _invoke(["booking", "confirm-price", "--booking-id", booking_id])
    captures.append(confirmed_result)
    baggage_result, baggage = _invoke(["booking", "baggage", "list", "--booking-id", booking_id])
    captures.append(baggage_result)
    baggage_id = str(baggage["data"]["options"][0]["baggage_id"])
    for command in (
        [
            "booking",
            "baggage",
            "select",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
            "--baggage-id",
            baggage_id,
        ],
        [
            "booking",
            "baggage",
            "remove",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
        ],
        [
            "booking",
            "baggage",
            "select",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
            "--baggage-id",
            baggage_id,
        ],
    ):
        result, _ = _invoke(command)
        captures.append(result)
    seats_result, seats = _invoke(["booking", "seat", "list", "--booking-id", booking_id])
    captures.append(seats_result)
    seat_id = str(seats["data"]["options"][0]["seat_id"])
    for command in (
        [
            "booking",
            "seat",
            "select",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
            "--seat-id",
            seat_id,
        ],
        [
            "booking",
            "seat",
            "remove",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
        ],
        [
            "booking",
            "seat",
            "select",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
            "--seat-id",
            seat_id,
        ],
    ):
        result, _ = _invoke(command)
        captures.append(result)
    created_result, created = _invoke(
        [
            "order",
            "create",
            "--booking-id",
            booking_id,
            "--passengers-stdin",
            "--seat-policy",
            "accept-similar-seat",
        ],
        input=_privacy_passengers(traveler_id),
    )
    captures.append(created_result)
    paid_result, paid = _invoke(["order", "pay", "--confirmation-id", str(created["data"]["payment_confirmation_id"])])
    captures.append(paid_result)
    status_result, status = _invoke(["order", "status", "--order-no", ORDER_NO])
    captures.append(status_result)

    persisted = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    transcripts = "\n".join(result.stdout + result.stderr for result in captures)
    exception_reprs = "\n".join(repr(result.exception) for result in captures)
    captured_output = capsys.readouterr()
    observed = "\n".join(
        (transcripts, persisted, caplog.text, captured_output.out, captured_output.err, exception_reprs)
    )
    forbidden = (
        "LEAK" + "CHECK/PRIVACY",
        "PRIVACY" + "DOC0001",
        "privacy.probe" + "@example.invalid",
        "0065-" + "55555555",
        "fixture-jwt",
        "fixture-ak",
        "fixture-sk",
        "private-session-value",
        "private-baggage-product",
        "private-seat-product",
        "booking.test.invalid",
        "test1.atrip-restful.yutu-api.com",
        '"status":0',
        '"next_action"',
    )

    assert paid["code"] == "TICKETED"
    assert status["code"] == "TICKETED"
    assert [value for value in forbidden if value in observed] == []
