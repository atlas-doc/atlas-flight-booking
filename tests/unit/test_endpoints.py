import pytest

from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import BusinessOperation, CredentialSlot, CustomerMode, EndpointResolver


def settings() -> InternalSettings:
    return InternalSettings(
        control_api_base_url="https://control.example.invalid",
        prod_api_base_url="https://prod.example.invalid/",
        sandbox_api_base_url="https://sandbox.example.invalid/",
        authorization_page_url="https://web.example.invalid/login",
    )


def test_prod_search_route_uses_standard_search_only_for_live_status() -> None:
    resolver = EndpointResolver(settings())
    expected = {
        1: ("fare_compare", "/priceCompareSearch.do", "pre"),
        2: ("fare_compare", "/priceCompareSearch.do", "pre"),
        3: ("standard", "/search.do", "production"),
        4: ("fare_compare", "/priceCompareSearch.do", "pre"),
    }

    actual = {
        activation: (
            route.provider.value,
            route.path,
            route.credential_slot.value,
        )
        for activation in expected
        for route in [
            resolver.resolve_search(
                activation_status=activation,
                top_up_completed=False,
                mode=CustomerMode.PROD,
            )
        ]
    }

    assert actual == expected


def test_prod_route_is_bookable_only_for_live_and_topped_up() -> None:
    resolver = EndpointResolver(settings())

    actual = {
        (activation, topped_up): resolver.resolve_search(
            activation_status=activation,
            top_up_completed=topped_up,
            mode=CustomerMode.PROD,
        ).bookable
        for activation in (1, 2, 3, 4)
        for topped_up in (False, True)
    }

    assert actual == {
        (1, False): False,
        (1, True): False,
        (2, False): False,
        (2, True): False,
        (3, False): False,
        (3, True): True,
        (4, False): False,
        (4, True): False,
    }


def test_sandbox_route_uses_standard_search_and_its_credential_slot() -> None:
    route = EndpointResolver(settings()).resolve_search(
        activation_status=1,
        top_up_completed=False,
        mode=CustomerMode.SANDBOX,
    )

    assert route.base_url == "https://sandbox.example.invalid"
    assert route.path == "/search.do"
    assert route.provider.value == "standard"
    assert route.credential_slot.value == "sandbox"
    assert route.bookable is True


def test_route_generation_changes_for_every_capability_boundary() -> None:
    resolver = EndpointResolver(settings())
    routes = [
        resolver.resolve_search(activation_status=1, top_up_completed=False, mode=CustomerMode.PROD),
        resolver.resolve_search(activation_status=2, top_up_completed=False, mode=CustomerMode.PROD),
        resolver.resolve_search(activation_status=3, top_up_completed=False, mode=CustomerMode.PROD),
        resolver.resolve_search(activation_status=3, top_up_completed=True, mode=CustomerMode.PROD),
        resolver.resolve_search(activation_status=3, top_up_completed=True, mode=CustomerMode.SANDBOX),
    ]

    assert len({route.generation for route in routes}) == len(routes)
    assert all(len(route.generation) == 24 for route in routes)


@pytest.mark.parametrize(
    ("operation", "expected_path"),
    [
        (BusinessOperation.VERIFY, "/verify.do"),
        (BusinessOperation.BAGGAGE, "/getLuggage.do"),
        (BusinessOperation.SEAT, "/seatAvailability.do"),
        (BusinessOperation.ORDER, "/order.do"),
        (BusinessOperation.PAY, "/pay.do"),
        (BusinessOperation.QUERY_ORDER, "/queryOrderDetails.do"),
    ],
)
def test_business_route_uses_configured_host_and_generation(operation, expected_path):
    settings = InternalSettings(prod_api_base_url="https://business.example.invalid/")
    resolver = EndpointResolver(settings)
    route = resolver.resolve_business(
        operation=operation,
        activation_status=3,
        top_up_completed=True,
        mode=CustomerMode.PROD,
    )
    assert route.base_url == "https://business.example.invalid"
    assert route.path == expected_path
    assert route.credential_slot is CredentialSlot.PRODUCTION
    assert len(route.generation) == 24
    assert route.generation == resolver.resolve_search(
        activation_status=3,
        top_up_completed=True,
        mode=CustomerMode.PROD,
    ).generation


@pytest.mark.parametrize(
    ("operation", "expected_path"),
    [
        (BusinessOperation.VERIFY, "/verify.do"),
        (BusinessOperation.BAGGAGE, "/getLuggage.do"),
        (BusinessOperation.SEAT, "/seatAvailability.do"),
        (BusinessOperation.ORDER, "/order.do"),
        (BusinessOperation.PAY, "/pay.do"),
        (BusinessOperation.QUERY_ORDER, "/queryOrderDetails.do"),
    ],
)
def test_sandbox_business_route_uses_sandbox_host_and_credential(operation, expected_path) -> None:
    resolver = EndpointResolver(settings())

    route = resolver.resolve_business(
        operation=operation,
        activation_status=1,
        top_up_completed=False,
        mode=CustomerMode.SANDBOX,
    )

    assert route.base_url == "https://sandbox.example.invalid"
    assert route.path == expected_path
    assert route.credential_slot is CredentialSlot.SANDBOX
    assert route.generation == resolver.resolve_search(
        activation_status=1,
        top_up_completed=False,
        mode=CustomerMode.SANDBOX,
    ).generation


@pytest.mark.parametrize(
    ("activation_status", "top_up_completed", "mode"),
    [
        (1, True, CustomerMode.PROD),
        (2, True, CustomerMode.PROD),
        (3, False, CustomerMode.PROD),
    ],
)
def test_business_route_rejects_non_bookable_customer_state(
    activation_status, top_up_completed, mode
) -> None:
    with pytest.raises(ValueError):
        EndpointResolver(settings()).resolve_business(
            operation=BusinessOperation.VERIFY,
            activation_status=activation_status,
            top_up_completed=top_up_completed,
            mode=mode,
        )


def test_subscription_url_uses_configured_public_link() -> None:
    settings = InternalSettings(subscription_page_url="https://atriptech.example.invalid/subscription")

    assert EndpointResolver(settings).subscription_url == "https://atriptech.example.invalid/subscription"


def test_order_url_quotes_only_the_order_number():
    settings = InternalSettings(order_detail_url_template="https://www.atriptech.com/#/order/detail/{order_no}/en")
    assert EndpointResolver(settings).order_url("AT AX/1") == (
        "https://www.atriptech.com/#/order/detail/AT%20AX%2F1/en"
    )
