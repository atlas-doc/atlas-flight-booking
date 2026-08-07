from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atlas_cli.access import TransactionAccess
from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BookingRequirements,
    OrderAttemptState,
    SeatOption,
    SegmentSlot,
    TravelerSlot,
    VerifiedBookingSeed,
)
from atlas_cli.booking_persistence import PersistedBookingState
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.business_client import BusinessApiError, BusinessResponse
from atlas_cli.endpoints import BusinessOperation, BusinessRoute, CredentialSlot
from atlas_cli.models import CommandStatus
from atlas_cli.orders import OrderAdapter, OrderService
from atlas_cli.passengers import PassengerSource
from atlas_cli.search_models import NormalizedOffer, NormalizedPassengerPrice, NormalizedSegment
from atlas_cli.secure_store import ApiCredential, Credentials
from tests.fake_workflow_store import FakeWorkflowSecretStore

NOW = datetime(2026, 8, 5, 8, tzinfo=UTC)
GENERATION = "g" * 24
ORDER_NO = "ATAXA20260721085144583"


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
        direction="outbound",
    )
    return NormalizedOffer(
        upstream_identifier="private-routing",
        currency="USD",
        total_price=100,
        transaction_fee_total=5,
        passenger_prices=[
            NormalizedPassengerPrice(
                passenger_type="adult", count=1, base_fare_per_passenger=75, tax_per_passenger=20, subtotal=95
            )
        ],
        segments=(segment,),
        ancillary_supported=("seat",),
        bookable=True,
        price_status="verified",
    )


def seed(*, searched_total: float = 100, verified_total: float = 100) -> VerifiedBookingSeed:
    current = offer().model_copy(update={"total_price": verified_total})
    searched = offer().model_copy(update={"total_price": searched_total})
    return VerifiedBookingSeed(
        search_id="srch_1",
        offer_id="off_1",
        route_generation=GENERATION,
        routing_identifier="private-routing",
        session_id="private-session",
        searched_offer=searched,
        verified_offer=current,
        requirements=BookingRequirements(required_fields=("name", "passenger_type")),
        travelers=(TravelerSlot(traveler_id="trav_1", passenger_type="adult"),),
        segments=(SegmentSlot(segment_id="seg_1", segment_index=1, direction="outbound", segment=current.segments[0]),),
        expires_at=NOW + timedelta(hours=2),
    )


def source() -> PassengerSource:
    return PassengerSource(
        use_stdin=True,
        file_path=None,
        stdin=io.StringIO(
            '{"passengers":[{"traveler_id":"trav_1","name":"GARCIA/MARIA","passenger_type":"adult","gender":"F","document":{"type":"PP","number":"P1234567"}}],"contact":{"name":"GARCIA/MARIA","email":"maria@example.com"}}'
        ),
    )


def response(
    status: int = 0,
    *,
    balance: bool = True,
    order_no: object = ORDER_NO,
    deadline: str = "2026-08-05 20:00:00",
) -> BusinessResponse:
    return BusinessResponse(
        status=status,
        msg=None,
        request_id="req-order",
        data={
            "orderNo": order_no,
            "totalPrice": 112.5,
            "totalTransactionFee": 5,
            "currency": "USD",
            "tktLimitTime": deadline,
            "paymentOptions": [{"paymentMethod": 1}] if balance else [],
        },
    )


@dataclass
class FakeBusiness:
    outcomes: list[BusinessResponse | BusinessApiError]
    requests: list[dict[str, object]] = field(default_factory=list)

    def post(self, route: BusinessRoute, credential: ApiCredential, payload: dict[str, object]) -> BusinessResponse:
        self.requests.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BusinessApiError):
            raise outcome
        return outcome


@dataclass
class Secrets:
    def load_credentials(self) -> Credentials:
        return Credentials(jwt="jwt", client_code="c", cid="id")


@dataclass
class Access:
    def resolve_transaction_access(self, jwt: str, operation: BusinessOperation) -> TransactionAccess:
        return TransactionAccess(
            route=BusinessRoute(
                "https://business.invalid", "/order.do", operation, CredentialSlot.PRODUCTION, GENERATION
            ),
            credential=ApiCredential(ak="private", sk="private"),
        )

    def order_url(self, order_no: str) -> str:
        return f"https://www.atriptech.com/#/order/detail/{order_no}/en"


def make_service(
    tmp_path: Path,
    outcome: BusinessResponse | BusinessApiError | None = None,
    *,
    searched_total: float = 100,
    verified_total: float = 100,
    order_url: Callable[[str], str | None] | None = None,
) -> tuple[OrderService, FakeBusiness, BookingStore]:
    tokens = iter(["1", "pay"])
    store = BookingStore(tmp_path, secrets=FakeWorkflowSecretStore(), token_factory=tokens.__next__, now=lambda: NOW)
    assert (
        store.create_from_verified(seed(searched_total=searched_total, verified_total=verified_total)).booking_id
        == "book_1"
    )
    business = FakeBusiness([outcome or response()])
    service = OrderService(
        secrets=Secrets(),
        access=Access(),
        adapter=OrderAdapter(business),
        booking_store=store,
        order_url=order_url,
        now=lambda: NOW,
    )
    return service, business, store


@pytest.mark.parametrize(
    ("cli_policy", "upstream_policy"),
    [("continue-without-seat", "STOP_SEAT"), ("cancel-order", "STOP_TICKET"), ("accept-similar-seat", "SIMILAR_SEAT")],
)
def test_order_create_maps_seat_policy_when_a_seat_is_selected(
    tmp_path: Path, cli_policy: str, upstream_policy: str
) -> None:
    service, business, store = make_service(tmp_path)
    context = store.load("book_1", generation=GENERATION)
    seat = SeatOption(
        seat_id="seat_1",
        product_code="private-seat",
        segment_id="seg_1",
        segment_index=1,
        row=5,
        column="A",
        price=12.5,
        currency="USD",
    )
    store.replace_options("book_1", kind=AncillaryKind.SEAT, options=(seat,), generation=GENERATION)
    store.select(
        "book_1",
        AncillarySelection(
            kind=AncillaryKind.SEAT,
            traveler_id="trav_1",
            segment_id="seg_1",
            option_id="seat_1",
            product_code="private-seat",
            segment_index=1,
        ),
        generation=GENERATION,
    )
    assert context.order_attempt_state is OrderAttemptState.READY
    result = service.create("book_1", source(), cli_policy)
    assert result.code == "PAYMENT_CONFIRMATION_REQUIRED"
    assert business.requests[0]["ifSeatOccupied"] == upstream_policy


def test_order_confirmation_is_masked_pii_free_and_current(tmp_path: Path) -> None:
    service, _, store = make_service(tmp_path)
    result = service.create("book_1", source(), None)
    encoded = result.model_dump_json()
    assert result.code == "PAYMENT_CONFIRMATION_REQUIRED"
    assert result.data["order_no"] == ORDER_NO
    assert result.data["order_url"] == f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en"
    assert str(result.data["payment_confirmation_id"]).startswith("paycfm_")
    assert "P1234567" not in encoded and "maria@example.com" not in encoded
    stored = (tmp_path / "contexts.json").read_text()
    assert "P1234567" not in stored and "maria@example.com" not in stored
    assert store.load("book_1", generation=GENERATION).order is not None


def test_order_confirmation_omits_unavailable_public_link(tmp_path: Path) -> None:
    service, _, store = make_service(tmp_path, order_url=lambda _order_no: None)

    result = service.create("book_1", source(), None)

    assert result.code == "PAYMENT_CONFIRMATION_REQUIRED"
    assert result.data["order_no"] == ORDER_NO
    assert "order_url" not in result.data
    order = store.load("book_1", generation=GENERATION).order
    assert order is not None
    assert order.order_url is None


def test_transport_failure_marks_unknown_and_never_retries_order(tmp_path: Path) -> None:
    error = BusinessApiError(code="SERVICE_TEMPORARILY_UNAVAILABLE", message="offline", retryable=True)
    service, business, store = make_service(tmp_path, error)
    result = service.create("book_1", source(), None)
    assert result.code == "ORDER_CREATION_UNKNOWN"
    assert len(business.requests) == 1
    assert store.load("book_1", generation=GENERATION).order_attempt_state is OrderAttemptState.UNKNOWN


@pytest.mark.parametrize(
    ("status", "state", "code"),
    [
        (307, OrderAttemptState.READY, "BOOKING_INPUT_INVALID"),
        (309, OrderAttemptState.READY, "ANCILLARY_SELECTION_INVALID"),
        (318, OrderAttemptState.UNKNOWN, "DUPLICATE_BOOKING_SUSPECTED"),
        (330, OrderAttemptState.UNKNOWN, "ORDER_CREATION_UNKNOWN"),
    ],
)
def test_order_failure_state_tracks_side_effect_certainty(
    tmp_path: Path, status: int, state: OrderAttemptState, code: str
) -> None:
    service, _, store = make_service(tmp_path, response(status))
    assert service.create("book_1", source(), None).code == code
    assert store.load("book_1", generation=GENERATION).order_attempt_state is state


@pytest.mark.parametrize(
    ("upstream_status", "expected_fields"),
    [
        (323, ["contact.email"]),
        (410, ["contact"]),
    ],
)
def test_contact_failures_identify_only_the_fields_to_correct(
    tmp_path: Path,
    upstream_status: int,
    expected_fields: list[str],
) -> None:
    service, _, store = make_service(tmp_path, response(upstream_status))

    result = service.create("book_1", source(), None)

    assert result.code == "CONTACT_INFO_INVALID"
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.details == {"fields": expected_fields}
    assert store.load("book_1", generation=GENERATION).order_attempt_state is OrderAttemptState.READY


def test_missing_balance_method_stores_order_without_confirmation(tmp_path: Path) -> None:
    service, _, store = make_service(tmp_path, response(balance=False))
    result = service.create("book_1", source(), None)
    assert result.code == "PAYMENT_METHOD_UNAVAILABLE"
    assert result.data == {"order_no": ORDER_NO, "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en"}
    assert store.load("book_1", generation=GENERATION).order is not None


def test_missing_order_number_marks_order_unknown(tmp_path: Path) -> None:
    service, business, store = make_service(tmp_path, response(order_no=None))
    assert service.create("book_1", source(), None).code == "ORDER_CREATION_UNKNOWN"
    assert len(business.requests) == 1
    assert store.load("book_1", generation=GENERATION).order_attempt_state is OrderAttemptState.UNKNOWN


def test_local_save_failure_after_real_order_exposes_only_recovery_locator(tmp_path: Path) -> None:
    service, _, store = make_service(tmp_path)
    original_save = store.save_order_with_confirmation

    def fail_save(*args: object, **kwargs: object) -> tuple[object, object]:
        del args, kwargs
        raise BookingStoreError(code="BOOKING_STATE_INVALID", message="local write failed")

    store.save_order_with_confirmation = fail_save  # type: ignore[method-assign]
    result = service.create("book_1", source(), None)
    store.save_order_with_confirmation = original_save  # type: ignore[method-assign]

    assert result.code == "ORDER_CREATION_UNKNOWN"
    assert result.data == {
        "order_no": ORDER_NO,
        "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en",
    }
    assert store.load("book_1", generation=GENERATION).order_attempt_state is OrderAttemptState.UNKNOWN


def test_missing_price_confirmation_requires_confirmation_before_loading_passengers(tmp_path: Path) -> None:
    service, business, store = make_service(tmp_path, searched_total=100, verified_total=110)
    result = service.create("book_1", source(), None)
    assert result.code == "PRICE_CONFIRMATION_REQUIRED"
    assert business.requests == []
    assert store.load("book_1", generation=GENERATION).order_attempt_state is OrderAttemptState.READY


def test_price_decrease_is_disclosed_and_bound_to_persisted_summary(tmp_path: Path) -> None:
    service, _, store = make_service(tmp_path, searched_total=100, verified_total=90)
    result = service.create("book_1", source(), None)
    summary = result.data["payment_summary"]
    assert summary["price_change"] == "decreased"
    assert summary["previous_offer_total"] == 100
    assert summary["current_offer_total"] == 90
    order = store.load("book_1", generation=GENERATION).order
    assert order is not None
    persisted = PersistedBookingState.model_validate_json((tmp_path / "contexts.json").read_text())
    assert persisted.confirmations[0].summary_digest == order.summary_digest
    assert persisted.confirmations[0].expires_at == order.payment_deadline


@pytest.mark.parametrize("policy", [None, "not-a-policy"])
def test_selected_seat_requires_a_valid_policy_but_no_seat_does_not(tmp_path: Path, policy: str | None) -> None:
    service, business, store = make_service(tmp_path)
    seat = SeatOption(
        seat_id="seat_1",
        product_code="private-seat",
        segment_id="seg_1",
        segment_index=1,
        row=5,
        column="A",
        price=12.5,
        currency="USD",
    )
    store.replace_options("book_1", kind=AncillaryKind.SEAT, options=(seat,), generation=GENERATION)
    store.select(
        "book_1",
        AncillarySelection(
            kind=AncillaryKind.SEAT,
            traveler_id="trav_1",
            segment_id="seg_1",
            option_id="seat_1",
            product_code="private-seat",
            segment_index=1,
        ),
        generation=GENERATION,
    )
    assert service.create("book_1", source(), policy).code == "INVALID_ARGUMENT"
    assert business.requests == []
    assert store.load("book_1", generation=GENERATION).order_attempt_state is OrderAttemptState.READY


def test_status_308_expires_context_and_blocks_a_retry(tmp_path: Path) -> None:
    service, business, _ = make_service(tmp_path, response(308))
    assert service.create("book_1", source(), None).code == "PRICE_CHANGED"
    assert service.create("book_1", source(), None).code == "OFFER_EXPIRED"
    assert len(business.requests) == 1


def test_begin_order_context_is_used_for_payload_after_concurrent_seat_selection(tmp_path: Path) -> None:
    service, business, store = make_service(tmp_path)
    seat = SeatOption(
        seat_id="seat_1",
        product_code="private-seat",
        segment_id="seg_1",
        segment_index=1,
        row=5,
        column="A",
        price=12.5,
        currency="USD",
    )
    store.replace_options("book_1", kind=AncillaryKind.SEAT, options=(seat,), generation=GENERATION)
    original_begin = store.begin_order

    def select_then_begin(booking_id: str, *, generation: str):
        store.select(
            "book_1",
            AncillarySelection(
                kind=AncillaryKind.SEAT,
                traveler_id="trav_1",
                segment_id="seg_1",
                option_id="seat_1",
                product_code="private-seat",
                segment_index=1,
            ),
            generation=GENERATION,
        )
        return original_begin(booking_id, generation=generation)

    store.begin_order = select_then_begin  # type: ignore[method-assign]
    result = service.create("book_1", source(), "continue-without-seat")
    assert result.code == "PAYMENT_CONFIRMATION_REQUIRED"
    passenger = business.requests[0]["passengers"][0]
    assert passenger["ancillaries"] == [{"productCode": "private-seat", "segmentIndex": 1}]
    assert business.requests[0]["ifSeatOccupied"] == "STOP_SEAT"


def test_past_payment_deadline_persists_real_order_without_confirmation(tmp_path: Path) -> None:
    service, _, store = make_service(tmp_path, response(deadline="2026-08-05 07:00:00"))
    result = service.create("book_1", source(), None)
    assert result.code == "PAYMENT_DEADLINE_EXPIRED"
    assert result.data == {"order_no": ORDER_NO, "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en"}
    saved = PersistedBookingState.model_validate_json((tmp_path / "contexts.json").read_text())
    assert saved.contexts[0].order is not None and saved.confirmations == ()


def test_expired_context_rejects_order_before_the_one_shot_call(tmp_path: Path) -> None:
    service, business, store = make_service(tmp_path)
    store.expire_context("book_1", generation=GENERATION)
    assert service.create("book_1", source(), None).code == "OFFER_EXPIRED"
    assert business.requests == []


def test_nonexistent_context_rejects_before_reading_passenger_source_or_calling_order(tmp_path: Path) -> None:
    class ForbiddenPassengerInput:
        def read(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            raise AssertionError("passenger PII must not be read for an invalid booking context")

    service, business, _ = make_service(tmp_path)
    invalid_source = PassengerSource(use_stdin=True, file_path=None, stdin=ForbiddenPassengerInput())  # type: ignore[arg-type]
    result = service.create("book_does_not_exist", invalid_source, None)
    assert result.code == "OFFER_EXPIRED"
    assert business.requests == []


def test_no_selected_seat_ignores_an_irrelevant_invalid_seat_policy(tmp_path: Path) -> None:
    service, business, _ = make_service(tmp_path)
    result = service.create("book_1", source(), "not-a-policy")
    assert result.code == "PAYMENT_CONFIRMATION_REQUIRED"
    assert "ifSeatOccupied" not in business.requests[0]


def test_raw_passenger_pii_is_absent_from_captured_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    service, _, _ = make_service(tmp_path)
    result = service.create("book_1", source(), None)
    assert result.code == "PAYMENT_CONFIRMATION_REQUIRED"
    assert "P1234567" not in caplog.text and "maria@example.com" not in caplog.text
