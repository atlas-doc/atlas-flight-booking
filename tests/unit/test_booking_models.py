from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BaggageOption,
    BookingContext,
    BookingRequirements,
    MaskedPassengerSummary,
    OrderAttemptState,
    SegmentSlot,
    TravelerSlot,
    VerifiedBookingSeed,
)
from atlas_cli.search_models import NormalizedOffer, NormalizedPassengerPrice, NormalizedSegment


def offer(total: float, ancillary_supported: tuple[str, ...] = ("baggage", "seat")) -> NormalizedOffer:
    return NormalizedOffer(
        upstream_identifier="opaque-route",
        currency="USD",
        total_price=total,
        transaction_fee_total=5,
        passenger_prices=[
            NormalizedPassengerPrice(
                passenger_type="adult",
                count=1,
                base_fare_per_passenger=75,
                tax_per_passenger=20,
                subtotal=95,
            )
        ],
        segments=[
            NormalizedSegment(
                departure_airport="KUL",
                arrival_airport="SIN",
                departure_time="202608101000",
                arrival_time="202608101110",
                carrier="AK",
                flight_number="AK701",
                duration_minutes=70,
                cabin_class=1,
            )
        ],
        ancillary_supported=ancillary_supported,
        bookable=True,
        price_status="verified",
    )


def test_booking_models_are_frozen_and_forbid_extra_fields() -> None:
    requirements = BookingRequirements(required_fields=("name", "document.number"))

    with pytest.raises(ValidationError):
        BookingRequirements(required_fields=("name",), private_name="MARIA")
    with pytest.raises(ValidationError):
        requirements.required_fields = ()


def test_booking_context_exposes_safe_segment_payloads() -> None:
    now = datetime(2026, 8, 5, 8, tzinfo=UTC)
    normalized = offer(100)
    segment = SegmentSlot(
        segment_id="seg_1",
        segment_index=1,
        direction="outbound",
        segment=normalized.segments[0],
    )
    context = BookingContext(
        booking_id="book_1",
        search_id="srch_1",
        offer_id="off_1",
        route_generation="g" * 24,
        secret_ref="bsec_abcdefghijkl",
        secret_revision="rev_abcdefghijkl",
        session_id="session-safe",
        searched_offer=normalized,
        verified_offer=normalized,
        price_change="unchanged",
        requirements=BookingRequirements(required_fields=("name",)),
        travelers=(TravelerSlot(traveler_id="trav_1", passenger_type="adult"),),
        segments=(segment,),
        baggage_supported=True,
        seat_supported=True,
        created_at=now,
        updated_at=now,
        expires_at=now,
    )

    assert context.most_significant_carrier() == "AK"
    assert context.segment_payloads() == (
        [
            {
                "segmentIndex": 1,
                "carrier": "AK",
                "flightNumber": "AK701",
                "depAirport": "KUL",
                "arrAirport": "SIN",
                "depTime": "202608101000",
                "arrTime": "202608101110",
                "cabinClass": 1,
            }
        ],
        [],
    )
    assert context.order_attempt_state is OrderAttemptState.READY


def test_selection_has_one_safe_record_per_kind_traveler_and_segment() -> None:
    selection = AncillarySelection(
        kind=AncillaryKind.BAGGAGE,
        traveler_id="trav_1",
        segment_id="seg_1",
        option_id="bag_1",
        product_code="internal-product",
        segment_index=1,
    )
    option = BaggageOption(
        baggage_id="bag_1",
        product_code="internal-product",
        segment_id="seg_1",
        segment_index=1,
        piece=1,
        weight_kg=20,
        category="checked",
        price=30,
        currency="USD",
    )

    assert selection.kind is AncillaryKind.BAGGAGE
    assert option.weight_kg == 20
    assert "internal-product" not in repr(selection)


def test_verified_seed_forbids_raw_passenger_fields() -> None:
    now = datetime(2026, 8, 5, 8, tzinfo=UTC)
    private = "MARIA-RAW-EXTRA-FIELD-PRIVATE"

    with pytest.raises(ValidationError) as raised:
        VerifiedBookingSeed(
            search_id="srch_1",
            offer_id="off_1",
            route_generation="g" * 24,
            routing_identifier="opaque-route",
            session_id="session-safe",
            searched_offer=offer(100),
            verified_offer=offer(100),
            requirements=BookingRequirements(required_fields=("name",)),
            travelers=(TravelerSlot(traveler_id="trav_1", passenger_type="adult"),),
            segments=(),
            expires_at=now,
            passenger_name=private,
        )

    exposed = f"{raised.value} {raised.value!r} {raised.value.errors()} {raised.value.json()}"
    assert private not in exposed


@pytest.mark.parametrize(
    ("values", "private"),
    [
        ({"name": "MARIA/SANTOS"}, "MARIA/SANTOS"),
        ({"name": "M***/S***", "document": "P1234567"}, "P1234567"),
    ],
)
def test_masked_passenger_summary_rejects_raw_pii_without_echoing_it(values: dict[str, str], private: str) -> None:
    with pytest.raises(ValidationError) as raised:
        MaskedPassengerSummary(traveler_id="trav_1", **values)

    exposed = f"{raised.value} {raised.value!r} {raised.value.errors()} {raised.value.json()}"
    assert private not in exposed
