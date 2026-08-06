from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BaggageOption,
    BookingContext,
    BookingRequirements,
    BookingState,
    OrderAttemptState,
    SeatOption,
    SegmentSlot,
    TravelerSlot,
)
from atlas_cli.booking_persistence import (
    BookingProjectionError,
    PersistedBookingState,
    hydrate_booking_context,
    project_booking_context,
)
from atlas_cli.search_models import NormalizedOffer, NormalizedPassengerPrice, NormalizedSegment
from atlas_cli.secure_store import BookingSecrets

NOW = datetime(2026, 8, 5, 8, tzinfo=UTC)


def offer() -> NormalizedOffer:
    segment = NormalizedSegment(
        departure_airport="KUL",
        arrival_airport="SIN",
        departure_time="202608101000",
        arrival_time="202608101110",
        carrier="AK",
        flight_number="AK701",
        duration_minutes=70,
        cabin_class=1,
    )
    return NormalizedOffer(
        upstream_identifier="private-routing-value",
        currency="USD",
        total_price=100,
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
        segments=[segment],
        ancillary_supported=("baggage", "seat"),
        bookable=True,
        price_status="verified",
    )


def context(*, terminal: bool = False) -> BookingContext:
    selected_offer = offer()
    return BookingContext(
        booking_id="book_public",
        search_id="srch_public",
        offer_id="off_public",
        route_generation="g" * 24,
        secret_ref="bsec_abcdefghijkl",
        secret_revision="rev_abcdefghijkl",
        session_id=None if terminal else "private-session-value",
        searched_offer=selected_offer,
        verified_offer=selected_offer,
        price_change="unchanged",
        requirements=BookingRequirements(required_fields=("name",)),
        travelers=(TravelerSlot(traveler_id="trav_public", passenger_type="adult"),),
        segments=(
            SegmentSlot(
                segment_id="seg_public",
                segment_index=1,
                direction="outbound",
                segment=selected_offer.segments[0],
            ),
        ),
        baggage_supported=True,
        seat_supported=True,
        baggage_options=(
            BaggageOption(
                baggage_id="bag_public",
                product_code="private-baggage-product",
                segment_id="seg_public",
                segment_index=1,
                piece=1,
                weight_kg=20,
                category="checked",
                price=30,
                currency="USD",
            ),
        ),
        seat_options=(
            SeatOption(
                seat_id="seat_public",
                product_code="private-seat-product",
                segment_id="seg_public",
                segment_index=1,
                row=12,
                column="A",
                characteristics=("window",),
                price=10,
                currency="USD",
            ),
        ),
        selections=(
            AncillarySelection(
                kind=AncillaryKind.BAGGAGE,
                traveler_id="trav_public",
                segment_id="seg_public",
                option_id="bag_public",
                product_code="private-baggage-product",
                segment_index=1,
            ),
        ),
        order_attempt_state=OrderAttemptState.UNKNOWN if terminal else OrderAttemptState.READY,
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )


def secrets(**updates: object) -> BookingSecrets:
    values: dict[str, object] = {
        "booking_id": "book_public",
        "generation": "g" * 24,
        "revision": "rev_abcdefghijkl",
        "session_id": "private-session-value",
        "products": {
            "bag_public": "private-baggage-product",
            "seat_public": "private-seat-product",
        },
    }
    values.update(updates)
    return BookingSecrets.model_validate(values)


def test_booking_projection_serializes_no_workflow_secrets() -> None:
    state = BookingState(contexts=(context(),))

    serialized = PersistedBookingState.from_domain(state).model_dump_json()

    for forbidden in (
        "routing_identifier",
        "routingIdentifier",
        "upstream_identifier",
        "session_id",
        "sessionId",
        "product_code",
        "productCode",
        "private-routing-value",
        "private-session-value",
        "private-baggage-product",
        "private-seat-product",
    ):
        assert forbidden not in serialized
    assert "bag_public" in serialized
    assert "seat_public" in serialized
    assert "trav_public" in serialized
    assert "seg_public" in serialized


def test_exact_secure_binding_hydrates_session_options_and_selection() -> None:
    projected = project_booking_context(context())

    hydrated = hydrate_booking_context(projected, secrets())

    assert hydrated.session_id == "private-session-value"
    assert hydrated.baggage_options[0].product_code == "private-baggage-product"
    assert hydrated.seat_options[0].product_code == "private-seat-product"
    assert hydrated.selections[0].product_code == "private-baggage-product"
    assert hydrated.selections[0].traveler_id == "trav_public"
    assert hydrated.selections[0].segment_id == "seg_public"


@pytest.mark.parametrize(
    "invalid",
    [
        secrets(products={"bag_public": "private-baggage-product"}),
        secrets(
            products={
                "bag_public": "private-baggage-product",
                "seat_public": "private-seat-product",
                "extra_public": "private-extra-product",
            }
        ),
        secrets(booking_id="book_wrong"),
        secrets(generation="h" * 24),
        secrets(revision="rev_wrongbinding"),
    ],
)
def test_invalid_binding_fails_closed_without_echoing_values(invalid: BookingSecrets) -> None:
    with pytest.raises(BookingProjectionError) as raised:
        hydrate_booking_context(project_booking_context(context()), invalid)

    exposed = str(raised.value)
    assert "private" not in exposed
    assert "wrong" not in exposed


def test_terminal_context_rejects_a_remaining_secure_record() -> None:
    with pytest.raises(BookingProjectionError):
        hydrate_booking_context(project_booking_context(context(terminal=True)), secrets())
