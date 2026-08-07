"""Single-use Atlas balance payment with query-only recovery."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from atlas_cli.access import AccessManager, AccessManagerError, TransactionAccess
from atlas_cli.api_client import ApiClientError
from atlas_cli.booking_models import OrderState, PaymentState
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.business_client import AtlasBusinessClient, BusinessApiError, BusinessResponse
from atlas_cli.business_status import BookingApiError, BusinessStage, booking_error_result, map_business_status
from atlas_cli.endpoints import BusinessOperation, BusinessRoute
from atlas_cli.models import CommandResult, CommandStatus, action_required_result
from atlas_cli.secure_store import ApiCredential, Credentials, SecureStoreError
from atlas_cli.ticketing import TicketingService

_UNKNOWN_PAYMENT_STATUSES = {402, 404, 411}
_PROCESSING_PAYMENT_STATUSES = {406, 615}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PaymentSubmission:
    order_no: str


def required_public_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BookingApiError.invalid_response()
    return value


class BusinessClient(Protocol):
    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse: ...


class PaymentAdapter:
    def __init__(self, business: BusinessClient | AtlasBusinessClient) -> None:
        self._business = business

    def pay(self, route: BusinessRoute, credential: ApiCredential, order_no: str) -> PaymentSubmission:
        response = self._business.post(
            route,
            credential,
            {"orderNo": order_no, "paymentMethod": 1},
        )
        meaning = map_business_status(BusinessStage.PAY, response.status)
        if meaning is not None:
            raise BookingApiError.from_meaning(
                meaning,
                request_id=response.request_id,
                upstream_status=response.status,
            )
        return PaymentSubmission(order_no=required_public_string(response.data, "orderNo"))


class ControlCredentialStore(Protocol):
    def load_credentials(self) -> Credentials | None: ...


class TransactionAccessResolver(Protocol):
    def resolve_transaction_access(self, jwt: str, operation: BusinessOperation) -> TransactionAccess: ...


class PaymentGateway(Protocol):
    def pay(self, route: BusinessRoute, credential: ApiCredential, order_no: str) -> PaymentSubmission: ...


class PaymentStore(Protocol):
    def consume_payment_confirmation(self, confirmation_id: str, *, now: datetime) -> OrderState: ...

    def update_payment(self, order_no: str, state: PaymentState) -> OrderState: ...


class TicketingRecovery(Protocol):
    def poll(self, order_no: str, *, timeout_seconds: float) -> CommandResult: ...


class PaymentService:
    def __init__(
        self,
        *,
        secrets: ControlCredentialStore,
        access: TransactionAccessResolver | AccessManager,
        adapter: PaymentGateway | PaymentAdapter,
        booking_store: PaymentStore | BookingStore,
        ticketing: TicketingRecovery | TicketingService,
        order_url: Callable[[str], str | None] | None = None,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self._secrets = secrets
        self._access = access
        self._adapter = adapter
        self._booking_store = booking_store
        self._ticketing = ticketing
        self._order_url = order_url
        self._now = now

    def pay(self, confirmation_id: str) -> CommandResult:
        access, error_result = self._resolve_access()
        if error_result is not None:
            return error_result
        assert access is not None

        try:
            order = self._booking_store.consume_payment_confirmation(confirmation_id, now=self._now())
        except (BookingStoreError, OSError) as error:
            return booking_error_result(error)

        locator = self._order_locator(order)
        try:
            submission = self._adapter.pay(access.route, access.credential, order.order_no)
        except BookingApiError as error:
            if self._requires_query_recovery(error):
                state = (
                    PaymentState.PROCESSING
                    if error.upstream_status in _PROCESSING_PAYMENT_STATUSES
                    else PaymentState.UNKNOWN
                )
                return self._recover(order.order_no, state, locator)
            self._update_payment_best_effort(order.order_no, PaymentState.UNKNOWN)
            return booking_error_result(error, data=locator)
        except BusinessApiError:
            return self._recover(order.order_no, PaymentState.UNKNOWN, locator)

        if submission.order_no != order.order_no:
            return self._recover(order.order_no, PaymentState.UNKNOWN, locator)
        self._update_payment_best_effort(order.order_no, PaymentState.SUBMITTED)
        return self._poll_after_payment(order.order_no, locator)

    def _resolve_access(self) -> tuple[TransactionAccess | None, CommandResult | None]:
        try:
            credentials = self._secrets.load_credentials()
            if credentials is None or not credentials.jwt.strip():
                raise AccessManagerError(code="AUTHORIZATION_REQUIRED", message="Authorization required")
            access = self._access.resolve_transaction_access(credentials.jwt, BusinessOperation.PAY)
        except (AccessManagerError, ApiClientError, SecureStoreError) as error:
            if isinstance(error, SecureStoreError):
                error = AccessManagerError(
                    code="SECURE_STORE_UNAVAILABLE",
                    message="Secure credential storage is unavailable",
                )
            return None, booking_error_result(error)
        return access, None

    @staticmethod
    def _requires_query_recovery(error: BookingApiError) -> bool:
        return (
            error.upstream_status in _UNKNOWN_PAYMENT_STATUSES | _PROCESSING_PAYMENT_STATUSES
            or error.code in {"PAYMENT_STATUS_UNKNOWN", "SERVICE_RESPONSE_INVALID"}
            or error.side_effect_uncertain
        )

    def _recover(
        self,
        order_no: str,
        state: PaymentState,
        locator: dict[str, object],
    ) -> CommandResult:
        self._update_payment_best_effort(order_no, state)
        return self._poll_after_payment(order_no, locator)

    def _poll_after_payment(
        self,
        order_no: str,
        locator: dict[str, object],
    ) -> CommandResult:
        result = self._ticketing.poll(order_no, timeout_seconds=120.0)
        if result.status is not CommandStatus.RETRYABLE_ERROR and not result.retryable:
            return result
        return action_required_result(
            "PAYMENT_STATUS_UNKNOWN",
            "Payment status could not be confirmed",
            request_id=result.request_id,
            data={**result.data, **locator},
        )

    def _update_payment_best_effort(self, order_no: str, state: PaymentState) -> None:
        with suppress(Exception):
            self._booking_store.update_payment(order_no, state)

    def _order_locator(self, order: OrderState) -> dict[str, object]:
        locator: dict[str, object] = {"order_no": order.order_no}
        order_url = self._order_url(order.order_no) if self._order_url is not None else order.order_url
        if order_url is not None:
            locator["order_url"] = order_url
        return locator
