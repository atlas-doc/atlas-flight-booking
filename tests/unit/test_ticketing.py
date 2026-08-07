from __future__ import annotations

from dataclasses import dataclass, field

from atlas_cli.access import TransactionAccess
from atlas_cli.api_client import ApiClientError
from atlas_cli.booking_models import (
    MaskedPassengerSummary,
    OrderState,
    PaymentState,
    PaymentSummary,
    TicketingState,
)
from atlas_cli.business_client import BusinessApiError, BusinessResponse
from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import BusinessOperation, BusinessRoute, CredentialSlot, EndpointResolver
from atlas_cli.models import CommandStatus
from atlas_cli.secure_store import ApiCredential, Credentials
from atlas_cli.ticketing import QueryOrderAdapter, TicketingService

ORDER_NO = "ATAXA20260721085144583"


def order_response(
    order_status: str,
    ticket_status: str,
    ticket_numbers: list[str],
    *,
    order_no: object = ORDER_NO,
    passengers: object | None = None,
) -> BusinessResponse:
    return BusinessResponse(
        status=0,
        msg=None,
        request_id="req-query",
        data={
            "orderNo": order_no,
            "orderStatus": order_status,
            "ticketStatus": ticket_status,
            "paxTicketInfos": passengers
            if passengers is not None
            else [{"airlinePNRs": ["PNR123"], "ticketNos": ticket_numbers, "name": "PRIVATE/NAME"}],
        },
    )


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class FakeBusiness:
    outcomes: list[BusinessResponse | BusinessApiError]
    timeouts: list[float] = field(default_factory=list)
    payloads: list[dict[str, object]] = field(default_factory=list)

    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
        *,
        request_timeout_seconds: float | None = None,
    ) -> BusinessResponse:
        del route, credential
        self.payloads.append(payload)
        if request_timeout_seconds is not None:
            self.timeouts.append(request_timeout_seconds)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BusinessApiError):
            raise outcome
        return outcome


class Secrets:
    def load_credentials(self) -> Credentials:
        return Credentials(jwt="jwt", client_code="client", cid="id")


class Access:
    def __init__(self) -> None:
        self.deadlines: list[float | None] = []

    def resolve_transaction_access(
        self,
        jwt: str,
        operation: BusinessOperation,
        *,
        deadline: float | None = None,
        monotonic=None,
    ) -> TransactionAccess:
        assert jwt == "jwt"
        assert operation is BusinessOperation.QUERY_ORDER
        del monotonic
        self.deadlines.append(deadline)
        return TransactionAccess(
            route=BusinessRoute(
                "https://business.invalid", "/queryOrderDetails.do", operation, CredentialSlot.PRODUCTION, "g" * 24
            ),
            credential=ApiCredential(ak="private", sk="private"),
        )

    def order_url(self, order_no: str) -> str:
        return f"https://www.atriptech.com/#/order/detail/{order_no}/en"


class TimedAccess(Access):
    def __init__(self, clock: FakeClock, elapsed_seconds: float) -> None:
        super().__init__()
        self._clock = clock
        self._elapsed_seconds = elapsed_seconds

    def resolve_transaction_access(
        self,
        jwt: str,
        operation: BusinessOperation,
        *,
        deadline: float | None = None,
        monotonic=None,
    ) -> TransactionAccess:
        access = super().resolve_transaction_access(jwt, operation, deadline=deadline, monotonic=monotonic)
        self._clock.now += self._elapsed_seconds
        return access


@dataclass
class Store:
    order: OrderState | None
    payment_updates: list[PaymentState] = field(default_factory=list)
    ticketing_updates: list[tuple[TicketingState, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)

    def load_order(self, order_no: str) -> OrderState:
        if self.order is None or self.order.order_no != order_no:
            from atlas_cli.booking_store import BookingStoreError

            raise BookingStoreError(code="ORDER_NOT_FOUND", message="Order could not be found")
        return self.order

    def update_payment(self, order_no: str, state: PaymentState) -> OrderState:
        assert self.order is not None and self.order.order_no == order_no
        self.payment_updates.append(state)
        self.order = self.order.model_copy(update={"payment_state": state})
        return self.order

    def update_ticketing(
        self,
        order_no: str,
        state: TicketingState,
        *,
        airline_pnrs: tuple[str, ...] = (),
        ticket_numbers: tuple[str, ...] = (),
    ) -> OrderState:
        assert self.order is not None and self.order.order_no == order_no
        self.ticketing_updates.append((state, airline_pnrs, ticket_numbers))
        self.order = self.order.model_copy(
            update={"ticketing_state": state, "airline_pnrs": airline_pnrs, "ticket_numbers": ticket_numbers}
        )
        return self.order


def local_order(payment_state: PaymentState = PaymentState.SUBMITTED, passenger_count: int = 1) -> OrderState:
    passengers = tuple(
        MaskedPassengerSummary(traveler_id=f"trav_{index}", name="G***/M***", document="****4567")
        for index in range(passenger_count)
    )
    return OrderState(
        order_no=ORDER_NO,
        order_url=f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en",
        total_price=100,
        transaction_fee=5,
        currency="USD",
        payment_deadline="2026-08-06T00:00:00Z",
        summary=PaymentSummary(
            ticket_price=95,
            baggage_total=0,
            seat_total=0,
            total_price=100,
            currency="USD",
            passengers=passengers,
        ),
        summary_digest="digest",
        payment_state=payment_state,
    )


def make_ticketing_service(
    responses: list[BusinessResponse | BusinessApiError],
    *,
    local_payment_state: PaymentState = PaymentState.SUBMITTED,
    passenger_count: int = 1,
    monotonic=None,
    sleep=None,
    access: Access | None = None,
) -> tuple[TicketingService, FakeBusiness, Store]:
    business = FakeBusiness(responses)
    store = Store(local_order(local_payment_state, passenger_count))
    resolver = access or Access()
    service = TicketingService(
        secrets=Secrets(),
        access=resolver,
        adapter=QueryOrderAdapter(business),
        booking_store=store,
        order_url=resolver.order_url,
        monotonic=monotonic,
        sleep=sleep,
    )
    return service, business, store


def test_ticketed_requires_final_order_and_ticket_state() -> None:
    service, business, store = make_ticketing_service([order_response("2", "1", ["7811234567890"])])

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "TICKETED"
    assert result.data["ticket_numbers"] == ["7811234567890"]
    assert result.data["order_url"].endswith(f"/{ORDER_NO}/en")
    assert store.ticketing_updates[-1][0] is TicketingState.TICKETED
    assert business.payloads == [{"orderNo": ORDER_NO}]


def test_polling_stops_at_120_seconds_and_returns_successful_pending() -> None:
    clock = FakeClock()
    service, business, _ = make_ticketing_service(
        [order_response("1", "0", []) for _ in range(20)], monotonic=clock.monotonic, sleep=clock.sleep
    )

    result = service.poll(ORDER_NO, timeout_seconds=999)

    assert result.status is CommandStatus.SUCCESS
    assert result.code == "TICKETING_PENDING"
    assert clock.monotonic() == 120
    assert clock.sleeps == [1, 2, 3, 5, 8, 13, 21, 30, 30, 7]
    assert all(timeout > 0 for timeout in business.timeouts)


def test_poll_resolves_access_once_and_passes_exact_decreasing_remaining_budgets() -> None:
    clock = FakeClock()
    access = TimedAccess(clock, elapsed_seconds=10)
    service, business, _ = make_ticketing_service(
        [order_response("1", "0", []), order_response("2", "1", ["7811234567890"])],
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        access=access,
    )

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "TICKETED"
    assert access.deadlines == [120]
    assert business.timeouts == [110, 109]


def test_poll_does_not_start_query_after_access_consumes_remaining_budget() -> None:
    clock = FakeClock()
    access = TimedAccess(clock, elapsed_seconds=120)
    service, business, _ = make_ticketing_service(
        [order_response("2", "1", ["7811234567890"])],
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        access=access,
    )

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "TICKETING_PENDING"
    assert access.deadlines == [120]
    assert business.payloads == []


def test_unpaid_after_payment_is_unknown_not_a_second_payment_signal() -> None:
    service, _, store = make_ticketing_service([order_response("0", "0", [])])

    result = service.query_once(ORDER_NO)

    assert result.code == "PAYMENT_STATUS_UNKNOWN"
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert store.payment_updates == [PaymentState.UNKNOWN]


def test_unpaid_local_payment_constraints_remain_terminal_or_confirmation_required() -> None:
    unavailable, _, _ = make_ticketing_service(
        [order_response("0", "0", [])], local_payment_state=PaymentState.UNAVAILABLE
    )
    awaiting, _, _ = make_ticketing_service(
        [order_response("0", "0", [])], local_payment_state=PaymentState.AWAITING_CONFIRMATION
    )

    assert unavailable.query_once(ORDER_NO).code == "PAYMENT_METHOD_UNAVAILABLE"
    assert awaiting.query_once(ORDER_NO).code == "PAYMENT_CONFIRMATION_REQUIRED"


def test_ticketed_waits_for_all_local_passenger_ticket_numbers() -> None:
    clock = FakeClock()
    service, _, _ = make_ticketing_service(
        [
            order_response("2", "1", ["7811234567890"]),
            order_response("2", "1", ["7811234567890", "7811234567891"]),
        ],
        passenger_count=2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "TICKETED"
    assert len(result.data["ticket_numbers"]) == 2
    assert clock.sleeps == [1]


def test_whitespace_and_duplicate_ticket_numbers_do_not_inflate_local_ticket_count() -> None:
    service, _, _ = make_ticketing_service(
        [order_response("2", "1", ["7811234567890", " 7811234567890 "])], passenger_count=2
    )

    result = service.query_once(ORDER_NO)

    assert result.code == "TICKETING_PENDING"
    assert result.data["ticket_numbers"] == ["7811234567890"]


def test_cancelled_order_is_terminal_and_persists_safe_state() -> None:
    service, _, store = make_ticketing_service([order_response("-3", "0", [])])

    result = service.query_once(ORDER_NO)

    assert result.status is CommandStatus.TERMINAL_ERROR
    assert result.code == "ORDER_CANCELLED"
    assert result.data == {"order_no": ORDER_NO, "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en"}
    assert store.ticketing_updates[-1][0] is TicketingState.CANCELLED


def test_retryable_query_status_is_returned_once_then_retried_by_poll() -> None:
    clock = FakeClock()
    retryable = BusinessResponse(status=705, msg=None, request_id="req-retry", data={})
    service, _, _ = make_ticketing_service(
        [retryable, order_response("2", "1", ["7811234567890"])], monotonic=clock.monotonic, sleep=clock.sleep
    )

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "TICKETED"
    assert clock.sleeps == [1]


def test_retryable_transport_failure_is_retried_within_the_same_budget() -> None:
    clock = FakeClock()
    unavailable = BusinessApiError(
        code="SERVICE_TEMPORARILY_UNAVAILABLE", message="private transport failure", retryable=True
    )
    service, _, _ = make_ticketing_service(
        [unavailable, order_response("2", "1", ["7811234567890"])],
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "TICKETED"
    assert clock.sleeps == [1]


def test_non_documented_retryable_error_returns_immediately() -> None:
    credential_rejected = BusinessApiError(code="CREDENTIAL_REJECTED", message="private", retryable=True)
    clock = FakeClock()
    service, business, _ = make_ticketing_service([credential_rejected], monotonic=clock.monotonic, sleep=clock.sleep)

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "CREDENTIAL_REJECTED"
    assert result.retryable is True
    assert business.payloads == [{"orderNo": ORDER_NO}]
    assert clock.sleeps == []


def test_repeated_documented_query_transients_exhaust_as_successful_pending() -> None:
    clock = FakeClock()
    service, _, _ = make_ticketing_service(
        [BusinessResponse(status=705, msg=None, request_id="req", data={}) for _ in range(20)],
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = service.poll(ORDER_NO, timeout_seconds=120)

    assert result.code == "TICKETING_PENDING"
    assert clock.sleeps == [1, 2, 3, 5, 8, 13, 21, 30, 30, 7]


def test_not_found_and_malformed_pii_rich_responses_are_safe() -> None:
    not_found = BusinessResponse(status=800, msg="private upstream message", request_id="req-missing", data={})
    service, _, _ = make_ticketing_service([not_found])
    missing = service.query_once(ORDER_NO)
    assert missing.code == "ORDER_NOT_FOUND"
    assert missing.data == {"order_no": ORDER_NO, "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en"}

    malformed = order_response("2", "1", [], passengers=[{"ticketNos": "P1234567", "name": "SECRET/NAME"}])
    service, _, _ = make_ticketing_service([malformed])
    invalid = service.query_once(ORDER_NO)
    assert invalid.code == "SERVICE_RESPONSE_INVALID"
    assert "P1234567" not in invalid.model_dump_json()
    assert "SECRET/NAME" not in invalid.model_dump_json()


def test_external_order_never_exposes_local_passengers_or_requires_unknown_count() -> None:
    business = FakeBusiness([order_response("2", "1", ["7811234567890"])])
    from atlas_cli.booking_store import BookingStoreError

    class MissingStore(Store):
        def load_order(self, order_no: str) -> OrderState:
            raise BookingStoreError(code="ORDER_NOT_FOUND", message="Order could not be found")

    service = TicketingService(
        secrets=Secrets(),
        access=Access(),
        adapter=QueryOrderAdapter(business),
        booking_store=MissingStore(None),
        order_url=Access().order_url,
    )

    result = service.query_once(ORDER_NO)

    assert result.code == "TICKETED"
    assert result.data == {
        "order_no": ORDER_NO,
        "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en",
        "airline_pnrs": ["PNR123"],
        "ticket_numbers": ["7811234567890"],
    }


def test_invalid_local_state_does_not_block_external_order_query_or_persistence() -> None:
    business = FakeBusiness([order_response("2", "1", ["7811234567890"])])
    from atlas_cli.booking_store import BookingStoreError

    class InvalidStore(Store):
        def load_order(self, order_no: str) -> OrderState:
            raise BookingStoreError(code="BOOKING_STATE_INVALID", message="Local data could not be processed")

        def update_payment(self, order_no: str, state: PaymentState) -> OrderState:
            raise AssertionError("invalid local state must not be updated")

        def update_ticketing(
            self,
            order_no: str,
            state: TicketingState,
            *,
            airline_pnrs: tuple[str, ...] = (),
            ticket_numbers: tuple[str, ...] = (),
        ) -> OrderState:
            raise AssertionError("invalid local state must not be updated")

    service = TicketingService(
        secrets=Secrets(),
        access=Access(),
        adapter=QueryOrderAdapter(business),
        booking_store=InvalidStore(None),
        order_url=Access().order_url,
    )

    result = service.query_once(ORDER_NO)

    assert result.code == "TICKETED"
    assert "passengers" not in result.data


def test_control_plane_api_failure_is_returned_as_a_stable_result() -> None:
    class FailingAccess(Access):
        def resolve_transaction_access(
            self,
            jwt: str,
            operation: BusinessOperation,
            *,
            deadline: float | None = None,
            monotonic=None,
        ) -> TransactionAccess:
            del jwt, operation, deadline, monotonic
            raise ApiClientError(
                code="SERVICE_TEMPORARILY_UNAVAILABLE", message="private control failure", retryable=True
            )

    service = TicketingService(
        secrets=Secrets(),
        access=FailingAccess(),
        adapter=QueryOrderAdapter(FakeBusiness([])),
        booking_store=Store(local_order()),
        order_url=Access().order_url,
    )

    result = service.query_once(ORDER_NO)

    assert result.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert result.data == {"order_no": ORDER_NO, "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en"}


def test_configured_resolver_order_url_is_used_for_external_order() -> None:
    resolver = EndpointResolver(InternalSettings(order_detail_url_template="https://orders.example/{order_no}/detail"))
    business = FakeBusiness([order_response("2", "1", ["7811234567890"])])
    from atlas_cli.booking_store import BookingStoreError

    class MissingStore(Store):
        def load_order(self, order_no: str) -> OrderState:
            raise BookingStoreError(code="ORDER_NOT_FOUND", message="Order could not be found")

    service = TicketingService(
        secrets=Secrets(),
        access=Access(),
        adapter=QueryOrderAdapter(business),
        booking_store=MissingStore(None),
        order_url=resolver.order_url,
    )

    assert service.query_once(ORDER_NO).data["order_url"] == f"https://orders.example/{ORDER_NO}/detail"


def test_order_query_omits_unavailable_public_link() -> None:
    business = FakeBusiness([order_response("2", "1", ["7811234567890"])])
    service = TicketingService(
        secrets=Secrets(),
        access=Access(),
        adapter=QueryOrderAdapter(business),
        booking_store=Store(local_order()),
        order_url=lambda _order_no: None,
    )

    result = service.query_once(ORDER_NO)

    assert result.code == "TICKETED"
    assert result.data["order_no"] == ORDER_NO
    assert "order_url" not in result.data


def test_normalizer_rejects_order_number_mismatch_without_echoing_it() -> None:
    service, _, _ = make_ticketing_service([order_response("2", "1", [], order_no="PRIVATE-OTHER-ORDER")])

    result = service.query_once(ORDER_NO)

    assert result.code == "SERVICE_RESPONSE_INVALID"
    assert "PRIVATE-OTHER-ORDER" not in result.model_dump_json()
