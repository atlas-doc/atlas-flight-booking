from urllib.parse import parse_qs

from atlas_cli.api_models import AuthTokenCreated
from atlas_cli.auth import AuthService
from atlas_cli.config import InternalSettings
from atlas_cli.models import CommandStatus
from atlas_cli.secure_store import Credentials, PendingAuth


class LoginApi:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, str]] = []

    def create_auth_token(self, *, cli_version: str, device_name: str) -> AuthTokenCreated:
        self.create_calls.append((cli_version, device_name))
        return AuthTokenCreated(
            token="token-1",
            expires_at="2026-08-03 19:00:00",
            request_id="req-login",
        )


class LoginSecrets:
    def __init__(self) -> None:
        self.pending: PendingAuth | None = None

    def save_pending_auth(self, pending: PendingAuth) -> None:
        self.pending = pending

    def load_credentials(self) -> Credentials | None:
        return None


def test_login_creates_and_stores_pending_authorization_without_exposing_token() -> None:
    api = LoginApi()
    secrets = LoginSecrets()
    settings = InternalSettings(
        control_api_base_url="https://control.example.invalid",
        authorization_page_url="https://web.example.invalid/#/login",
    )
    service = AuthService(
        api=api,
        secrets=secrets,
        settings=settings,
        cli_version="0.1.0",
        platform_system=lambda: "Darwin",
        platform_machine=lambda: "arm64",
    )

    result = service.login()

    assert api.create_calls == [("0.1.0", "darwin-arm64")]
    assert secrets.pending == PendingAuth(token="token-1", expires_at="2026-08-03 19:00:00")
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.retryable is False
    assert set(result.data) == {"authorization_url", "expires_at"}
    assert result.data["expires_at"] == "2026-08-03 19:00:00"

    authorization_url = result.data["authorization_url"]
    assert isinstance(authorization_url, str)
    query = parse_qs(authorization_url.partition("?")[2])
    assert query == {
        "utm": ["skill"],
        "cliAuthToken": ["token-1"],
        "redirect": ["/skill-entry"],
    }
    assert "control.example.invalid" not in result.model_dump_json()
    assert "token" not in result.data

