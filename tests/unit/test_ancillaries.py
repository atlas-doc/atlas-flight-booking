from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from atlas_cli.access import TransactionAccess
from atlas_cli.ancillaries import AncillaryAdapter, AncillaryService
from atlas_cli.booking_models import (
    AncillaryKind,
    BookingRequirements,
    SeatOption,
    SegmentSlot,
    TravelerSlot,
    VerifiedBookingSeed,
)
from atlas_cli.booking_store import BookingStore
from atlas_cli.business_client import BusinessApiError, BusinessResponse
from atlas_cli.endpoints import BusinessOperation, BusinessRoute, CredentialSlot
from atlas_cli.models import CommandStatus
from atlas_cli.search_models import NormalizedOffer, NormalizedPassengerPrice, NormalizedSegment
from atlas_cli.secure_store import ApiCredential, Credentials
from tests.fake_workflow_store import FakeWorkflowSecretStore

NOW = datetime(2026, 8, 5, 8, tzinfo=UTC)
GENERATION = "g" * 24
PRIVATE_BAGGAGE = "PC_PRIVATE_BAGGAGE"
PRIVATE_SEAT = "PC_PRIVATE_SEAT"


def segment(
    *,
    flight_number: str = "AK701",
    direction: Literal["outbound", "inbound"] = "outbound",
) -> NormalizedSegment:
    return NormalizedSegment(
        departure_airport="KUL" if direction == "outbound" else "SIN",
        arrival_airport="SIN" if direction == "outbound" else "KUL",
        departure_time="202608101000",
        arrival_time="202608101110",
        carrier="AK",
        flight_number=flight_number,
        duration_minutes=70,
        cabin_class=1,
        direction=direction,
    )


def offer(
    supported: tuple[str, ...] = ("baggage", "seat"),
    *,
    segments: tuple[NormalizedSegment, ...] = (segment(),),
) -> NormalizedOffer:
    return NormalizedOffer(
        upstream_identifier="private-routing",
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
        segments=segments,
        ancillary_supported=supported,
        bookable=True,
        price_status="verified",
    )


def booking_seed(
    *,
    search_support: tuple[str, ...] = ("baggage", "seat"),
    verify_support: tuple[str, ...] = ("baggage", "seat"),
    connected: bool = False,
    round_trip: bool = False,
    duplicate_index: bool = False,
) -> VerifiedBookingSeed:
    if connected:
        normalized_segments = (segment(), segment(flight_number="AK703"))
    elif round_trip:
        normalized_segments = (
            segment(),
            segment(flight_number="AK703", direction="inbound"),
        )
    else:
        normalized_segments = (segment(),)
    searched = offer(search_support, segments=normalized_segments)
    verified = offer(verify_support, segments=normalized_segments)
    segment_slots = tuple(
        SegmentSlot(
            segment_id=f"seg_{index}",
            segment_index=1 if duplicate_index else index,
            direction=item.direction,
            segment=item,
        )
        for index, item in enumerate(verified.segments, start=1)
    )
    return VerifiedBookingSeed(
        search_id="srch_1",
        offer_id="off_1",
        route_generation=GENERATION,
        routing_identifier="private-routing",
        session_id="private-session",
        searched_offer=searched,
        verified_offer=verified,
        requirements=BookingRequirements(required_fields=("name", "passenger_type")),
        travelers=(TravelerSlot(traveler_id="trav_1", passenger_type="adult"),),
        segments=segment_slots,
        expires_at=NOW + timedelta(hours=2),
    )


def baggage_product(
    product_code: str = PRIVATE_BAGGAGE,
    *,
    segment_index: int = 1,
    piece: int = 1,
    weight: int = 20,
    price: float = 30,
) -> dict[str, object]:
    return {
        "ancillaryCode": "DISPLAY_ONLY",
        "auxBaggageElement": {
            "isAllWeight": True,
            "piece": piece,
            "size": "158CM",
            "weight": weight,
        },
        "canPurchasePostTicket": 0,
        "canPurchaseWithTicket": 1,
        "categoryCode": "StandardCheckInBaggage",
        "currency": "USD",
        "maxQty": 1,
        "minQty": 1,
        "price": price,
        "productCode": product_code,
        "segmentIndex": segment_index,
        "vendorCurrency": "USD",
        "vendorPrice": price,
    }


def baggage_response(
    status: int = 0,
    *,
    products: list[dict[str, object]] | None = None,
    retry_after: object | None = None,
) -> BusinessResponse:
    data: dict[str, object] = {
        "data": {
            "offerId": "private-session",
            "outboundSegments": [],
            "ancillaryProductElements": [baggage_product()] if products is None else products,
        }
    }
    if retry_after is not None:
        data["retryAfter"] = retry_after
    return BusinessResponse(status=status, msg="private-upstream-message", request_id="request-bag", data=data)


def seat_response(
    status: int = 0,
    *,
    seats: list[dict[str, object]] | None = None,
    retry_after: object | None = None,
    segment_index: int = 1,
) -> BusinessResponse:
    selected_seats = (
        seats
        if seats is not None
        else [
            {
                "column": "A",
                "seatStatus": "F",
                "seatCharacteristics": ["W", "L"],
                "price": 12.5,
                "currency": "USD",
                "vendorPrice": 12.5,
                "vendorCurrency": "USD",
                "productCode": PRIVATE_SEAT,
            }
        ]
    )
    data: dict[str, object] = {
        "cabins": [
            {
                "segmentIndex": segment_index,
                "cabin": {
                    "cabinLayout": {"columns": [], "rows": {"first": 5, "last": 20}},
                    "rows": [{"number": 5, "seats": selected_seats}],
                },
            }
        ]
    }
    if retry_after is not None:
        data["retryAfter"] = retry_after
    return BusinessResponse(status=status, msg="private-upstream-message", request_id="request-seat", data=data)


def free_seat() -> dict[str, object]:
    return {
        "column": "A",
        "seatStatus": "F",
        "seatCharacteristics": ["W", "L"],
        "price": 12.5,
        "currency": "USD",
        "vendorPrice": 12.5,
        "vendorCurrency": "USD",
        "productCode": PRIVATE_SEAT,
    }


@dataclass
class FakeBusiness:
    baggage_outcomes: list[BusinessResponse | BusinessApiError]
    seat_outcomes: list[BusinessResponse | BusinessApiError]
    baggage_calls: list[tuple[BusinessRoute, ApiCredential, dict[str, object]]] = field(default_factory=list)
    seat_calls: list[tuple[BusinessRoute, ApiCredential, dict[str, object]]] = field(default_factory=list)

    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse:
        if route.operation is BusinessOperation.BAGGAGE:
            self.baggage_calls.append((route, credential, payload))
            outcome = self.baggage_outcomes.pop(0)
        else:
            self.seat_calls.append((route, credential, payload))
            outcome = self.seat_outcomes.pop(0)
        if isinstance(outcome, BusinessApiError):
            raise outcome
        return outcome


@dataclass
class FakeSecrets:
    credentials: Credentials | None = field(
        default_factory=lambda: Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    )

    def load_credentials(self) -> Credentials | None:
        return self.credentials


@dataclass
class FakeAccess:
    generation: str = GENERATION
    calls: list[tuple[str, BusinessOperation]] = field(default_factory=list)

    def resolve_transaction_access(self, jwt: str, operation: BusinessOperation) -> TransactionAccess:
        self.calls.append((jwt, operation))
        return TransactionAccess(
            route=BusinessRoute(
                base_url="https://business.example.invalid",
                path=f"/{operation.value}.do",
                operation=operation,
                credential_slot=CredentialSlot.PRODUCTION,
                generation=self.generation,
            ),
            credential=ApiCredential(ak="private-ak", sk="private-sk"),
        )


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@dataclass
class InterleavingBookingStore:
    inner: BookingStore | None = None
    before_select: Callable[[BookingStore], None] | None = None

    def bind(self, store: BookingStore) -> None:
        self.inner = store

    def current(self) -> BookingStore:
        assert self.inner is not None
        return self.inner

    def load(self, booking_id: str, *, generation: str):
        return self.current().load(booking_id, generation=generation)

    def replace_options(self, booking_id: str, **kwargs: object):
        return self.current().replace_options(booking_id, **kwargs)  # type: ignore[arg-type]

    def close_ancillary(self, booking_id: str, **kwargs: object):
        return self.current().close_ancillary(booking_id, **kwargs)  # type: ignore[arg-type]

    def select(self, booking_id: str, selection: object, **kwargs: object):
        if self.before_select is not None:
            action = self.before_select
            self.before_select = None
            action(self.current())
        return self.current().select(booking_id, selection, **kwargs)  # type: ignore[arg-type]

    def remove(self, booking_id: str, **kwargs: object):
        return self.current().remove(booking_id, **kwargs)  # type: ignore[arg-type]


def tokens() -> Iterator[str]:
    return iter(str(index) for index in range(1, 100))


def make_ancillary_service(
    tmp_path: Path,
    *,
    search_support: tuple[str, ...] = ("baggage", "seat"),
    verify_support: tuple[str, ...] = ("baggage", "seat"),
    baggage_outcomes: list[BusinessResponse | BusinessApiError] | None = None,
    seat_outcomes: list[BusinessResponse | BusinessApiError] | None = None,
    sleep: Callable[[float], None] = lambda _: None,
    retry_after: float = 2,
    access_generation: str = GENERATION,
    connected: bool = False,
    round_trip: bool = False,
    duplicate_index: bool = False,
    store_proxy: InterleavingBookingStore | None = None,
) -> tuple[AncillaryService, FakeBusiness, BookingStore]:
    token_values = tokens()
    store = BookingStore(
        tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=token_values.__next__, now=lambda: NOW
    )
    context = store.create_from_verified(
        booking_seed(
            search_support=search_support,
            verify_support=verify_support,
            connected=connected,
            round_trip=round_trip,
            duplicate_index=duplicate_index,
        )
    )
    assert context.booking_id == "book_1"
    business = FakeBusiness(
        baggage_outcomes=list(baggage_outcomes or [baggage_response()]),
        seat_outcomes=list(seat_outcomes or [seat_response()]),
    )
    if store_proxy is not None:
        store_proxy.bind(store)
    service = AncillaryService(
        secrets=FakeSecrets(),
        access=FakeAccess(generation=access_generation),
        adapter=AncillaryAdapter(
            business,
            token_factory=token_values.__next__,
            default_retry_seconds=retry_after,
        ),
        booking_store=store_proxy or store,
        sleep=sleep,
        default_retry_seconds=retry_after,
    )
    return service, business, store


def assert_private_codes_absent(result: object) -> None:
    encoded = result.model_dump_json()  # type: ignore[union-attr]
    assert PRIVATE_BAGGAGE not in encoded
    assert PRIVATE_SEAT not in encoded


def test_search_unsupported_baggage_skips_api_and_keeps_booking_available(tmp_path: Path) -> None:
    service, business, store = make_ancillary_service(
        tmp_path,
        search_support=(),
        verify_support=("seat",),
    )

    result = service.list_baggage("book_1")

    assert result.code == "BAGGAGE_UNAVAILABLE"
    assert result.status is CommandStatus.SUCCESS
    assert business.baggage_calls == []
    assert store.load("book_1", generation=GENERATION).booking_id == "book_1"
    assert_private_codes_absent(result)


def test_baggage_success_returns_opaque_options_and_hides_product_code(tmp_path: Path) -> None:
    service, business, _ = make_ancillary_service(tmp_path)

    result = service.list_baggage("book_1")

    assert result.code == "BAGGAGE_OPTIONS_LISTED"
    assert result.status is CommandStatus.SUCCESS
    assert result.data["options"] == [
        {
            "baggage_id": "bag_2",
            "segment_id": "seg_1",
            "piece": 1,
            "weight_kg": 20,
            "size": "158CM",
            "category": "StandardCheckInBaggage",
            "price": 30.0,
            "currency": "USD",
        }
    ]
    assert business.baggage_calls[0][2] == {"offerId": "private-session"}
    assert_private_codes_absent(result)


def test_seat_adapter_sends_context_and_filters_occupied_seats(tmp_path: Path) -> None:
    occupied = {
        "column": "B",
        "seatStatus": "O",
        "seatCharacteristics": ["M"],
        "price": 0,
        "currency": "USD",
        "productCode": "PC_OCCUPIED_PRIVATE",
    }
    service, business, _ = make_ancillary_service(
        tmp_path,
        seat_outcomes=[seat_response(seats=[occupied, free_seat()])],
    )

    result = service.list_seats("book_1")

    assert result.code == "SEAT_OPTIONS_LISTED"
    assert result.data["options"] == [
        {
            "seat_id": "seat_2",
            "segment_id": "seg_1",
            "row": 5,
            "column": "A",
            "characteristics": ["W", "L"],
            "price": 12.5,
            "currency": "USD",
        }
    ]
    assert business.seat_calls[0][2] == {
        "sessionId": "private-session",
        "carrier": "AK",
        "outboundSegments": [
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
    }
    assert "PC_OCCUPIED_PRIVATE" not in result.model_dump_json()
    assert_private_codes_absent(result)


def test_status_mapping_happens_before_malformed_seat_normalization_and_closes_only_seat(tmp_path: Path) -> None:
    service, business, store = make_ancillary_service(
        tmp_path,
        seat_outcomes=[BusinessResponse(status=218, msg="private", request_id="req", data={"cabins": "bad"})],
    )

    result = service.list_seats("book_1")

    context = store.load("book_1", generation=GENERATION)
    assert result.code == "SEAT_UNAVAILABLE"
    assert result.status is CommandStatus.SUCCESS
    assert len(business.seat_calls) == 1
    assert context.seat_supported is False
    assert context.baggage_supported is True
    assert_private_codes_absent(result)


@pytest.mark.parametrize(
    ("kind", "method_name", "responses", "expected_code", "expected_status", "capability_stays_open"),
    [
        (
            "baggage",
            "list_baggage",
            [baggage_response(205, retry_after=3), baggage_response(205)],
            "BAGGAGE_UNAVAILABLE",
            CommandStatus.SUCCESS,
            False,
        ),
        (
            "baggage",
            "list_baggage",
            [baggage_response(299, retry_after=3), baggage_response(299)],
            "BAGGAGE_UNAVAILABLE",
            CommandStatus.SUCCESS,
            False,
        ),
        (
            "baggage",
            "list_baggage",
            [baggage_response(9999, retry_after=3), baggage_response(9999)],
            "BAGGAGE_UNAVAILABLE",
            CommandStatus.SUCCESS,
            False,
        ),
        (
            "baggage",
            "list_baggage",
            [baggage_response(429, retry_after=3), baggage_response()],
            "BAGGAGE_OPTIONS_LISTED",
            CommandStatus.SUCCESS,
            True,
        ),
        (
            "baggage",
            "list_baggage",
            [baggage_response(429, retry_after=3), baggage_response(429)],
            "BAGGAGE_UNAVAILABLE",
            CommandStatus.RETRYABLE_ERROR,
            True,
        ),
        (
            "seat",
            "list_seats",
            [seat_response(216, retry_after=3), seat_response(216)],
            "SEAT_UNAVAILABLE",
            CommandStatus.SUCCESS,
            False,
        ),
        (
            "seat",
            "list_seats",
            [seat_response(217, retry_after=3), seat_response(217)],
            "SEAT_UNAVAILABLE",
            CommandStatus.SUCCESS,
            False,
        ),
        (
            "seat",
            "list_seats",
            [seat_response(429, retry_after=3), seat_response()],
            "SEAT_OPTIONS_LISTED",
            CommandStatus.SUCCESS,
            True,
        ),
    ],
)
def test_transient_ancillary_status_retries_once_then_stops(
    kind: str,
    method_name: str,
    responses: list[BusinessResponse],
    expected_code: str,
    expected_status: CommandStatus,
    capability_stays_open: bool,
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    service, business, store = make_ancillary_service(
        tmp_path,
        **{f"{kind}_outcomes": responses},
        sleep=clock.sleep,
        retry_after=2,
    )

    result = getattr(service, method_name)("book_1")

    assert result.code == expected_code
    assert result.status is expected_status
    assert len(getattr(business, f"{kind}_calls")) == 2
    assert clock.sleeps == [3]
    context = store.load("book_1", generation=GENERATION)
    assert getattr(context, f"{kind}_supported") is capability_stays_open
    assert_private_codes_absent(result)


def test_repeated_transport_failure_stays_retryable_without_closing_capability(tmp_path: Path) -> None:
    transient = BusinessApiError(
        code="SERVICE_TEMPORARILY_UNAVAILABLE",
        message="Service temporarily unavailable",
        retryable=True,
    )
    clock = FakeClock()
    service, business, store = make_ancillary_service(
        tmp_path,
        baggage_outcomes=[transient, transient],
        sleep=clock.sleep,
        retry_after=4,
    )

    result = service.list_baggage("book_1")

    assert result.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert result.status is CommandStatus.RETRYABLE_ERROR
    assert len(business.baggage_calls) == 2
    assert clock.sleeps == [4]
    assert store.load("book_1", generation=GENERATION).baggage_supported is True
    assert_private_codes_absent(result)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [(0, 0.0), (60, 60.0), (-1, 7.0), (61, 7.0), (True, 7.0), ("3", 7.0)],
)
def test_retry_after_accepts_only_safe_numeric_values(
    retry_after: object,
    expected: float,
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    service, _, _ = make_ancillary_service(
        tmp_path,
        baggage_outcomes=[baggage_response(429, retry_after=retry_after), baggage_response()],
        sleep=clock.sleep,
        retry_after=7,
    )

    service.list_baggage("book_1")

    assert clock.sleeps == [expected]


@pytest.mark.parametrize(
    ("kind", "method_name", "amount", "outcomes"),
    [
        (
            "baggage",
            "list_baggage",
            float("nan"),
            {"baggage_outcomes": [baggage_response(products=[baggage_product(price=float("nan"))])]},
        ),
        (
            "baggage",
            "list_baggage",
            float("inf"),
            {"baggage_outcomes": [baggage_response(products=[baggage_product(price=float("inf"))])]},
        ),
        (
            "seat",
            "list_seats",
            float("nan"),
            {"seat_outcomes": [seat_response(seats=[{**free_seat(), "price": float("nan")}])]},
        ),
        (
            "seat",
            "list_seats",
            float("inf"),
            {"seat_outcomes": [seat_response(seats=[{**free_seat(), "price": float("inf")}])]},
        ),
    ],
)
def test_non_finite_ancillary_price_is_invalid_without_option_state(
    kind: str,
    method_name: str,
    amount: float,
    outcomes: dict[str, object],
    tmp_path: Path,
) -> None:
    del amount
    service, _, store = make_ancillary_service(tmp_path, **outcomes)  # type: ignore[arg-type]

    result = getattr(service, method_name)("book_1")

    assert result.code == "SERVICE_RESPONSE_INVALID"
    assert result.status is CommandStatus.TERMINAL_ERROR
    context = store.load("book_1", generation=GENERATION)
    assert getattr(context, f"{kind}_options") == ()
    assert getattr(context, f"{kind}_supported") is True
    assert_private_codes_absent(result)


@pytest.mark.parametrize(
    ("kind", "method_name", "response", "expected_code", "expected_status"),
    [
        ("baggage", "list_baggage", baggage_response(products=[]), "BAGGAGE_UNAVAILABLE", CommandStatus.SUCCESS),
        (
            "seat",
            "list_seats",
            seat_response(
                seats=[
                    {
                        "column": "A",
                        "seatStatus": "O",
                        "seatCharacteristics": [],
                        "price": 0,
                        "currency": "USD",
                        "productCode": PRIVATE_SEAT,
                    }
                ]
            ),
            "SEAT_UNAVAILABLE",
            CommandStatus.SUCCESS,
        ),
        ("baggage", "list_baggage", baggage_response(214), "BAGGAGE_UNAVAILABLE", CommandStatus.SUCCESS),
        ("seat", "list_seats", seat_response(214), "BOOKING_EXPIRED", CommandStatus.TERMINAL_ERROR),
    ],
)
def test_empty_or_immediately_unavailable_lookup_closes_only_that_capability(
    kind: str,
    method_name: str,
    response: BusinessResponse,
    expected_code: str,
    expected_status: CommandStatus,
    tmp_path: Path,
) -> None:
    service, business, store = make_ancillary_service(tmp_path, **{f"{kind}_outcomes": [response]})

    result = getattr(service, method_name)("book_1")

    assert result.code == expected_code
    assert result.status is expected_status
    assert len(getattr(business, f"{kind}_calls")) == 1
    context = store.load("book_1", generation=GENERATION)
    assert getattr(context, f"{kind}_supported") is False
    other = "seat" if kind == "baggage" else "baggage"
    assert getattr(context, f"{other}_supported") is True
    assert_private_codes_absent(result)


def test_unknown_body_status_is_terminal_and_does_not_mutate_capability(tmp_path: Path) -> None:
    service, business, store = make_ancillary_service(tmp_path, seat_outcomes=[seat_response(9876)])

    result = service.list_seats("book_1")

    assert result.code == "SERVICE_REQUEST_FAILED"
    assert result.status is CommandStatus.TERMINAL_ERROR
    assert len(business.seat_calls) == 1
    assert store.load("book_1", generation=GENERATION).seat_supported is True
    assert_private_codes_absent(result)


def test_selection_is_bound_to_current_traveler_segment_and_option(tmp_path: Path) -> None:
    service, _, store = make_ancillary_service(tmp_path)
    listed = service.list_seats("book_1")
    seat_id = str(listed.data["options"][0]["seat_id"])  # type: ignore[index]

    result = service.select_seat("book_1", "trav_1", "seg_1", seat_id)

    assert result.code == "SEAT_SELECTED"
    assert result.data == {
        "booking_id": "book_1",
        "traveler_id": "trav_1",
        "segment_id": "seg_1",
        "seat_id": seat_id,
    }
    selection = store.load("book_1", generation=GENERATION).selections[0]
    assert selection.product_code == PRIVATE_SEAT
    assert_private_codes_absent(result)


@pytest.mark.parametrize(
    ("traveler_id", "segment_id", "option_id"),
    [
        ("trav_old", "seg_1", "listed"),
        ("trav_1", "seg_old", "listed"),
        ("trav_1", "seg_1", "seat_old"),
        ("trav_1", "seg_old", "seat_old"),
    ],
)
def test_invalid_or_old_selection_ids_are_rejected_without_private_values(
    traveler_id: str,
    segment_id: str,
    option_id: str,
    tmp_path: Path,
) -> None:
    service, _, store = make_ancillary_service(tmp_path)
    first = service.list_seats("book_1")
    listed_id = str(first.data["options"][0]["seat_id"])  # type: ignore[index]
    selected_option = listed_id if option_id == "listed" else option_id

    result = service.select_seat("book_1", traveler_id, segment_id, selected_option)

    assert result.code == "ANCILLARY_SELECTION_INVALID"
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert store.load("book_1", generation=GENERATION).selections == ()
    assert_private_codes_absent(result)


def test_refresh_rejects_old_option_and_selection_replacement_is_atomic(tmp_path: Path) -> None:
    service, _, store = make_ancillary_service(
        tmp_path,
        seat_outcomes=[
            seat_response(),
            seat_response(
                seats=[
                    {
                        "column": "C",
                        "seatStatus": "F",
                        "seatCharacteristics": ["A"],
                        "price": 20,
                        "currency": "USD",
                        "productCode": "PC_PRIVATE_SEAT_TWO",
                    }
                ]
            ),
        ],
    )
    first = service.list_seats("book_1")
    old_id = str(first.data["options"][0]["seat_id"])  # type: ignore[index]
    service.select_seat("book_1", "trav_1", "seg_1", old_id)
    second = service.list_seats("book_1")
    new_id = str(second.data["options"][0]["seat_id"])  # type: ignore[index]

    assert store.load("book_1", generation=GENERATION).selections == ()

    rejected = service.select_seat("book_1", "trav_1", "seg_1", old_id)
    replaced = service.select_seat("book_1", "trav_1", "seg_1", new_id)

    assert rejected.code == "ANCILLARY_SELECTION_INVALID"
    assert replaced.code == "SEAT_SELECTED"
    selections = store.load("book_1", generation=GENERATION).selections
    assert len(selections) == 1
    assert selections[0].option_id == new_id
    assert selections[0].product_code == "PC_PRIVATE_SEAT_TWO"
    assert_private_codes_absent(rejected)
    assert "PC_PRIVATE_SEAT_TWO" not in replaced.model_dump_json()


def test_remove_selection_validates_binding_and_removes_only_selected_slot(tmp_path: Path) -> None:
    service, _, store = make_ancillary_service(tmp_path)
    listed = service.list_baggage("book_1")
    baggage_id = str(listed.data["options"][0]["baggage_id"])  # type: ignore[index]
    service.select_baggage("book_1", "trav_1", "seg_1", baggage_id)

    invalid = service.remove_baggage("book_1", "trav_old", "seg_1")
    removed = service.remove_baggage("book_1", "trav_1", "seg_1")

    assert invalid.code == "ANCILLARY_SELECTION_INVALID"
    assert removed.code == "BAGGAGE_REMOVED"
    assert removed.data == {"booking_id": "book_1", "traveler_id": "trav_1", "segment_id": "seg_1"}
    assert store.load("book_1", generation=GENERATION).selections == ()
    assert_private_codes_absent(invalid)
    assert_private_codes_absent(removed)


def test_route_generation_change_rejects_list_select_and_remove_without_api_or_write(tmp_path: Path) -> None:
    service, business, store = make_ancillary_service(tmp_path, access_generation="h" * 24)
    saved_before = store.contexts_file.read_text(encoding="utf-8")

    results = (
        service.list_baggage("book_1"),
        service.select_baggage("book_1", "trav_1", "seg_1", "bag_old"),
        service.remove_baggage("book_1", "trav_1", "seg_1"),
    )

    assert [result.code for result in results] == ["OFFER_EXPIRED"] * 3
    assert business.baggage_calls == []
    assert store.contexts_file.read_text(encoding="utf-8") == saved_before
    for result in results:
        assert_private_codes_absent(result)


def test_inconsistent_connected_baggage_is_rejected_before_order(tmp_path: Path) -> None:
    service, _, store = make_ancillary_service(
        tmp_path,
        connected=True,
        baggage_outcomes=[
            baggage_response(
                products=[
                    baggage_product(segment_index=1, weight=20),
                    baggage_product("PC_PRIVATE_BAGGAGE_TWO", segment_index=2, weight=25),
                ]
            )
        ],
    )
    listed = service.list_baggage("book_1")
    first_id = str(listed.data["options"][0]["baggage_id"])  # type: ignore[index]
    second_id = str(listed.data["options"][1]["baggage_id"])  # type: ignore[index]
    first = service.select_baggage("book_1", "trav_1", "seg_1", first_id)

    inconsistent = service.select_baggage("book_1", "trav_1", "seg_2", second_id)

    assert first.code == "BAGGAGE_SELECTED"
    assert inconsistent.code == "ANCILLARY_SELECTION_INVALID"
    selections = store.load("book_1", generation=GENERATION).selections
    assert len(selections) == 1
    assert selections[0].segment_id == "seg_1"
    assert "PC_PRIVATE_BAGGAGE_TWO" not in inconsistent.model_dump_json()
    assert_private_codes_absent(inconsistent)


@pytest.mark.parametrize(
    ("kind", "list_method", "select_method", "option_key", "outcomes"),
    [
        (
            "baggage",
            "list_baggage",
            "select_baggage",
            "baggage_id",
            {"baggage_outcomes": [baggage_response(products=[baggage_product(segment_index=2)])]},
        ),
        (
            "seat",
            "list_seats",
            "select_seat",
            "seat_id",
            {"seat_outcomes": [seat_response(segment_index=2)]},
        ),
    ],
)
def test_round_trip_global_index_maps_to_exact_segment_and_rejects_mismatched_selection(
    kind: str,
    list_method: str,
    select_method: str,
    option_key: str,
    outcomes: dict[str, object],
    tmp_path: Path,
) -> None:
    service, _, store = make_ancillary_service(tmp_path, round_trip=True, **outcomes)  # type: ignore[arg-type]

    listed = getattr(service, list_method)("book_1")
    option_id = str(listed.data["options"][0][option_key])  # type: ignore[index]
    mismatched = getattr(service, select_method)("book_1", "trav_1", "seg_1", option_id)
    selected = getattr(service, select_method)("book_1", "trav_1", "seg_2", option_id)

    assert listed.data["options"][0]["segment_id"] == "seg_2"  # type: ignore[index]
    assert mismatched.code == "ANCILLARY_SELECTION_INVALID"
    assert selected.code == f"{kind.upper()}_SELECTED"
    selections = store.load("book_1", generation=GENERATION).selections
    assert len(selections) == 1
    assert selections[0].segment_id == "seg_2"
    assert selections[0].segment_index == 2
    assert_private_codes_absent(listed)
    assert_private_codes_absent(mismatched)
    assert_private_codes_absent(selected)


@pytest.mark.parametrize(
    ("kind", "method_name", "outcomes", "duplicate_index"),
    [
        (
            "baggage",
            "list_baggage",
            {"baggage_outcomes": [baggage_response(products=[baggage_product(segment_index=99)])]},
            False,
        ),
        (
            "seat",
            "list_seats",
            {"seat_outcomes": [seat_response(segment_index=99)]},
            False,
        ),
        (
            "baggage",
            "list_baggage",
            {"baggage_outcomes": [baggage_response(products=[baggage_product(segment_index=1)])]},
            True,
        ),
        (
            "seat",
            "list_seats",
            {"seat_outcomes": [seat_response(segment_index=1)]},
            True,
        ),
    ],
)
def test_unknown_or_duplicate_ancillary_index_fails_closed_before_selection(
    kind: str,
    method_name: str,
    outcomes: dict[str, object],
    duplicate_index: bool,
    tmp_path: Path,
) -> None:
    service, _, store = make_ancillary_service(
        tmp_path,
        round_trip=True,
        duplicate_index=duplicate_index,
        **outcomes,  # type: ignore[arg-type]
    )

    result = getattr(service, method_name)("book_1")

    assert result.code == "SERVICE_RESPONSE_INVALID"
    assert result.status is CommandStatus.TERMINAL_ERROR
    context = store.load("book_1", generation=GENERATION)
    assert context.selections == ()
    assert getattr(context, f"{kind}_options") == ()
    assert_private_codes_absent(result)


@pytest.mark.parametrize("interleaving", ["refresh", "close"])
def test_locked_store_rejects_service_selection_after_concurrent_option_change(
    interleaving: str,
    tmp_path: Path,
) -> None:
    proxy = InterleavingBookingStore()
    service, _, store = make_ancillary_service(tmp_path, store_proxy=proxy)
    listed = service.list_seats("book_1")
    seat_id = str(listed.data["options"][0]["seat_id"])  # type: ignore[index]

    def change_current_state(inner: BookingStore) -> None:
        if interleaving == "close":
            inner.close_ancillary("book_1", kind=AncillaryKind.SEAT, generation=GENERATION)
            return
        inner.replace_options(
            "book_1",
            kind=AncillaryKind.SEAT,
            options=(
                SeatOption(
                    seat_id="seat_new",
                    product_code="private-seat-new",
                    segment_id="seg_1",
                    segment_index=1,
                    row=8,
                    column="C",
                    price=20,
                    currency="USD",
                ),
            ),
            generation=GENERATION,
        )

    proxy.before_select = change_current_state

    result = service.select_seat("book_1", "trav_1", "seg_1", seat_id)

    assert result.code == "ANCILLARY_SELECTION_INVALID"
    assert result.status is CommandStatus.ACTION_REQUIRED
    context = store.load("book_1", generation=GENERATION)
    assert context.selections == ()
    if interleaving == "close":
        assert context.seat_supported is False
    else:
        assert [item.seat_id for item in context.seat_options] == ["seat_new"]
    assert "private-seat-new" not in result.model_dump_json()
    assert_private_codes_absent(result)
