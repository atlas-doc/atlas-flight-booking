from atlas_cli.api_client import ApiClientError
from atlas_cli.api_models import RefreshedSession
from atlas_cli.auth import AuthService
from atlas_cli.config import InternalSettings
from atlas_cli.secure_store import Credentials, PendingAuth, SecureStoreError


class RefreshApi:
    def __init__(self, result: RefreshedSession | ApiClientError) -> None:
        self.result = result
        self.calls: list[str] = []

    def refresh_session(self, jwt: str) -> RefreshedSession:
        self.calls.append(jwt)
        if isinstance(self.result, ApiClientError):
            raise self.result
        return self.result


class RefreshStore:
    def __init__(self, credentials: Credentials | None, *, save_fails: bool = False) -> None:
        self.credentials = credentials
        self.save_fails = save_fails
        self.cleared = False

    def load_credentials(self) -> Credentials | None:
        return self.credentials

    def save_credentials(self, credentials: Credentials) -> None:
        if self.save_fails:
            raise SecureStoreError("raw keychain failure")
        self.credentials = credentials

    def clear_credentials(self) -> None:
        self.credentials = None
        self.cleared = True

    def save_pending_auth(self, pending: PendingAuth) -> None:
        del pending
        raise AssertionError("session refresh must not create pending authorization")


def service(
    api_result: RefreshedSession | ApiClientError,
    store: RefreshStore,
) -> tuple[AuthService, RefreshApi]:
    api = RefreshApi(api_result)
    return (
        AuthService(
            api=api,  # type: ignore[arg-type]
            secrets=store,  # type: ignore[arg-type]
            settings=InternalSettings(),
            cli_version="0.3.7",
        ),
        api,
    )


def test_manual_session_refresh_saves_new_token_and_returns_only_safe_metadata() -> None:
    original = Credentials(jwt="old-jwt-value", client_code="CLIENT", cid="CUSTOMER")
    store = RefreshStore(original)
    auth, api = service(
        RefreshedSession(
            token="new-jwt-value",
            expireSecond=36000,
            request_id="req-refresh",
        ),
        store,
    )

    result = auth.refresh_session()

    assert result.code == "SESSION_REFRESHED"
    assert result.request_id == "req-refresh"
    assert result.data == {"expire_seconds": 36000}
    assert "old-jwt-value" not in result.model_dump_json()
    assert "new-jwt-value" not in result.model_dump_json()
    assert store.credentials == Credentials(
        jwt="new-jwt-value",
        client_code="CLIENT",
        cid="CUSTOMER",
    )
    assert api.calls == ["old-jwt-value"]


def test_manual_session_refresh_5120_clears_token_and_requires_authorization() -> None:
    store = RefreshStore(Credentials(jwt="wrong-jwt", client_code="CLIENT", cid="CUSTOMER"))
    auth, _ = service(
        ApiClientError(
            code="AUTHORIZATION_REQUIRED",
            message="Authorization required",
            retryable=False,
            request_id="req-reauthorize",
        ),
        store,
    )

    result = auth.refresh_session()

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.request_id == "req-reauthorize"
    assert store.credentials is None
    assert store.cleared is True


def test_manual_session_refresh_temporary_failure_preserves_token_for_retry() -> None:
    original = Credentials(jwt="current-jwt", client_code="CLIENT", cid="CUSTOMER")
    store = RefreshStore(original)
    auth, _ = service(
        ApiClientError(
            code="SERVICE_TEMPORARILY_UNAVAILABLE",
            message="Service temporarily unavailable",
            retryable=True,
            request_id="req-retry",
        ),
        store,
    )

    result = auth.refresh_session()

    assert result.code == "AUTH_SERVICE_UNAVAILABLE"
    assert result.retryable is True
    assert result.request_id == "req-retry"
    assert store.credentials == original


def test_manual_session_refresh_without_credentials_requires_authorization() -> None:
    store = RefreshStore(None)
    auth, api = service(
        RefreshedSession(token="unused", expireSecond=36000),
        store,
    )

    result = auth.refresh_session()

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert api.calls == []


def test_manual_session_refresh_keychain_write_failure_is_stable() -> None:
    original = Credentials(jwt="old-jwt", client_code="CLIENT", cid="CUSTOMER")
    store = RefreshStore(original, save_fails=True)
    auth, _ = service(
        RefreshedSession(token="new-jwt", expireSecond=36000),
        store,
    )

    result = auth.refresh_session()

    assert result.code == "SECURE_STORE_UNAVAILABLE"
    assert "keychain" not in result.model_dump_json().lower()
