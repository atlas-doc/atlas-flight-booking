from datetime import UTC, datetime, timedelta

import pytest

from atlas_cli.access import AccessManagerError, AccessSnapshot
from atlas_cli.api_client import ApiClientError
from atlas_cli.api_models import AccessInfo, AuthTokenStatus, ExchangedCredentials
from atlas_cli.auth import AuthService
from atlas_cli.config import InternalSettings
from atlas_cli.secure_store import Credentials, PendingAuth, SecureStoreError


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.current = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)


class PollApi:
    def __init__(
        self,
        statuses: list[AuthTokenStatus | ApiClientError],
        *,
        clock: FakeClock | None = None,
        status_advance: float = 0.0,
        exchange_advance: float = 0.0,
        access_outcomes: list[AccessInfo | ApiClientError] | None = None,
    ) -> None:
        self.statuses = statuses
        self.clock = clock
        self.status_advance = status_advance
        self.exchange_advance = exchange_advance
        self.access_outcomes = access_outcomes or []
        self.status_calls: list[str] = []
        self.status_timeouts: list[float | None] = []
        self.exchange_calls: list[str] = []
        self.exchange_timeouts: list[float | None] = []
        self.access_calls: list[str] = []
        self.access_timeouts: list[float | None] = []

    def get_auth_token_status(self, token: str, *, timeout_seconds: float | None = None) -> AuthTokenStatus:
        self.status_calls.append(token)
        self.status_timeouts.append(timeout_seconds)
        if self.clock is not None:
            self.clock.advance(self.status_advance)
        outcome = self.statuses.pop(0)
        if isinstance(outcome, ApiClientError):
            raise outcome
        return outcome

    def exchange_auth_token(self, token: str, *, timeout_seconds: float | None = None) -> ExchangedCredentials:
        self.exchange_calls.append(token)
        self.exchange_timeouts.append(timeout_seconds)
        if self.clock is not None:
            self.clock.advance(self.exchange_advance)
        return ExchangedCredentials(
            jwt="jwt-value",
            client_code="CLIENT",
            cid="CUSTOMER",
            request_id="req-exchange",
        )

    def check_access_info(self, jwt: str, *, timeout_seconds: float | None = None) -> AccessInfo:
        self.access_calls.append(jwt)
        self.access_timeouts.append(timeout_seconds)
        if self.access_outcomes:
            outcome = self.access_outcomes.pop(0)
            if isinstance(outcome, ApiClientError):
                raise outcome
            return outcome
        return AccessInfo(
            activation_status=3,
            top_up_completed=True,
            access_info_exists=True,
            request_id="req-access",
        )


class PollSecrets:
    def __init__(self, pending: PendingAuth | None) -> None:
        self.pending = pending
        self.credentials: Credentials | None = None
        self.events: list[str] = []
        self.load_error = False
        self.save_error = False

    def load_pending_auth(self) -> PendingAuth | None:
        if self.load_error:
            raise SecureStoreError("unavailable")
        return self.pending

    def load_credentials(self) -> Credentials | None:
        return self.credentials

    def save_credentials(self, credentials: Credentials) -> None:
        if self.save_error:
            raise SecureStoreError("unavailable")
        self.events.append("credentials_saved")
        self.credentials = credentials

    def clear_pending_auth(self) -> None:
        self.events.append("pending_cleared")
        self.pending = None


class FakeSynchronizer:
    def __init__(
        self,
        events: list[str],
        outcome: AccessSnapshot | ApiClientError | AccessManagerError,
    ) -> None:
        self.events = events
        self.outcome = outcome
        self.calls: list[str] = []

    def synchronize(self, jwt: str) -> AccessSnapshot:
        self.calls.append(jwt)
        if isinstance(self.outcome, (ApiClientError, AccessManagerError)):
            raise self.outcome
        self.events.append("api_credentials_synchronized")
        return self.outcome


def pending_auth(expires_at: str = "2026-08-03 19:00:00") -> PendingAuth:
    return PendingAuth(token="token-1", expires_at=expires_at)


def pending_status(*, retry_after: float | None = None) -> AuthTokenStatus:
    return AuthTokenStatus(status="PENDING", message="Waiting", retry_after_seconds=retry_after)


def completed_status() -> AuthTokenStatus:
    return AuthTokenStatus(status="COMPLETED", message="Completed", request_id="req-status")


def make_service(
    api: PollApi,
    secrets: PollSecrets,
    clock: FakeClock,
    synchronizer: FakeSynchronizer | None = None,
) -> AuthService:
    return AuthService(
        api=api,
        secrets=secrets,
        settings=InternalSettings(),
        cli_version="0.1.0",
        clock=clock,
        credential_synchronizer=synchronizer,
    )


def test_missing_pending_record_returns_session_missing_without_api_call() -> None:
    api = PollApi([])
    secrets = PollSecrets(None)

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert result.code == "AUTH_SESSION_MISSING"
    assert api.status_calls == []


def test_expired_pending_record_is_cleared_without_api_call() -> None:
    api = PollApi([])
    secrets = PollSecrets(pending_auth("2026-08-03 17:59:59"))

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert result.code == "AUTH_EXPIRED"
    assert secrets.pending is None
    assert api.status_calls == []


def test_pending_uses_default_two_second_interval() -> None:
    api = PollApi([pending_status()])
    secrets = PollSecrets(pending_auth())
    clock = FakeClock()

    result = make_service(api, secrets, clock).poll(timeout_seconds=2)

    assert result.code == "AUTH_PENDING"
    assert clock.sleeps == [2.0]
    assert api.status_timeouts == [pytest.approx(2.0)]


def test_retry_after_overrides_default_interval() -> None:
    api = PollApi([pending_status(retry_after=5.0)])
    secrets = PollSecrets(pending_auth())
    clock = FakeClock()

    result = make_service(api, secrets, clock).poll(timeout_seconds=5)

    assert result.code == "AUTH_PENDING"
    assert clock.sleeps == [5.0]


def test_deadline_retains_pending_record_and_never_exchanges() -> None:
    api = PollApi([pending_status()])
    secrets = PollSecrets(pending_auth())

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=1)

    assert result.code == "AUTH_PENDING"
    assert secrets.pending is not None
    assert api.exchange_calls == []


def test_completed_status_exchanges_exactly_once() -> None:
    api = PollApi([completed_status()])
    secrets = PollSecrets(pending_auth())

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert result.code == "AUTHORIZED"
    assert api.exchange_calls == ["token-1"]


def test_status_call_that_consumes_deadline_does_not_start_exchange() -> None:
    clock = FakeClock()
    api = PollApi([completed_status()], clock=clock, status_advance=1.0)
    secrets = PollSecrets(pending_auth())

    result = make_service(api, secrets, clock).poll(timeout_seconds=1)

    assert result.code == "AUTH_PENDING"
    assert api.status_timeouts == [pytest.approx(1.0)]
    assert api.exchange_calls == []
    assert secrets.pending is not None


def test_exchange_and_access_receive_decreasing_remaining_budget() -> None:
    clock = FakeClock()
    api = PollApi(
        [completed_status()],
        clock=clock,
        status_advance=0.25,
        exchange_advance=0.25,
    )
    secrets = PollSecrets(pending_auth())

    result = make_service(api, secrets, clock).poll(timeout_seconds=1)

    assert result.code == "AUTHORIZED"
    assert api.status_timeouts == [pytest.approx(1.0)]
    assert api.exchange_timeouts == [pytest.approx(0.75)]
    assert api.access_timeouts == [pytest.approx(0.5)]


def test_credentials_are_saved_before_pending_record_is_cleared() -> None:
    api = PollApi([completed_status()])
    secrets = PollSecrets(pending_auth())

    make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert secrets.events == ["credentials_saved", "pending_cleared"]
    assert secrets.credentials == Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")


def test_completed_exchange_checks_access_and_returns_capabilities() -> None:
    api = PollApi([completed_status()])
    secrets = PollSecrets(pending_auth())

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert api.access_calls == ["jwt-value"]
    assert result.request_id == "req-access"
    assert result.data == {
        "authenticated": True,
        "search_available": True,
        "ticketing_available": True,
    }


def test_retryable_api_error_retains_pending_record() -> None:
    error = ApiClientError(
        code="SERVICE_TEMPORARILY_UNAVAILABLE",
        message="Service temporarily unavailable",
        retryable=True,
    )
    api = PollApi([error])
    secrets = PollSecrets(pending_auth())

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert result.code == "AUTH_SERVICE_UNAVAILABLE"
    assert result.retryable is True
    assert secrets.pending is not None


def test_poll_retry_after_access_failure_resumes_from_saved_credentials() -> None:
    temporary_error = ApiClientError(
        code="SERVICE_TEMPORARILY_UNAVAILABLE",
        message="Service temporarily unavailable",
        retryable=True,
    )
    recovered_access = AccessInfo(
        activation_status=3,
        top_up_completed=True,
        access_info_exists=True,
        request_id="req-recovered",
    )
    api = PollApi(
        [completed_status()],
        access_outcomes=[temporary_error, recovered_access],
    )
    secrets = PollSecrets(pending_auth())
    service = make_service(api, secrets, FakeClock())

    first = service.poll(timeout_seconds=120)
    second = service.poll(timeout_seconds=120)

    assert first.code == "AUTH_SERVICE_UNAVAILABLE"
    assert second.code == "AUTHORIZED"
    assert api.status_calls == ["token-1"]
    assert api.exchange_calls == ["token-1"]
    assert api.access_calls == ["jwt-value", "jwt-value"]


def test_secure_store_error_does_not_claim_success() -> None:
    api = PollApi([])
    secrets = PollSecrets(pending_auth())
    secrets.load_error = True

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert result.code == "SECURE_STORE_UNAVAILABLE"
    assert result.status.value == "terminal_error"
    assert api.status_calls == []


def test_failed_credential_save_retains_pending_record() -> None:
    api = PollApi([completed_status()])
    secrets = PollSecrets(pending_auth())
    secrets.save_error = True

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert result.code == "SECURE_STORE_UNAVAILABLE"
    assert secrets.pending is not None
    assert "pending_cleared" not in secrets.events


def test_completed_authorization_synchronizes_api_credentials_before_clearing_pending() -> None:
    api = PollApi([completed_status()])
    secrets = PollSecrets(pending_auth())
    synchronizer = FakeSynchronizer(
        secrets.events,
        AccessSnapshot(
            activation_status=3,
            top_up_completed=True,
            search_available=True,
            ticketing_available=True,
            request_id="req-sync",
        ),
    )

    result = make_service(api, secrets, FakeClock(), synchronizer).poll(timeout_seconds=120)

    assert secrets.events == ["credentials_saved", "api_credentials_synchronized", "pending_cleared"]
    assert synchronizer.calls == ["jwt-value"]
    assert result.request_id == "req-sync"
    assert result.data == {
        "authenticated": True,
        "search_available": True,
        "ticketing_available": True,
    }
    assert "jwt" not in result.model_dump_json().lower()


def test_synchronization_failure_keeps_pending_and_saved_jwt_for_safe_retry() -> None:
    api = PollApi([completed_status()])
    secrets = PollSecrets(pending_auth())
    synchronizer = FakeSynchronizer(
        secrets.events,
        ApiClientError(
            code="SERVICE_TEMPORARILY_UNAVAILABLE",
            message="Service temporarily unavailable",
            retryable=True,
        ),
    )

    result = make_service(api, secrets, FakeClock(), synchronizer).poll(timeout_seconds=120)

    assert result.code == "AUTH_SERVICE_UNAVAILABLE"
    assert secrets.credentials is not None
    assert secrets.pending is not None
    assert secrets.events == ["credentials_saved"]


def test_invalid_credential_synchronization_response_is_sanitized_and_retryable_from_pending() -> None:
    api = PollApi([completed_status()])
    secrets = PollSecrets(pending_auth())
    synchronizer = FakeSynchronizer(
        secrets.events,
        AccessManagerError(
            code="SERVICE_RESPONSE_INVALID",
            message="Service response could not be processed",
        ),
    )

    result = make_service(api, secrets, FakeClock(), synchronizer).poll(timeout_seconds=120)

    assert result.code == "SERVICE_RESPONSE_INVALID"
    assert result.status.value == "terminal_error"
    assert secrets.pending is not None
    assert secrets.credentials is not None


def test_service_code_auth_expired_clears_pending_authorization_record() -> None:
    expired = ApiClientError(
        code="AUTH_EXPIRED",
        message="Authorization expired",
        retryable=False,
    )
    api = PollApi([expired])
    secrets = PollSecrets(pending_auth())

    result = make_service(api, secrets, FakeClock()).poll(timeout_seconds=120)

    assert result.code == "AUTH_EXPIRED"
    assert secrets.pending is None
    assert secrets.events == ["pending_cleared"]
