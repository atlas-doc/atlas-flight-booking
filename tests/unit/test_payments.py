from __future__ import annotations

import json
import multiprocessing
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

import portalocker
import pytest

from atlas_cli.access import AccessManagerError, TransactionAccess
from atlas_cli.booking_models import (
    MaskedPassengerSummary,
    OrderState,
    PaymentState,
    PaymentSummary,
)
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.business_client import BusinessApiError, BusinessResponse
from atlas_cli.durable_io import durable_replace as real_durable_replace
from atlas_cli.endpoints import BusinessOperation, BusinessRoute, CredentialSlot
from atlas_cli.models import (
    CommandResult,
    CommandStatus,
    action_required_result,
    retryable_error_result,
    success_result,
)
from atlas_cli.payments import PaymentAdapter, PaymentService
from atlas_cli.secure_store import ApiCredential, Credentials
from tests.fake_workflow_store import FakeWorkflowSecretStore
from tests.unit.test_booking_store import confirmation_seed, seeded_store

ORDER_NO = "ATAXA20260721085144583"
FIXED_NOW = datetime(2026, 8, 5, 8, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


def local_order(
    *,
    payment_state: PaymentState = PaymentState.AWAITING_CONFIRMATION,
    payment_deadline: datetime = FIXED_NOW + timedelta(hours=1),
) -> OrderState:
    return OrderState(
        order_no=ORDER_NO,
        order_url=f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en",
        total_price=105,
        transaction_fee=5,
        currency="USD",
        payment_deadline=payment_deadline,
        summary=PaymentSummary(
            ticket_price=100,
            baggage_total=0,
            seat_total=0,
            total_price=105,
            currency="USD",
            passengers=(MaskedPassengerSummary(traveler_id="trav_1", name="M***/A***"),),
        ),
        summary_digest="digest-safe",
        payment_state=payment_state,
    )


@dataclass
class AtomicPaymentStore:
    order: OrderState = field(default_factory=local_order)
    confirmation_id: str = "paycfm_1"
    confirmation_deadline: datetime = FIXED_NOW + timedelta(minutes=10)
    binding_matches: bool = True
    consumed: bool = False
    consume_calls: int = 0
    updates: list[PaymentState] = field(default_factory=list)
    consume_error: Exception | None = None
    update_error: Exception | None = None
    lock: Lock = field(default_factory=Lock)

    def consume_payment_confirmation(self, confirmation_id: str, *, now: datetime) -> OrderState:
        with self.lock:
            self.consume_calls += 1
            if self.consume_error is not None:
                raise self.consume_error
            if (
                confirmation_id != self.confirmation_id
                or self.consumed
                or now >= self.confirmation_deadline
                or now >= self.order.payment_deadline
                or not self.binding_matches
                or self.order.payment_state is not PaymentState.AWAITING_CONFIRMATION
            ):
                raise BookingStoreError(
                    code="PAYMENT_CONFIRMATION_INVALID",
                    message="Payment confirmation is invalid or expired",
                )
            self.consumed = True
            self.order = self.order.model_copy(update={"payment_state": PaymentState.PAYING})
            return self.order

    def update_payment(self, order_no: str, state: PaymentState) -> OrderState:
        assert order_no == self.order.order_no
        self.updates.append(state)
        if self.update_error is not None:
            raise self.update_error
        self.order = self.order.model_copy(update={"payment_state": state})
        return self.order


@dataclass
class FakeBusiness:
    outcome: BusinessResponse | BusinessApiError
    requests: list[dict[str, object]] = field(default_factory=list)
    operations: list[BusinessOperation] = field(default_factory=list)

    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse:
        assert credential.ak == "private-ak"
        assert credential.sk == "private-sk"
        self.operations.append(route.operation)
        self.requests.append(payload)
        if isinstance(self.outcome, BusinessApiError):
            raise self.outcome
        return self.outcome


class Secrets:
    def __init__(self, credentials: Credentials | None = None) -> None:
        self.credentials = credentials if credentials is not None else Credentials(jwt="jwt", client_code="c", cid="i")

    def load_credentials(self) -> Credentials | None:
        return self.credentials


class MissingSecrets(Secrets):
    def __init__(self) -> None:
        super().__init__()

    def load_credentials(self) -> None:
        return None


class Access:
    def __init__(self, error: AccessManagerError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, BusinessOperation]] = []

    def resolve_transaction_access(self, jwt: str, operation: BusinessOperation) -> TransactionAccess:
        self.calls.append((jwt, operation))
        if self.error is not None:
            raise self.error
        return TransactionAccess(
            route=BusinessRoute(
                "https://business.invalid",
                "/pay.do",
                operation,
                CredentialSlot.PRODUCTION,
                "g" * 24,
            ),
            credential=ApiCredential(ak="private-ak", sk="private-sk"),
        )


@dataclass
class Ticketing:
    result: CommandResult = field(
        default_factory=lambda: success_result(
            "TICKETED",
            "Tickets have been issued",
            data={
                "order_no": ORDER_NO,
                "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en",
            },
        )
    )
    poll_calls: list[tuple[str, float]] = field(default_factory=list)

    def poll(self, order_no: str, *, timeout_seconds: float) -> CommandResult:
        self.poll_calls.append((order_no, timeout_seconds))
        return self.result


def pay_response(status: int = 0, *, order_no: object = ORDER_NO) -> BusinessResponse:
    return BusinessResponse(
        status=status,
        msg="PRIVATE UPSTREAM MESSAGE",
        request_id="req-pay-safe",
        data={"orderNo": order_no},
    )


def make_payment_service(
    *,
    pay_status: int = 0,
    response_order_no: object = ORDER_NO,
    business_error: BusinessApiError | None = None,
    store: AtomicPaymentStore | None = None,
    secrets: Secrets | None = None,
    access: Access | None = None,
    ticketing: Ticketing | None = None,
    order_url: Callable[[str], str | None] | None = None,
) -> tuple[PaymentService, FakeBusiness, Ticketing, AtomicPaymentStore, Access]:
    business = FakeBusiness(business_error or pay_response(pay_status, order_no=response_order_no))
    payment_store = store or AtomicPaymentStore()
    ticketing_service = ticketing or Ticketing()
    access_resolver = access or Access()
    service = PaymentService(
        secrets=secrets or Secrets(),
        access=access_resolver,
        adapter=PaymentAdapter(business),
        booking_store=payment_store,
        ticketing=ticketing_service,
        order_url=order_url,
        now=fixed_now,
    )
    return service, business, ticketing_service, payment_store, access_resolver


class ProcessFileBusiness:
    def __init__(self, recorder_path: Path) -> None:
        self._recorder_path = recorder_path

    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse:
        assert route.operation is BusinessOperation.PAY
        assert credential.ak == "private-ak"
        assert payload == {"orderNo": ORDER_NO, "paymentMethod": 1}
        with portalocker.Lock(str(self._recorder_path), mode="a", timeout=10) as recorder:
            recorder.write(f"{ORDER_NO}\n")
            recorder.flush()
            os.fsync(recorder.fileno())
        return pay_response()


def _run_payment_process(
    state_directory: str,
    confirmation_id: str,
    recorder_path: str,
    start_event,
    result_queue,
) -> None:
    try:
        store = BookingStore(Path(state_directory), secrets=FakeWorkflowSecretStore(), now=fixed_now)
        service = PaymentService(
            secrets=Secrets(),
            access=Access(),
            adapter=PaymentAdapter(ProcessFileBusiness(Path(recorder_path))),
            booking_store=store,
            ticketing=Ticketing(),
            now=fixed_now,
        )
        if not start_event.wait(timeout=10):
            result_queue.put("WORKER_START_TIMEOUT")
            return
        result_queue.put(service.pay(confirmation_id).code)
    except Exception as error:
        result_queue.put(f"WORKER_ERROR:{type(error).__name__}")


def test_payment_sends_balance_method_once_then_polls() -> None:
    service, business, ticketing, store, access = make_payment_service()

    result = service.pay("paycfm_1")

    assert business.requests == [{"orderNo": ORDER_NO, "paymentMethod": 1}]
    assert business.operations == [BusinessOperation.PAY]
    assert access.calls == [("jwt", BusinessOperation.PAY)]
    assert ticketing.poll_calls == [(ORDER_NO, 120.0)]
    assert store.updates == [PaymentState.SUBMITTED]
    assert result.code == "TICKETED"


def test_payment_begins_only_after_durable_confirmation_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = seeded_store(tmp_path)
    confirmation = store.issue_payment_confirmation("book_1", confirmation_seed())
    events: list[str] = []
    def recording_replace(source: str | Path, destination: str | Path) -> None:
        events.append("replace")
        real_durable_replace(Path(source), Path(destination))

    class OrderingBusiness(FakeBusiness):
        def post(
            self,
            route: BusinessRoute,
            credential: ApiCredential,
            payload: dict[str, object],
        ) -> BusinessResponse:
            events.append("pay")
            return super().post(route, credential, payload)

    monkeypatch.setattr("atlas_cli.booking_store.durable_replace", recording_replace)
    business = OrderingBusiness(pay_response())
    service = PaymentService(
        secrets=Secrets(),
        access=Access(),
        adapter=PaymentAdapter(business),
        booking_store=store,
        ticketing=Ticketing(),
        now=fixed_now,
    )

    result = service.pay(confirmation.confirmation_id)

    assert result.code == "TICKETED"
    assert events[:2] == ["replace", "pay"]


def test_durable_replace_failure_prevents_payment_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = seeded_store(tmp_path)
    confirmation = store.issue_payment_confirmation("book_1", confirmation_seed())

    def fail_durable_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("simulated durability failure")

    monkeypatch.setattr("atlas_cli.booking_store.durable_replace", fail_durable_replace)
    business = FakeBusiness(pay_response())
    service = PaymentService(
        secrets=Secrets(),
        access=Access(),
        adapter=PaymentAdapter(business),
        booking_store=store,
        ticketing=Ticketing(),
        now=fixed_now,
    )

    result = service.pay(confirmation.confirmation_id)

    assert result.code == "SERVICE_REQUEST_FAILED"
    assert business.requests == []


@pytest.mark.parametrize(
    ("pay_status", "saved_state"),
    [
        (402, PaymentState.UNKNOWN),
        (404, PaymentState.UNKNOWN),
        (406, PaymentState.PROCESSING),
        (411, PaymentState.UNKNOWN),
        (615, PaymentState.PROCESSING),
    ],
)
def test_uncertain_or_processing_payment_never_repeats_pay(
    pay_status: int,
    saved_state: PaymentState,
) -> None:
    service, business, ticketing, store, _ = make_payment_service(pay_status=pay_status)

    result = service.pay("paycfm_1")

    assert len(business.requests) == 1
    assert ticketing.poll_calls == [(ORDER_NO, 120.0)]
    assert store.updates == [saved_state]
    assert result.code in {"TICKETED", "TICKETING_PENDING", "PAYMENT_STATUS_UNKNOWN"}


def test_confirmation_cannot_be_reused_even_after_transport_timeout() -> None:
    service, business, ticketing, store, _ = make_payment_service(
        business_error=BusinessApiError(
            code="SERVICE_TEMPORARILY_UNAVAILABLE",
            message="Service temporarily unavailable",
            retryable=True,
        ),
        ticketing=Ticketing(
            action_required_result(
                "PAYMENT_STATUS_UNKNOWN",
                "Payment status could not be confirmed",
                data={"order_no": ORDER_NO, "order_url": f"https://www.atriptech.com/orders/{ORDER_NO}"},
            )
        ),
    )

    first = service.pay("paycfm_1")
    second = service.pay("paycfm_1")

    assert first.code == "PAYMENT_STATUS_UNKNOWN"
    assert second.code == "PAYMENT_CONFIRMATION_INVALID"
    assert len(business.requests) == 1
    assert len(ticketing.poll_calls) == 1
    assert store.updates == [PaymentState.UNKNOWN]


@pytest.mark.parametrize("pay_transport_uncertain", [False, True])
def test_retryable_status_query_after_payment_is_non_retryable_unknown(
    pay_transport_uncertain: bool,
) -> None:
    ticketing = Ticketing(
        retryable_error_result(
            "SERVICE_TEMPORARILY_UNAVAILABLE",
            "Service temporarily unavailable",
            request_id="req-query-safe",
            data={"order_no": ORDER_NO},
        )
    )
    business_error = (
        BusinessApiError(
            code="SERVICE_TEMPORARILY_UNAVAILABLE",
            message="Service temporarily unavailable",
            retryable=True,
        )
        if pay_transport_uncertain
        else None
    )
    service, business, _, store, _ = make_payment_service(
        business_error=business_error,
        ticketing=ticketing,
    )

    result = service.pay("paycfm_1")

    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.code == "PAYMENT_STATUS_UNKNOWN"
    assert result.message == "Payment status could not be confirmed"
    assert result.retryable is False
    assert result.request_id == "req-query-safe"
    assert result.data == {
        "order_no": ORDER_NO,
        "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en",
    }
    assert business.requests == [{"orderNo": ORDER_NO, "paymentMethod": 1}]
    assert ticketing.poll_calls == [(ORDER_NO, 120.0)]
    assert store.consumed is True


@pytest.mark.parametrize("response_order_no", ["UNEXPECTED-ORDER", "", None])
def test_invalid_response_order_number_queries_only_original_order(response_order_no: object) -> None:
    service, business, ticketing, store, _ = make_payment_service(response_order_no=response_order_no)

    result = service.pay("paycfm_1")

    assert len(business.requests) == 1
    assert ticketing.poll_calls == [(ORDER_NO, 120.0)]
    assert store.updates == [PaymentState.UNKNOWN]
    assert result.code in {"TICKETED", "TICKETING_PENDING", "PAYMENT_STATUS_UNKNOWN"}


@pytest.mark.parametrize(
    "store",
    [
        AtomicPaymentStore(confirmation_deadline=FIXED_NOW),
        AtomicPaymentStore(order=local_order(payment_deadline=FIXED_NOW)),
        AtomicPaymentStore(binding_matches=False),
    ],
)
def test_invalid_current_confirmation_never_reaches_payment(store: AtomicPaymentStore) -> None:
    service, business, ticketing, _, _ = make_payment_service(store=store)

    result = service.pay("paycfm_1")

    assert result.code == "PAYMENT_CONFIRMATION_INVALID"
    assert business.requests == []
    assert ticketing.poll_calls == []


def test_missing_authorization_does_not_burn_confirmation() -> None:
    store = AtomicPaymentStore()
    service, business, ticketing, _, access = make_payment_service(store=store, secrets=MissingSecrets())

    result = service.pay("paycfm_1")

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert store.consume_calls == 0
    assert store.consumed is False
    assert business.requests == []
    assert ticketing.poll_calls == []
    assert access.calls == []


def test_failed_access_refresh_does_not_burn_confirmation() -> None:
    store = AtomicPaymentStore()
    access = Access(AccessManagerError(code="SUBSCRIPTION_REQUIRED", message="Subscription required"))
    service, business, ticketing, _, _ = make_payment_service(store=store, access=access)

    result = service.pay("paycfm_1")

    assert result.code == "SUBSCRIPTION_REQUIRED"
    assert store.consume_calls == 0
    assert store.consumed is False
    assert business.requests == []
    assert ticketing.poll_calls == []


def test_store_failure_before_confirmation_consumption_never_calls_payment() -> None:
    store = AtomicPaymentStore(
        consume_error=BookingStoreError(
            code="BOOKING_STATE_INVALID",
            message="Saved booking state could not be processed",
        )
    )
    service, business, ticketing, _, _ = make_payment_service(store=store)

    result = service.pay("paycfm_1")

    assert result.code == "BOOKING_STATE_INVALID"
    assert business.requests == []
    assert ticketing.poll_calls == []


def test_post_uncertain_persistence_failure_still_queries_original_order_once() -> None:
    store = AtomicPaymentStore(
        update_error=BookingStoreError(
            code="BOOKING_STATE_INVALID",
            message="Saved booking state could not be processed",
        )
    )
    service, business, ticketing, _, _ = make_payment_service(pay_status=402, store=store)

    result = service.pay("paycfm_1")

    assert result.code == "TICKETED"
    assert business.requests == [{"orderNo": ORDER_NO, "paymentMethod": 1}]
    assert ticketing.poll_calls == [(ORDER_NO, 120.0)]


def test_post_success_persistence_failure_still_queries_original_order_once() -> None:
    store = AtomicPaymentStore(
        update_error=BookingStoreError(
            code="BOOKING_STATE_INVALID",
            message="Saved booking state could not be processed",
        )
    )
    service, business, ticketing, _, _ = make_payment_service(store=store)

    result = service.pay("paycfm_1")

    assert result.code == "TICKETED"
    assert business.requests == [{"orderNo": ORDER_NO, "paymentMethod": 1}]
    assert ticketing.poll_calls == [(ORDER_NO, 120.0)]


def test_post_known_terminal_persistence_failure_returns_stable_code_without_poll() -> None:
    store = AtomicPaymentStore(
        update_error=BookingStoreError(
            code="BOOKING_STATE_INVALID",
            message="Saved booking state could not be processed",
        )
    )
    service, business, ticketing, _, _ = make_payment_service(pay_status=403, store=store)

    result = service.pay("paycfm_1")

    assert result.code == "PAYMENT_METHOD_UNAVAILABLE"
    assert business.requests == [{"orderNo": ORDER_NO, "paymentMethod": 1}]
    assert ticketing.poll_calls == []


def test_concurrent_calls_submit_at_most_one_payment() -> None:
    service, business, ticketing, _, _ = make_payment_service()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(service.pay, ("paycfm_1", "paycfm_1")))

    assert sorted(result.code for result in results) == ["PAYMENT_CONFIRMATION_INVALID", "TICKETED"]
    assert len(business.requests) == 1
    assert ticketing.poll_calls == [(ORDER_NO, 120.0)]


def test_independent_processes_consume_one_persisted_confirmation_at_most_once(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    confirmation = store.issue_payment_confirmation("book_1", confirmation_seed())
    recorder_path = tmp_path / "pay-calls.log"
    process_context = multiprocessing.get_context("spawn")
    start_event = process_context.Event()
    result_queue = process_context.Queue()
    processes = [
        process_context.Process(
            target=_run_payment_process,
            args=(
                str(tmp_path),
                confirmation.confirmation_id,
                str(recorder_path),
                start_event,
                result_queue,
            ),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0]
    first_codes = sorted(result_queue.get(timeout=5) for _ in processes)
    assert first_codes == ["PAYMENT_CONFIRMATION_INVALID", "TICKETED"]
    assert recorder_path.read_text(encoding="utf-8").splitlines() == [ORDER_NO]

    persisted = json.loads(store.contexts_file.read_text(encoding="utf-8"))
    saved_confirmation = next(
        item for item in persisted["confirmations"] if item["confirmation_id"] == confirmation.confirmation_id
    )
    assert saved_confirmation["consumed_at"] is not None

    later_start = process_context.Event()
    later_result_queue = process_context.Queue()
    later_process = process_context.Process(
        target=_run_payment_process,
        args=(
            str(tmp_path),
            confirmation.confirmation_id,
            str(recorder_path),
            later_start,
            later_result_queue,
        ),
    )
    later_process.start()
    later_start.set()
    later_process.join(timeout=15)

    assert later_process.exitcode == 0
    assert later_result_queue.get(timeout=5) == "PAYMENT_CONFIRMATION_INVALID"
    assert recorder_path.read_text(encoding="utf-8").splitlines() == [ORDER_NO]


@pytest.mark.parametrize(
    ("pay_status", "expected_code"),
    [
        (401, "PAYMENT_DEADLINE_EXPIRED"),
        (403, "PAYMENT_METHOD_UNAVAILABLE"),
        (412, "PAYMENT_METHOD_UNAVAILABLE"),
        (413, "UNSUPPORTED_BOOKING_FLOW"),
    ],
)
def test_known_pre_side_effect_failure_is_returned_without_payment_retry_or_poll(
    pay_status: int,
    expected_code: str,
) -> None:
    service, business, ticketing, store, _ = make_payment_service(pay_status=pay_status)

    result = service.pay("paycfm_1")

    assert result.code == expected_code
    assert result.data == {
        "order_no": ORDER_NO,
        "order_url": f"https://www.atriptech.com/#/order/detail/{ORDER_NO}/en",
    }
    assert len(business.requests) == 1
    assert ticketing.poll_calls == []
    assert store.consumed is True


def test_payment_result_omits_unavailable_public_link() -> None:
    service, _, _, _, _ = make_payment_service(
        pay_status=403,
        order_url=lambda _order_no: None,
    )

    result = service.pay("paycfm_1")

    assert result.code == "PAYMENT_METHOD_UNAVAILABLE"
    assert result.data == {"order_no": ORDER_NO}


def test_payment_result_never_exposes_credentials_routing_session_product_or_upstream_message() -> None:
    service, _, _, _, _ = make_payment_service(pay_status=403)

    serialized = service.pay("paycfm_1").model_dump_json()

    for private in (
        "private-ak",
        "private-sk",
        "business.invalid",
        "session-safe",
        "product-safe",
        "PRIVATE UPSTREAM MESSAGE",
        "paycfm_1",
    ):
        assert private not in serialized
    assert ORDER_NO in serialized
    assert "atriptech.com" in serialized
