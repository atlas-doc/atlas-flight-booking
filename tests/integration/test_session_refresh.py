import httpx

from atlas_cli.api_client import AtlasApiClient
from atlas_cli.auth import AuthService
from atlas_cli.config import InternalSettings
from atlas_cli.secure_store import Credentials


def envelope(
    data: object,
    *,
    request_id: str = "req-1",
    code: int = 200,
    success: bool = True,
) -> dict[str, object]:
    return {
        "code": code,
        "success": success,
        "message": "Operate Successfully" if success else "Request rejected",
        "uuid": request_id,
        "data": data,
        "time": "2026-08-07 19:10:10",
    }


class SessionStore:
    def __init__(self) -> None:
        self.credentials: Credentials | None = Credentials(
            jwt="old-jwt-value",
            client_code="CLIENT",
            cid="CUSTOMER",
        )

    def load_credentials(self) -> Credentials | None:
        return self.credentials

    def save_credentials(self, credentials: Credentials) -> None:
        self.credentials = credentials

    def clear_credentials(self) -> None:
        self.credentials = None


def service_with_handler(
    handler: httpx.MockTransport,
    store: SessionStore,
) -> AuthService:
    settings = InternalSettings(control_api_base_url="https://control.example.invalid")
    client = AtlasApiClient(
        settings,
        client=httpx.Client(transport=handler),
        async_client=httpx.AsyncClient(transport=handler),
        credential_store=store,
    )
    return AuthService(
        api=client,
        secrets=store,
        settings=settings,
        cli_version="0.3.7",
    )


def test_auth_status_silently_refreshes_and_remains_authorized() -> None:
    store = SessionStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cli/session/refresh":
            return httpx.Response(
                200,
                json=envelope({"token": "new-jwt-value", "expireSecond": 36000}),
            )
        if request.headers["Token"] == "old-jwt-value":
            return httpx.Response(200, json=envelope(None, code=5555, success=False))
        return httpx.Response(
            200,
            json=envelope(
                {
                    "clientStatus": {"activationStatus": 3},
                    "topUp": {"completed": True},
                    "accessInfo": {"exists": True},
                },
                request_id="req-authorized",
            ),
        )

    result = service_with_handler(httpx.MockTransport(handler), store).status()

    assert result.code == "AUTHORIZED"
    assert result.request_id == "req-authorized"
    assert result.data["authenticated"] is True
    assert store.credentials == Credentials(
        jwt="new-jwt-value",
        client_code="CLIENT",
        cid="CUSTOMER",
    )


def test_auth_status_refresh_5120_clears_old_token_and_requires_authorization() -> None:
    store = SessionStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cli/session/refresh":
            return httpx.Response(
                200,
                json=envelope(None, request_id="req-reauthorize", code=5120, success=False),
            )
        return httpx.Response(461, json={"message": "raw expired session"})

    result = service_with_handler(httpx.MockTransport(handler), store).status()

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.request_id == "req-reauthorize"
    assert result.data == {}
    assert store.credentials is None
