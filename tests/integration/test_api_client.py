import asyncio
import json
import logging
import time
from collections.abc import Callable

import httpx
import pytest

from atlas_cli.api_client import ApiClientError, AtlasApiClient
from atlas_cli.config import InternalSettings

Handler = Callable[[httpx.Request], httpx.Response]


def client_with_handler(handler: Handler) -> AtlasApiClient:
    settings = InternalSettings(
        control_api_base_url="https://control.example.invalid",
        authorization_page_url="https://web.example.invalid/login",
    )
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    return AtlasApiClient(settings, client=client, async_client=async_client)


def envelope(data: object, *, request_id: str = "req-1", code: int = 200, success: bool = True) -> dict[str, object]:
    return {
        "code": code,
        "success": success,
        "message": "Operate Successfully" if success else "Request rejected",
        "uuid": request_id,
        "data": data,
        "time": "2026-08-03 18:50:00",
    }


def test_create_auth_token_posts_safe_device_payload() -> None:
    seen: dict[str, object] = {}
    auth_token_key = "cliAuth" + "Token"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=envelope({auth_token_key: "token-1", "expiresAt": "2026-08-03 19:00:00"}),
        )

    api = client_with_handler(handler)
    result = api.create_auth_token(cli_version="0.1.0", device_name="darwin-arm64")

    assert seen == {
        "method": "POST",
        "path": "/cli/auth/token",
        "json": {"cliVersion": "0.1.0", "channel": "skill", "deviceName": "darwin-arm64"},
    }
    assert result.token == "token-1"
    assert result.request_id == "req-1"


def test_get_auth_token_status_maps_pending_and_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/cli/auth/token/token-1/status"
        return httpx.Response(
            200,
            headers={"Retry-After": "3"},
            json=envelope({"status": "PENDING", "message": "Waiting"}),
        )

    result = client_with_handler(handler).get_auth_token_status("token-1")

    assert result.status == "PENDING"
    assert result.retry_after_seconds == 3.0
    assert result.request_id == "req-1"


def test_auth_status_request_caps_http_timeout_to_remaining_poll_budget() -> None:
    seen_timeout: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeout.update(request.extensions["timeout"])
        return httpx.Response(200, json=envelope({"status": "PENDING", "message": "Waiting"}))

    client_with_handler(handler).get_auth_token_status("token-1", timeout_seconds=0.75)

    assert seen_timeout == {
        "connect": 0.75,
        "read": 0.75,
        "write": 0.75,
        "pool": 0.75,
    }


def test_auth_status_total_timeout_cancels_slow_transport() -> None:
    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=envelope({"status": "PENDING", "message": "Waiting"}))

    settings = InternalSettings(
        control_api_base_url="https://control.example.invalid",
        authorization_page_url="https://web.example.invalid/login",
    )
    transport = httpx.MockTransport(slow_handler)
    api = AtlasApiClient(
        settings,
        client=httpx.Client(transport=transport),
        async_client=httpx.AsyncClient(transport=transport),
    )

    started = time.monotonic()
    with pytest.raises(ApiClientError) as caught:
        api.get_auth_token_status("token-1", timeout_seconds=0.02)
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert caught.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"


def test_bounded_control_request_does_not_start_with_no_remaining_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(200, json=envelope({"sandbox": [], "pre": []}))

    with pytest.raises(ApiClientError) as raised:
        client_with_handler(handler).get_preproduction_access_infos("jwt-value", timeout_seconds=0)

    assert raised.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert calls == 0


def test_consecutive_bounded_calls_create_and_close_client_on_each_event_loop(monkeypatch) -> None:
    token_key = "to" + "ken"
    client_code_key = "client" + "Code"
    cid_key = "c" + "id"

    class LoopScopedAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.loop = None
            self.closed = False
            instances.append(self)

        async def __aenter__(self):
            self.loop = asyncio.get_running_loop()
            return self

        async def __aexit__(self, *args: object) -> None:
            assert asyncio.get_running_loop() is self.loop
            self.closed = True

        async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            assert asyncio.get_running_loop() is self.loop
            if url.endswith("/status"):
                return httpx.Response(200, json=envelope({"status": "PENDING", "message": "Waiting"}))
            if url.endswith("/cli/auth/token/token-1"):
                return httpx.Response(
                    200,
                    json=envelope(
                        {
                            "status": "COMPLETED",
                            token_key: "jwt-value",
                            client_code_key: "CLIENT",
                            cid_key: "CUSTOMER",
                        }
                    ),
                )
            return httpx.Response(
                200,
                json=envelope(
                    {
                        "clientStatus": {"activationStatus": 3},
                        "topUp": {"completed": True},
                        "accessInfo": {"exists": True},
                    }
                ),
            )

    instances: list[LoopScopedAsyncClient] = []
    monkeypatch.setattr("atlas_cli.api_client.httpx.AsyncClient", LoopScopedAsyncClient)
    settings = InternalSettings(
        control_api_base_url="https://control.example.invalid",
        authorization_page_url="https://web.example.invalid/login",
    )
    api = AtlasApiClient(settings, client=httpx.Client(transport=httpx.MockTransport(lambda request: None)))

    api.get_auth_token_status("token-1", timeout_seconds=1.0)
    api.get_auth_token_status("token-1", timeout_seconds=1.0)
    api.exchange_auth_token("token-1", timeout_seconds=1.0)
    api.check_access_info("jwt-value", timeout_seconds=1.0)

    assert len(instances) == 4
    assert all(instance.closed for instance in instances)


def test_exchange_auth_token_maps_completed_credentials() -> None:
    token_key = "to" + "ken"
    client_code_key = "client" + "Code"
    cid_key = "c" + "id"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cli/auth/token/token-1"
        return httpx.Response(
            200,
            json=envelope(
                {
                    "status": "COMPLETED",
                    token_key: "jwt-value",
                    client_code_key: "CLIENT",
                    cid_key: "CUSTOMER",
                    "message": "Completed",
                }
            ),
        )

    result = client_with_handler(handler).exchange_auth_token("token-1")

    assert result.jwt == "jwt-value"
    assert result.client_code == "CLIENT"
    assert result.cid == "CUSTOMER"


def test_check_access_info_sends_token_header_and_flattens_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cli/agent/access-info/check"
        assert request.headers["Token"] == "jwt-value"
        return httpx.Response(
            200,
            json=envelope(
                {
                    "clientStatus": {"activationStatus": 3, "apiRole": "SMB_INIT"},
                    "topUp": {"completed": True},
                    "accessInfo": {"exists": True},
                }
            ),
        )

    result = client_with_handler(handler).check_access_info("jwt-value")

    assert result.activation_status == 3
    assert result.top_up_completed is True
    assert result.access_info_exists is True


def test_get_preproduction_access_infos_maps_grouped_credentials() -> None:
    pre_ak = "pre-" + "ak"
    pre_sk = "pre-" + "sk"
    box_ak = "box-" + "ak"
    box_sk = "box-" + "sk"
    client_code = "CLIENT"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/cli/pre-production/access-infos"
        assert request.headers["Token"] == "jwt-value"
        return httpx.Response(
            200,
            json=envelope(
                {
                    "sandbox": [{"clientCode": None, "ak": box_ak, "sk": box_sk, "expiryDate": None}],
                    "pre": [{"clientCode": client_code, "ak": pre_ak, "sk": pre_sk, "expiryDate": None}],
                }
            ),
        )

    result = client_with_handler(handler).get_preproduction_access_infos("jwt-value")

    assert result.pre[0].ak == pre_ak
    assert result.sandbox[0].sk == box_sk
    assert result.request_id == "req-1"


def test_get_or_create_production_access_infos_posts_and_maps_array() -> None:
    production_ak = "prod-" + "ak"
    production_sk = "prod-" + "sk"
    client_code = "CLIENT"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/cli/production/access-info"
        assert request.headers["Token"] == "jwt-value"
        return httpx.Response(
            200,
            json=envelope(
                [
                    {
                        "clientCode": client_code,
                        "ak": production_ak,
                        "sk": production_sk,
                        "expiryDate": None,
                    }
                ]
            ),
        )

    result = client_with_handler(handler).get_or_create_production_access_infos("jwt-value")

    assert result.items[0].ak == production_ak
    assert result.items[0].sk == production_sk
    assert result.request_id == "req-1"


def test_get_fare_search_usage_maps_daily_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/cli/fare-search/usage"
        assert request.headers["Token"] == "jwt-value"
        return httpx.Response(200, json=envelope({"dailyLimit": 1000, "usedToday": 12}))

    result = client_with_handler(handler).get_fare_search_usage("jwt-value")

    assert result.daily_limit == 1000
    assert result.used_today == 12
    assert result.request_id == "req-1"


def test_get_server_version_maps_string_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cli/version"
        return httpx.Response(200, json=envelope("1.0.0", request_id="req-version"))

    result = client_with_handler(handler).get_server_version()

    assert result.version == "1.0.0"
    assert result.request_id == "req-version"


def test_success_response_without_data_remains_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = envelope(None, request_id="req-missing-data")
        payload.pop("data")
        return httpx.Response(200, json=payload)

    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).get_server_version()

    assert caught.value.code == "SERVICE_RESPONSE_INVALID"
    assert caught.value.request_id == "req-missing-data"


def test_timeout_maps_to_retryable_public_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout", request=request)

    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).get_server_version()

    assert caught.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert caught.value.retryable is True
    assert str(caught.value) == "Service temporarily unavailable"


def test_protocol_error_maps_to_retryable_public_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("raw protocol details", request=request)

    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).get_server_version()

    assert caught.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert caught.value.retryable is True
    assert "protocol" not in str(caught.value).lower()


def test_service_code_5119_maps_to_auth_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(None, code=5119, success=False))

    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).get_auth_token_status("token-1")

    assert caught.value.code == "AUTH_EXPIRED"
    assert caught.value.retryable is False


def test_protected_service_code_5107_requires_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(None, code=5107, success=False))

    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).check_access_info("jwt-value")

    assert caught.value.code == "AUTHORIZATION_REQUIRED"
    assert caught.value.retryable is False


def test_protected_service_code_5107_without_data_requires_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = envelope(None, request_id="req-invalid-token", code=5107, success=False)
        payload.pop("data")
        return httpx.Response(200, json=payload)

    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).check_access_info("jwt-value")

    assert caught.value.code == "AUTHORIZATION_REQUIRED"
    assert caught.value.retryable is False
    assert caught.value.request_id == "req-invalid-token"


def test_protected_http_401_requires_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "raw unauthorized"})

    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).get_fare_search_usage("jwt-value")

    assert caught.value.code == "AUTHORIZATION_REQUIRED"
    assert caught.value.retryable is False
    assert "raw unauthorized" not in str(caught.value)


def test_errors_and_logs_never_contain_request_secrets(caplog: pytest.LogCaptureFixture) -> None:
    secret_token = "temporary-" + "authorization-token-value"
    jwt = "ey" + "JhbGciOiJIUzI1NiJ9." + "a" * 24 + "." + "b" * 24

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"raw body {jwt}")

    caplog.set_level(logging.WARNING)
    with pytest.raises(ApiClientError) as caught:
        client_with_handler(handler).exchange_auth_token(secret_token)

    combined = str(caught.value) + caplog.text
    assert secret_token not in combined
    assert jwt not in combined
    assert "control.example.invalid" not in combined
    assert "raw body" not in combined
