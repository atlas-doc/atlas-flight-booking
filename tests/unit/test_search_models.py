from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas_cli.search_models import (
    NormalizedOffer,
    NormalizedPassengerPrice,
    NormalizedSegment,
    SearchRequest,
)


def test_search_request_normalizes_iata_dates_and_builds_stable_one_way_payload() -> None:
    request = SearchRequest(
        origin=" kul ",
        destination="sin",
        depart="2026-08-10",
        adults=2,
        children=1,
        infants=1,
        airlines=[" ak ", "tr"],
        currency="usd",
        include_multiple_fare_families=True,
    )

    assert request.origin == "KUL"
    assert request.destination == "SIN"
    assert request.to_upstream_payload("request-1") == {
        "tripType": "1",
        "requestId": "request-1",
        "adultNum": 2,
        "childNum": 1,
        "infantNum": 1,
        "fromCity": "KUL",
        "toCity": "SIN",
        "fromDate": "20260810",
        "airlines": ["AK", "TR"],
        "currency": "USD",
        "includeMultipleFareFamily": True,
    }


def test_return_date_selects_round_trip_payload() -> None:
    request = SearchRequest(
        origin="KUL",
        destination="SIN",
        depart="2026-08-10",
        return_date="2026-08-15",
        adults=1,
    )

    payload = request.to_upstream_payload("request-2")

    assert payload["tripType"] == "2"
    assert payload["retDate"] == "20260815"


@pytest.mark.parametrize(
    "values",
    [
        {"origin": "KU", "destination": "SIN", "depart": "2026-08-10", "adults": 1},
        {"origin": "KUL", "destination": "S1N", "depart": "2026-08-10", "adults": 1},
        {"origin": "KUL", "destination": "SIN", "depart": "10-08-2026", "adults": 1},
        {"origin": "KUL", "destination": "SIN", "depart": "2026-08-10", "adults": 0},
        {"origin": "KUL", "destination": "SIN", "depart": "2026-08-10", "adults": 9, "children": 1},
        {"origin": "KUL", "destination": "SIN", "depart": "2026-08-10", "adults": 1, "infants": 2},
        {
            "origin": "KUL",
            "destination": "SIN",
            "depart": "2026-08-10",
            "return_date": "2026-08-09",
            "adults": 1,
        },
    ],
)
def test_invalid_search_inputs_fail_validation(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(values)


def test_origin_and_destination_must_differ() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(origin="kul", destination="KUL", depart="2026-08-10", adults=1)


def test_optional_fields_are_omitted_from_minimal_payload() -> None:
    request = SearchRequest(origin="KUL", destination="SIN", depart="2026-08-10", adults=1)

    payload = request.to_upstream_payload("request-3")

    assert "retDate" not in payload
    assert "airlines" not in payload
    assert "currency" not in payload
    assert payload["includeMultipleFareFamily"] is False


def test_normalized_booking_offer_supports_verified_price_and_segment_routing_data() -> None:
    segment = NormalizedSegment(
        departure_airport="KUL",
        arrival_airport="SIN",
        departure_time="202608101000",
        arrival_time="202608101110",
        carrier="SQ",
        operating_carrier="SQ",
        flight_number="SQ101",
        duration_minutes=70,
        direction="outbound",
    )

    offer = NormalizedOffer(
        currency="USD",
        total_price=120.0,
        transaction_fee_total=0.0,
        passenger_prices=[
            NormalizedPassengerPrice(
                passenger_type="adult",
                count=1,
                base_fare_per_passenger=100.0,
                tax_per_passenger=20.0,
                subtotal=120.0,
            )
        ],
        segments=[segment],
        ancillary_supported=("baggage", "seat"),
        bookable=True,
        price_status="verified",
    )

    assert offer.price_status == "verified"
    assert offer.ancillary_supported == ("baggage", "seat")
    assert offer.segments[0].operating_carrier == "SQ"
    assert offer.segments[0].direction == "outbound"
