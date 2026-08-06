from __future__ import annotations

import logging

import httpx
import pytest

from atlas_cli.business_client import AtlasBusinessClient, BusinessApiError
from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import BusinessOperation, BusinessRoute, CredentialSlot, SearchProvider, SearchRoute
from atlas_cli.secure_store import ApiCredential


def route() -> SearchRoute:
    return SearchRoute(
        base_url="https://business.example.invalid/",
        path="/search.do",
        provider=SearchProvider.STANDARD,
        credential_slot=CredentialSlot.PRODUCTION,
        bookable=True,
        generation="a" * 24,
    )


def business_route() -> BusinessRoute:
    return BusinessRoute(
        base_url="https://business.example.invalid",
        path="/verify.do",
        operation=BusinessOperation.VERIFY,
        credential_slot=CredentialSlot.PRODUCTION,
        generation="g" * 24,
    )


def credential() -> ApiCredential:
    return ApiCredential(client_code="CLIENT", ak="client-" + "id", sk="client-" + "secret")


def client_with_handler(handler: httpx.MockTransport) -> AtlasBusinessClient:
    settings = InternalSettings(connect_timeout_seconds=2.5, read_timeout_seconds=12.0)
    return AtlasBusinessClient(settings, client=httpx.Client(transport=handler))


def test_post_builds_route_url_and_sends_only_business_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://business.example.invalid/verify.do"
        assert request.headers["x-atlas-client-id"] == "client-" + "id"
        assert request.headers["x-atlas-client-secret"] == "client-" + "secret"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json"
        assert "gzip" in request.headers["accept-encoding"]
        assert "token" not in request.headers
        assert "authorization" not in request.headers
        assert request.headers["host"] == "business.example.invalid"
        assert request.extensions["timeout"] == {
            "connect": 2.5,
            "read": 12.0,
            "write": 12.0,
            "pool": 2.5,
        }
        return httpx.Response(
            200,
            json={"status": 0, "msg": "ok", "requestId": "business-request", "routings": []},
        )

    result = client_with_handler(httpx.MockTransport(handler)).post(
        business_route(),
        credential(),
        {"fromCity": "KUL"},
    )

    assert result.status == 0
    assert result.msg == "ok"
    assert result.request_id == "business-request"
    assert result.data == {"routings": []}


def test_post_caps_each_timeout_component_to_request_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {
            "connect": 1.25,
            "read": 1.25,
            "write": 1.25,
            "pool": 1.25,
        }
        return httpx.Response(200, json={"status": 0})

    result = client_with_handler(httpx.MockTransport(handler)).post(
        business_route(), credential(), {}, request_timeout_seconds=1.25
    )

    assert result.status == 0


@pytest.mark.parametrize("request_timeout_seconds", [0.0, -1.0])
def test_post_rejects_non_positive_request_timeout_before_http(request_timeout_seconds: float) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("HTTP request should not be started")

    with pytest.raises(ValueError):
        client_with_handler(httpx.MockTransport(handler)).post(
            business_route(), credential(), {}, request_timeout_seconds=request_timeout_seconds
        )


def test_nonzero_business_status_is_returned_for_adapter_mapping() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"status": 109, "msg": "ignored upstream text", "uuid": "request-109"},
        )
    )

    result = client_with_handler(transport).post(route(), credential(), {})

    assert result.status == 109
    assert result.request_id == "request-109"
    assert result.data == {}


@pytest.mark.parametrize("http_status", [401])
def test_http_credential_rejection_maps_to_stable_refreshable_error(http_status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(http_status, text="private body"))

    with pytest.raises(BusinessApiError) as raised:
        client_with_handler(transport).post(route(), credential(), {})

    assert raised.value.code == "CREDENTIAL_REJECTED"
    assert raised.value.retryable is True


def test_business_status_900_maps_to_credential_rejection() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": 900, "msg": "private rejection", "uuid": "req-900"})
    )

    with pytest.raises(BusinessApiError) as raised:
        client_with_handler(transport).post(route(), credential(), {})

    assert raised.value.code == "CREDENTIAL_REJECTED"
    assert raised.value.retryable is True
    assert raised.value.request_id == "req-900"


@pytest.mark.parametrize("http_status", [429, 500, 503])
def test_transient_http_status_maps_to_retryable_service_error(http_status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(http_status, text="private body"))

    with pytest.raises(BusinessApiError) as raised:
        client_with_handler(transport).post(route(), credential(), {})

    assert raised.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert raised.value.retryable is True


def test_timeout_maps_to_retryable_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout", request=request)

    with pytest.raises(BusinessApiError) as raised:
        client_with_handler(httpx.MockTransport(handler)).post(route(), credential(), {})

    assert raised.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="private malformed body"),
        httpx.Response(200, json={"status": "invalid", "msg": "private malformed body"}),
        httpx.Response(200, json=[]),
    ],
)
def test_malformed_success_response_fails_closed(response: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda request: response)

    with pytest.raises(BusinessApiError) as raised:
        client_with_handler(transport).post(route(), credential(), {})

    assert raised.value.code == "SERVICE_RESPONSE_INVALID"
    assert str(raised.value) == "Service response could not be processed"


def test_errors_and_logs_do_not_expose_request_material(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    private_body = "private-upstream-body"
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text=private_body))

    with pytest.raises(BusinessApiError) as raised:
        client_with_handler(transport).post(route(), credential(), {"private": "payload"})

    exposed = f"{raised.value!r} {raised.value} {caplog.text}"
    for private_value in (
        credential().ak,
        credential().sk,
        private_body,
        "business.example.invalid",
        "https://",
        "payload",
    ):
        assert private_value not in exposed


def test_request_id_can_come_from_approved_response_header() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"status": 0},
            headers={"X-Request-ID": "header-request"},
        )
    )

    result = client_with_handler(transport).post(route(), credential(), {})

    assert result.request_id == "header-request"
