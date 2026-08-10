from atlas_cli.api_client import ApiClientError
from atlas_cli.api_models import AccessInfo
from atlas_cli.auth import AuthService
from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import CustomerMode
from atlas_cli.secure_store import Credentials, PendingAuth


class StatusApi:
    def __init__(self, access: AccessInfo | ApiClientError) -> None:
        self.access = access
        self.jwt_calls: list[str] = []

    def check_access_info(self, jwt: str) -> AccessInfo:
        self.jwt_calls.append(jwt)
        if isinstance(self.access, ApiClientError):
            raise self.access
        return self.access


class StatusSecrets:
    def __init__(self, credentials: Credentials | None) -> None:
        self.credentials = credentials

    def load_credentials(self) -> Credentials | None:
        return self.credentials

    def save_pending_auth(self, pending: PendingAuth) -> None:
        raise AssertionError("status must not create pending authorization")

    def clear_credentials(self) -> None:
        self.credentials = None


def make_service(
    credentials: Credentials | None,
    access: AccessInfo | ApiClientError,
    *,
    mode: CustomerMode = CustomerMode.PROD,
) -> tuple[AuthService, StatusApi, StatusSecrets]:
    api = StatusApi(access)
    secrets = StatusSecrets(credentials)
    service = AuthService(
        api=api,
        secrets=secrets,
        settings=InternalSettings(),
        cli_version="0.1.0",
        customer_mode=mode,
    )
    return service, api, secrets


def test_status_without_credentials_requires_authorization() -> None:
    service, api, _ = make_service(
        None,
        AccessInfo(activation_status=3, top_up_completed=True, access_info_exists=True),
    )

    result = service.status()

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.data == {"authenticated": False}
    assert api.jwt_calls == []


def test_status_with_access_maps_capabilities() -> None:
    credentials = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    service, api, _ = make_service(
        credentials,
        AccessInfo(
            activation_status=3,
            top_up_completed=False,
            access_info_exists=True,
            request_id="req-access",
        ),
    )

    result = service.status()

    assert result.code == "AUTHORIZED"
    assert result.data == {
        "authenticated": True,
        "search_available": True,
        "ticketing_available": False,
        "ticketing_activation_url": "https://www.atriptech.com/#/workspace",
        "ticketing_blocker": "TOP_UP_REQUIRED",
    }
    assert result.request_id == "req-access"
    assert api.jwt_calls == ["jwt-value"]


def test_status_distinguishes_pending_ticketing_activation_from_missing_top_up() -> None:
    credentials = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    service, _, _ = make_service(
        credentials,
        AccessInfo(activation_status=2, top_up_completed=False, access_info_exists=True),
    )

    result = service.status()

    assert result.data["ticketing_available"] is False
    assert result.data["ticketing_blocker"] == "TICKETING_ACTIVATION_REQUIRED"


def test_access_info_exists_does_not_gate_live_topped_up_ticketing() -> None:
    credentials = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    service, _, _ = make_service(
        credentials,
        AccessInfo(activation_status=3, top_up_completed=True, access_info_exists=False),
    )

    result = service.status()

    assert result.data["ticketing_available"] is True
    assert "ticketing_activation_url" not in result.data


def test_sandbox_status_reports_transaction_capability_without_exposing_mode() -> None:
    credentials = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    service, _, _ = make_service(
        credentials,
        AccessInfo(activation_status=1, top_up_completed=False, access_info_exists=False),
        mode=CustomerMode.SANDBOX,
    )

    result = service.status()

    assert result.data == {
        "authenticated": True,
        "search_available": True,
        "ticketing_available": True,
    }
    assert "ticketing_blocker" not in result.data
    assert "sandbox" not in str(result.model_dump()).lower()


def test_expired_jwt_clears_only_control_credentials_and_requires_authorization() -> None:
    credentials = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    service, api, secrets = make_service(
        credentials,
        ApiClientError(
            code="AUTHORIZATION_REQUIRED",
            message="Authorization required",
            retryable=False,
        ),
    )

    result = service.status()

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.status.value == "action_required"
    assert secrets.credentials is None
    assert api.jwt_calls == ["jwt-value"]
    assert not hasattr(secrets, "clear_api_credentials")
