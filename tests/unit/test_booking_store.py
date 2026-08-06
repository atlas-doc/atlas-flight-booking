from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BaggageOption,
    BookingRequirements,
    MaskedPassengerSummary,
    OrderAttemptState,
    OrderState,
    PaymentConfirmationSeed,
    PaymentState,
    PaymentSummary,
    SeatOption,
    SegmentSlot,
    TicketingState,
    TravelerSlot,
    VerifiedBookingSeed,
)
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.durable_io import durable_replace as real_durable_replace
from atlas_cli.search_models import NormalizedOffer, NormalizedPassengerPrice, NormalizedSegment
from tests.fake_workflow_store import FakeWorkflowSecretStore

FIXED_NOW = datetime(2026, 8, 5, 8, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, duration: timedelta) -> None:
        self.value += duration


def token_sequence(*values: str) -> Iterator[str]:
    return iter(values or ("1", "2", "3", "4"))


def offer(total: float, supported: tuple[str, ...] = ("baggage", "seat")) -> NormalizedOffer:
    return NormalizedOffer(
        upstream_identifier="routing-safe-value",
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
        ancillary_supported=supported,
        bookable=True,
        price_status="verified",
    )


def verified_seed(
    *,
    searched_total: float = 100,
    verified_total: float = 100,
    searched_supported: tuple[str, ...] = ("baggage", "seat"),
    verified_supported: tuple[str, ...] = ("baggage", "seat"),
    expires_at: datetime = FIXED_NOW + timedelta(hours=2),
) -> VerifiedBookingSeed:
    searched = offer(searched_total, searched_supported)
    verified = offer(verified_total, verified_supported)
    return VerifiedBookingSeed(
        search_id="srch_1",
        offer_id="off_1",
        route_generation="g" * 24,
        routing_identifier="routing-safe-value",
        session_id="session-safe-value",
        searched_offer=searched,
        verified_offer=verified,
        requirements=BookingRequirements(required_fields=("name", "document.number")),
        travelers=(TravelerSlot(traveler_id="trav_1", passenger_type="adult"),),
        segments=(
            SegmentSlot(
                segment_id="seg_1",
                segment_index=1,
                direction="outbound",
                segment=verified.segments[0],
            ),
        ),
        expires_at=expires_at,
    )


def payment_summary() -> PaymentSummary:
    return PaymentSummary(
        ticket_price=100,
        baggage_total=0,
        seat_total=0,
        total_price=105,
        currency="USD",
        passengers=(MaskedPassengerSummary(traveler_id="trav_1", name="M***/A***"),),
    )


def order(*, payment_deadline: datetime = FIXED_NOW + timedelta(hours=3)) -> OrderState:
    return OrderState(
        order_no="ATAXA20260721085144583",
        order_url="https://pay.example.invalid/safe-order",
        total_price=105,
        transaction_fee=5,
        currency="USD",
        payment_deadline=payment_deadline,
        summary=payment_summary(),
        summary_digest="digest-safe",
        payment_state=PaymentState.AWAITING_CONFIRMATION,
    )


def confirmation_seed(*, expires_at: datetime = FIXED_NOW + timedelta(minutes=10)) -> PaymentConfirmationSeed:
    return PaymentConfirmationSeed(
        order_no="ATAXA20260721085144583",
        summary_digest="digest-safe",
        expires_at=expires_at,
    )


def seeded_store(tmp_path: Path, *, clock: MutableClock | None = None) -> BookingStore:
    token = token_sequence("1", "confirm")
    store = BookingStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=token.__next__,
        now=(clock.now if clock else fixed_now),
    )
    context = store.create_from_verified(verified_seed())
    store.begin_order(context.booking_id, generation="g" * 24)
    store.save_order(context.booking_id, order(), generation="g" * 24)
    return store


def test_booking_context_round_trip_contains_no_passenger_pii(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())

    raw = store.contexts_file.read_text(encoding="utf-8")

    assert store.load(context.booking_id, generation="g" * 24) == context
    for private in ("MARIA", "P1234567", "maria@example.com", "+86-13800000000"):
        assert private not in raw


def test_create_derives_price_change_and_common_ancillary_support(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)

    context = store.create_from_verified(
        verified_seed(
            searched_total=100,
            verified_total=110,
            searched_supported=("baggage", "seat"),
            verified_supported=("seat",),
        )
    )

    assert context.price_change == "increased"
    assert context.baggage_supported is False
    assert context.seat_supported is True


def test_generation_mismatch_is_neutral_expiry(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())

    with pytest.raises(BookingStoreError) as raised:
        store.load(context.booking_id, generation="h" * 24)

    assert raised.value.code == "OFFER_EXPIRED"


def test_expired_booking_context_is_neutral_expiry(tmp_path: Path) -> None:
    clock = MutableClock(FIXED_NOW)
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=clock.now)
    context = store.create_from_verified(verified_seed(expires_at=clock.now() + timedelta(hours=2)))
    clock.advance(timedelta(hours=2, microseconds=1))

    with pytest.raises(BookingStoreError) as raised:
        store.load(context.booking_id, generation="g" * 24)

    assert raised.value.code == "OFFER_EXPIRED"


def test_selection_replaces_same_kind_traveler_and_segment_then_removes_it(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    options = (
        BaggageOption(
            baggage_id="bag_1",
            product_code="product-1",
            segment_id="seg_1",
            segment_index=1,
            piece=1,
            weight_kg=20,
            category="checked",
            price=30,
            currency="USD",
        ),
        BaggageOption(
            baggage_id="bag_2",
            product_code="product-2",
            segment_id="seg_1",
            segment_index=1,
            piece=1,
            weight_kg=25,
            category="checked",
            price=40,
            currency="USD",
        ),
    )
    store.replace_options(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        options=options,
        generation="g" * 24,
    )
    first = AncillarySelection(
        kind=AncillaryKind.BAGGAGE,
        traveler_id="trav_1",
        segment_id="seg_1",
        option_id="bag_1",
        product_code="product-1",
        segment_index=1,
    )
    second = first.model_copy(update={"option_id": "bag_2", "product_code": "product-2"})

    store.select(context.booking_id, first, generation="g" * 24)
    selected = store.select(context.booking_id, second, generation="g" * 24)
    removed = store.remove(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        traveler_id="trav_1",
        segment_id="seg_1",
        generation="g" * 24,
    )

    assert selected.selections == (second,)
    assert removed.selections == ()


def test_refreshing_options_atomically_clears_only_same_kind_selections(tmp_path: Path) -> None:
    secrets = FakeWorkflowSecretStore()
    store = BookingStore(tmp_path, secrets=secrets, token_factory=token_sequence("1").__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    baggage = BaggageOption(
        baggage_id="bag_1",
        product_code="baggage-product-1",
        segment_id="seg_1",
        segment_index=1,
        piece=1,
        weight_kg=20,
        category="checked",
        price=30,
        currency="USD",
    )
    seat = SeatOption(
        seat_id="seat_1",
        product_code="seat-product-1",
        segment_id="seg_1",
        segment_index=1,
        row=12,
        column="A",
        price=15,
        currency="USD",
    )
    store.replace_options(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        options=(baggage,),
        generation="g" * 24,
    )
    store.replace_options(
        context.booking_id,
        kind=AncillaryKind.SEAT,
        options=(seat,),
        generation="g" * 24,
    )
    baggage_selection = AncillarySelection(
        kind=AncillaryKind.BAGGAGE,
        traveler_id="trav_1",
        segment_id="seg_1",
        option_id="bag_1",
        product_code="baggage-product-1",
        segment_index=1,
    )
    seat_selection = AncillarySelection(
        kind=AncillaryKind.SEAT,
        traveler_id="trav_1",
        segment_id="seg_1",
        option_id="seat_1",
        product_code="seat-product-1",
        segment_index=1,
    )
    store.select(context.booking_id, baggage_selection, generation="g" * 24)
    store.select(context.booking_id, seat_selection, generation="g" * 24)
    refreshed = baggage.model_copy(update={"baggage_id": "bag_2", "product_code": "baggage-product-2"})

    updated = store.replace_options(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        options=(refreshed,),
        generation="g" * 24,
    )

    assert updated.baggage_options == (refreshed,)
    assert updated.selections == (seat_selection,)
    current_secret = secrets.bookings[(updated.secret_ref, updated.secret_revision)]
    assert current_secret.products == {
        "bag_2": "baggage-product-2",
        "seat_1": "seat-product-1",
    }
    assert set(secrets.bookings) == {(updated.secret_ref, updated.secret_revision)}
    public = store.contexts_file.read_text(encoding="utf-8")
    for forbidden in (
        "product_code",
        "productCode",
        "baggage-product-2",
        "seat-product-1",
        "session_id",
        "upstream_identifier",
    ):
        assert forbidden not in public


@pytest.mark.parametrize(
    "selection_update",
    [
        {"option_id": "bag_old"},
        {"product_code": "wrong-private-product"},
        {"segment_id": "seg_2", "segment_index": 2},
        {"segment_index": 2},
        {"traveler_id": "trav_old"},
    ],
)
def test_locked_select_rejects_selection_not_exactly_bound_to_current_state(
    tmp_path: Path,
    selection_update: dict[str, object],
) -> None:
    seed = verified_seed()
    first_segment = seed.segments[0]
    second_segment = first_segment.model_copy(
        update={
            "segment_id": "seg_2",
            "segment_index": 2,
            "segment": first_segment.segment.model_copy(update={"flight_number": "AK702"}),
        }
    )
    store = BookingStore(
        tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=token_sequence("1").__next__, now=fixed_now
    )
    context = store.create_from_verified(seed.model_copy(update={"segments": (*seed.segments, second_segment)}))
    option = BaggageOption(
        baggage_id="bag_1",
        product_code="current-private-product",
        segment_id="seg_1",
        segment_index=1,
        piece=1,
        weight_kg=20,
        category="checked",
        price=30,
        currency="USD",
    )
    store.replace_options(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        options=(option,),
        generation="g" * 24,
    )
    selection = AncillarySelection(
        kind=AncillaryKind.BAGGAGE,
        traveler_id="trav_1",
        segment_id="seg_1",
        option_id="bag_1",
        product_code="current-private-product",
        segment_index=1,
    ).model_copy(update=selection_update)
    saved_before = store.contexts_file.read_text(encoding="utf-8")

    with pytest.raises(BookingStoreError) as raised:
        store.select(context.booking_id, selection, generation="g" * 24)

    assert raised.value.code == "ANCILLARY_SELECTION_INVALID"
    assert "private" not in f"{raised.value} {raised.value!r}"
    assert store.contexts_file.read_text(encoding="utf-8") == saved_before
    assert store.load(context.booking_id, generation="g" * 24).selections == ()


def test_locked_select_rejects_closed_capability_without_restoring_selection(tmp_path: Path) -> None:
    store = BookingStore(
        tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=token_sequence("1").__next__, now=fixed_now
    )
    context = store.create_from_verified(verified_seed())
    option = SeatOption(
        seat_id="seat_1",
        product_code="private-seat-product",
        segment_id="seg_1",
        segment_index=1,
        row=12,
        column="A",
        price=15,
        currency="USD",
    )
    store.replace_options(
        context.booking_id,
        kind=AncillaryKind.SEAT,
        options=(option,),
        generation="g" * 24,
    )
    store.close_ancillary(
        context.booking_id,
        kind=AncillaryKind.SEAT,
        generation="g" * 24,
    )
    selection = AncillarySelection(
        kind=AncillaryKind.SEAT,
        traveler_id="trav_1",
        segment_id="seg_1",
        option_id="seat_1",
        product_code="private-seat-product",
        segment_index=1,
    )
    saved_before = store.contexts_file.read_text(encoding="utf-8")

    with pytest.raises(BookingStoreError) as raised:
        store.select(context.booking_id, selection, generation="g" * 24)

    assert raised.value.code == "ANCILLARY_SELECTION_INVALID"
    assert store.contexts_file.read_text(encoding="utf-8") == saved_before
    closed = store.load(context.booking_id, generation="g" * 24)
    assert closed.seat_supported is False
    assert closed.selections == ()


def test_locked_select_rejects_inconsistent_connected_baggage_without_replacing_state(tmp_path: Path) -> None:
    seed = verified_seed()
    first_segment = seed.segments[0]
    second_segment = first_segment.model_copy(
        update={
            "segment_id": "seg_2",
            "segment_index": 2,
            "segment": first_segment.segment.model_copy(update={"flight_number": "AK702"}),
        }
    )
    store = BookingStore(
        tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=token_sequence("1").__next__, now=fixed_now
    )
    context = store.create_from_verified(seed.model_copy(update={"segments": (*seed.segments, second_segment)}))
    first = BaggageOption(
        baggage_id="bag_1",
        product_code="private-product-1",
        segment_id="seg_1",
        segment_index=1,
        piece=1,
        weight_kg=20,
        category="checked",
        price=30,
        currency="USD",
    )
    second = first.model_copy(
        update={
            "baggage_id": "bag_2",
            "product_code": "private-product-2",
            "segment_id": "seg_2",
            "segment_index": 2,
            "weight_kg": 25,
        }
    )
    store.replace_options(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        options=(first, second),
        generation="g" * 24,
    )
    first_selection = AncillarySelection(
        kind=AncillaryKind.BAGGAGE,
        traveler_id="trav_1",
        segment_id="seg_1",
        option_id="bag_1",
        product_code="private-product-1",
        segment_index=1,
    )
    second_selection = AncillarySelection(
        kind=AncillaryKind.BAGGAGE,
        traveler_id="trav_1",
        segment_id="seg_2",
        option_id="bag_2",
        product_code="private-product-2",
        segment_index=2,
    )
    store.select(context.booking_id, first_selection, generation="g" * 24)
    saved_before = store.contexts_file.read_text(encoding="utf-8")

    with pytest.raises(BookingStoreError) as raised:
        store.select(context.booking_id, second_selection, generation="g" * 24)

    assert raised.value.code == "ANCILLARY_SELECTION_INVALID"
    assert store.contexts_file.read_text(encoding="utf-8") == saved_before
    assert store.load(context.booking_id, generation="g" * 24).selections == (first_selection,)


def test_expire_clears_ancillaries_and_blocks_booking_stage_calls(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    secrets = FakeWorkflowSecretStore()
    store = BookingStore(tmp_path, secrets=secrets, token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    store.replace_options(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        options=(
            BaggageOption(
                baggage_id="bag_1",
                product_code="product-1",
                segment_id="seg_1",
                segment_index=1,
                piece=1,
                weight_kg=20,
                category="checked",
                price=30,
                currency="USD",
            ),
        ),
        generation="g" * 24,
    )

    expired = store.expire_context(context.booking_id, generation="g" * 24)

    assert expired.baggage_options == ()
    assert expired.selections == ()
    assert secrets.bookings == {}
    with pytest.raises(BookingStoreError) as raised:
        store.begin_order(context.booking_id, generation="g" * 24)
    assert raised.value.code == "OFFER_EXPIRED"


def test_replace_options_generation_mismatch_is_neutral_expiry_without_write(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    saved_before = store.contexts_file.read_text(encoding="utf-8")

    with pytest.raises(BookingStoreError) as raised:
        store.replace_options(
            context.booking_id,
            kind=AncillaryKind.BAGGAGE,
            options=(),
            generation="h" * 24,
        )

    assert raised.value.code == "OFFER_EXPIRED"
    assert store.contexts_file.read_text(encoding="utf-8") == saved_before


@pytest.mark.parametrize(
    ("kind", "option", "private"),
    [
        (
            AncillaryKind.BAGGAGE,
            SeatOption(
                seat_id="seat_1",
                product_code="private-seat-product",
                segment_id="seg_1",
                segment_index=1,
                row=12,
                column="A",
                price=15,
                currency="USD",
            ),
            "private-seat-product",
        ),
        (
            AncillaryKind.SEAT,
            BaggageOption(
                baggage_id="bag_1",
                product_code="private-baggage-product",
                segment_id="seg_1",
                segment_index=1,
                piece=1,
                weight_kg=20,
                category="checked",
                price=30,
                currency="USD",
            ),
            "private-baggage-product",
        ),
    ],
)
def test_replace_options_rejects_kind_type_mismatch_without_write_or_product_echo(
    tmp_path: Path,
    kind: AncillaryKind,
    option: BaggageOption | SeatOption,
    private: str,
) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    saved_before = store.contexts_file.read_text(encoding="utf-8")

    with pytest.raises(BookingStoreError) as raised:
        store.replace_options(
            context.booking_id,
            kind=kind,
            options=(option,),
            generation="g" * 24,
        )

    assert raised.value.code == "ANCILLARY_SELECTION_INVALID"
    assert private not in f"{raised.value} {raised.value!r}"
    assert store.contexts_file.read_text(encoding="utf-8") == saved_before


def test_begin_order_is_compare_and_set_under_concurrency(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())

    def begin() -> str:
        try:
            return store.begin_order(context.booking_id, generation="g" * 24).order_attempt_state.value
        except BookingStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: begin(), range(2)))

    assert results.count("creating") == 1
    assert results.count("ORDER_STATE_INVALID") == 1
    assert store.load(context.booking_id, generation="g" * 24).order_attempt_state is OrderAttemptState.CREATING


def test_confirmed_failure_can_reset_creating_order_attempt(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    store.begin_order(context.booking_id, generation="g" * 24)

    reset = store.reset_order_attempt(context.booking_id, generation="g" * 24)

    assert reset.order_attempt_state is OrderAttemptState.READY


def test_confirmed_failure_reset_survives_context_expiry(tmp_path: Path) -> None:
    clock = MutableClock(FIXED_NOW)
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=clock.now)
    context = store.create_from_verified(verified_seed(expires_at=FIXED_NOW + timedelta(minutes=1)))
    store.begin_order(context.booking_id, generation="g" * 24)
    clock.advance(timedelta(minutes=2))

    reset = store.reset_order_attempt(context.booking_id, generation="stale-generation")

    assert reset.order_attempt_state is OrderAttemptState.READY


def test_increased_price_must_be_confirmed_before_begin_order(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed(searched_total=100, verified_total=110))

    with pytest.raises(BookingStoreError) as raised:
        store.begin_order(context.booking_id, generation="g" * 24)

    assert raised.value.code == "PRICE_CONFIRMATION_REQUIRED"
    confirmed = store.confirm_price(context.booking_id, generation="g" * 24)
    creating = store.begin_order(context.booking_id, generation="g" * 24)
    assert confirmed.increased_price_confirmed is True
    assert creating.order_attempt_state is OrderAttemptState.CREATING


def test_payment_confirmation_is_consumed_once_atomically(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    confirmation = store.issue_payment_confirmation("book_1", confirmation_seed())

    consumed = store.consume_payment_confirmation(confirmation.confirmation_id, now=fixed_now())

    assert consumed.order_no == "ATAXA20260721085144583"
    assert consumed.payment_state is PaymentState.PAYING
    with pytest.raises(BookingStoreError) as raised:
        store.consume_payment_confirmation(confirmation.confirmation_id, now=fixed_now())
    assert raised.value.code == "PAYMENT_CONFIRMATION_INVALID"


def test_save_order_rejects_bypassed_raw_passenger_pii_without_echo_or_persistence(tmp_path: Path) -> None:
    tokens = token_sequence("1")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    store.begin_order(context.booking_id, generation="g" * 24)
    raw_name = "MARIA/SANTOS"
    raw_document = "P1234567"
    unsafe_passenger = MaskedPassengerSummary.model_construct(
        traveler_id="trav_1", name=raw_name, document=raw_document
    )
    unsafe_summary = payment_summary().model_copy(update={"passengers": (unsafe_passenger,)})
    unsafe_order = order().model_copy(update={"summary": unsafe_summary})
    saved_before = store.contexts_file.read_text(encoding="utf-8")

    with pytest.raises(BookingStoreError) as raised:
        store.save_order(context.booking_id, unsafe_order, generation="g" * 24)

    exposed = f"{raised.value} {raised.value!r} {unsafe_order} {unsafe_order!r}"
    assert raw_name not in exposed
    assert raw_document not in exposed
    assert store.contexts_file.read_text(encoding="utf-8") == saved_before
    assert raw_name not in saved_before
    assert raw_document not in saved_before


def test_save_order_with_confirmation_performs_one_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    tokens = token_sequence("1", "confirm")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)
    context = store.create_from_verified(verified_seed())
    store.begin_order(context.booking_id, generation="g" * 24)
    destinations: list[Path] = []

    def recording_replace(source: Path, destination: Path) -> None:
        destinations.append(Path(destination))
        real_durable_replace(source, destination)

    monkeypatch.setattr("atlas_cli.booking_store.durable_replace", recording_replace)

    saved, confirmation = store.save_order_with_confirmation(
        context.booking_id,
        order(),
        confirmation_seed(),
        generation="g" * 24,
    )

    assert saved.order is not None
    assert confirmation.confirmation_id == "paycfm_confirm"
    assert destinations == [store.contexts_file]


def test_post_side_effect_operations_survive_context_expiry(tmp_path: Path) -> None:
    clock = MutableClock(FIXED_NOW)
    tokens = token_sequence("1", "confirm")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=clock.now)
    context = store.create_from_verified(verified_seed(expires_at=FIXED_NOW + timedelta(minutes=1)))
    store.begin_order(context.booking_id, generation="g" * 24)
    clock.advance(timedelta(minutes=2))

    saved, confirmation = store.save_order_with_confirmation(
        context.booking_id,
        order(),
        confirmation_seed(expires_at=clock.now() + timedelta(minutes=10)),
        generation="g" * 24,
    )
    paid = store.consume_payment_confirmation(confirmation.confirmation_id, now=clock.now())
    updated = store.update_payment(paid.order_no, PaymentState.PAID)
    ticketed = store.update_ticketing(
        paid.order_no,
        TicketingState.TICKETED,
        airline_pnrs=("SAFEPNR",),
        ticket_numbers=("SAFE-TICKET",),
    )

    assert saved.order_attempt_state is OrderAttemptState.CREATED
    assert updated.payment_state is PaymentState.PAID
    assert ticketed.ticketing_state is TicketingState.TICKETED
    assert store.load_order(paid.order_no) == ticketed


def test_confirmation_expiry_and_digest_binding_fail_closed(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    confirmation = store.issue_payment_confirmation(
        "book_1", confirmation_seed(expires_at=FIXED_NOW - timedelta(microseconds=1))
    )

    with pytest.raises(BookingStoreError) as raised:
        store.consume_payment_confirmation(confirmation.confirmation_id, now=FIXED_NOW)

    assert raised.value.code == "PAYMENT_CONFIRMATION_INVALID"
    assert store.load_order(order().order_no).payment_state is PaymentState.AWAITING_CONFIRMATION


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_store_files_have_private_permissions_without_loosening_existing_modes(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    tokens = token_sequence("1")
    store = BookingStore(private, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=fixed_now)

    store.create_from_verified(verified_seed())

    assert private.stat().st_mode & 0o777 == 0o700
    assert store.contexts_file.stat().st_mode & 0o077 == 0
    assert (private / "contexts.lock").stat().st_mode & 0o077 == 0

    store.contexts_file.chmod(0o400)
    private.chmod(0o500)
    with pytest.raises(BookingStoreError):
        store.load("book_missing", generation="g" * 24)
    assert private.stat().st_mode & 0o777 == 0o500
    assert store.contexts_file.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("malformed", ["not-json", "[]"])
def test_corrupt_state_fails_closed_without_leaking_content(tmp_path: Path, malformed: str) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "contexts.json").write_text(malformed, encoding="utf-8")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore())

    with pytest.raises(BookingStoreError) as raised:
        store.load("book_missing", generation="g" * 24)

    assert raised.value.code == "BOOKING_STATE_INVALID"
    assert str(raised.value) == "Saved booking state could not be processed"
    assert malformed not in str(raised.value)


def test_persisted_confirmation_is_marked_consumed_in_same_state_as_paying_order(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    confirmation = store.issue_payment_confirmation("book_1", confirmation_seed())

    store.consume_payment_confirmation(confirmation.confirmation_id, now=FIXED_NOW)

    raw = json.loads(store.contexts_file.read_text(encoding="utf-8"))
    assert raw["confirmations"][0]["consumed_at"] == "2026-08-05T08:00:00Z"
    assert raw["contexts"][0]["order"]["payment_state"] == "paying"


def test_initial_secret_round_trip_precedes_public_booking_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = FakeWorkflowSecretStore()
    workflow_tokens = iter(("secret000000", "revision0000"))
    store = BookingStore(
        tmp_path,
        secrets=secrets,
        token_factory=iter(("booking",)).__next__,
        workflow_token_factory=workflow_tokens.__next__,
        now=fixed_now,
    )
    real_write = store._atomic_write

    def recording_write(state) -> None:
        secrets.events.append("json")
        real_write(state)

    monkeypatch.setattr(store, "_atomic_write", recording_write)

    store.create_from_verified(verified_seed())

    assert secrets.events == [
        "save:bsec_secret000000:rev_revision0000",
        "load:bsec_secret000000:rev_revision0000",
        "json",
    ]


def test_public_booking_commit_failure_clears_unreferenced_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = FakeWorkflowSecretStore()
    store = BookingStore(tmp_path, secrets=secrets, now=fixed_now)

    def fail_write(state) -> None:
        raise OSError("private public-state failure")

    monkeypatch.setattr(store, "_atomic_write", fail_write)

    with pytest.raises(OSError, match="private public-state failure"):
        store.create_from_verified(verified_seed())

    assert secrets.bookings == {}
    assert secrets.events[-1].startswith("clear:")


def test_legacy_plaintext_booking_state_is_cleared_and_expires(tmp_path: Path) -> None:
    legacy = {
        "schema_version": "1",
        "contexts": [{"session_id": "private-session"}],
        "confirmations": [],
    }
    (tmp_path / "contexts.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), now=fixed_now)

    with pytest.raises(BookingStoreError) as raised:
        store.load("book_missing", generation="g" * 24)

    assert raised.value.code == "OFFER_EXPIRED"
    assert json.loads((tmp_path / "contexts.json").read_text(encoding="utf-8")) == {
        "schema_version": "2",
        "contexts": [],
        "confirmations": [],
    }


def test_missing_secret_for_active_booking_does_not_block_unrelated_order_lookup(
    tmp_path: Path,
) -> None:
    secrets = FakeWorkflowSecretStore()
    store = BookingStore(
        tmp_path,
        secrets=secrets,
        token_factory=iter(("active", "ordered")).__next__,
        now=fixed_now,
    )
    active = store.create_from_verified(verified_seed())
    ordered = store.create_from_verified(verified_seed())
    store.begin_order(ordered.booking_id, generation="g" * 24)
    saved_order = order()
    store.save_order(ordered.booking_id, saved_order, generation="g" * 24)
    secrets.bookings.pop((active.secret_ref, active.secret_revision))

    assert store.load_order(saved_order.order_no) == saved_order


def test_ancillary_public_commit_failure_keeps_previous_secret_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = FakeWorkflowSecretStore()
    workflow_tokens = iter(("secret000000", "initialrev00", "nextrevision", "failedrev000"))
    store = BookingStore(
        tmp_path,
        secrets=secrets,
        workflow_token_factory=workflow_tokens.__next__,
        now=fixed_now,
    )
    context = store.create_from_verified(verified_seed())
    first = BaggageOption(
        baggage_id="bag_1",
        product_code="private-product-one",
        segment_id="seg_1",
        segment_index=1,
        piece=1,
        weight_kg=20,
        category="checked",
        price=30,
        currency="USD",
    )
    current = store.replace_options(
        context.booking_id,
        kind=AncillaryKind.BAGGAGE,
        options=(first,),
        generation="g" * 24,
    )
    previous_json = store.contexts_file.read_bytes()
    previous_records = dict(secrets.bookings)

    def fail_write(state) -> None:
        raise OSError("private public-state failure")

    monkeypatch.setattr(store, "_atomic_write", fail_write)
    second = first.model_copy(update={"baggage_id": "bag_2", "product_code": "private-product-two"})

    with pytest.raises(OSError, match="private public-state failure"):
        store.replace_options(
            context.booking_id,
            kind=AncillaryKind.BAGGAGE,
            options=(second,),
            generation="g" * 24,
        )

    assert store.contexts_file.read_bytes() == previous_json
    assert secrets.bookings == previous_records
    assert (current.secret_ref, current.secret_revision) in secrets.bookings
