from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from atlas_cli.business_client import BusinessResponse
from atlas_cli.endpoints import CredentialSlot, SearchProvider, SearchRoute
from atlas_cli.search_adapters import BookingSearchAdapter, FareSearchAdapter, SearchAdapterError
from atlas_cli.search_models import SearchRequest
from atlas_cli.secure_store import ApiCredential


def search_request() -> SearchRequest:
    return SearchRequest(
        origin="KUL",
        destination="SIN",
        depart="2026-08-10",
        adults=2,
        children=1,
    )


def route(*, fare: bool, bookable: bool = False) -> SearchRoute:
    return SearchRoute(
        base_url="https://business.example.invalid",
        path="/priceCompareSearch.do" if fare else "/search.do",
        provider=SearchProvider.FARE_COMPARE if fare else SearchProvider.STANDARD,
        credential_slot=CredentialSlot.PRE if fare else CredentialSlot.PRODUCTION,
        bookable=bookable,
        generation="a" * 24,
    )


def credential() -> ApiCredential:
    return ApiCredential(ak="adapter-" + "ak", sk="adapter-" + "sk")


def routing(
    *,
    identifier: str = "upstream-routing",
    ancillary_supported: list[str] | None = None,
    carrier: str = "AK",
    operating_carrier: str | None = None,
) -> dict[str, object]:
    return {
        "routingIdentifier": identifier,
        "currency": "USD",
        "adultPrice": 100.0,
        "adultTax": 20.0,
        "childPrice": 80.0,
        "childTax": 10.0,
        "infantPrice": 0.0,
        "infantTax": 0.0,
        "transactionFee": 5.0,
        "transactionFeeMode": "PER_PAX",
        "fromSegments": [
            {
                "depAirport": "KUL",
                "arrAirport": "SIN",
                "depTime": "202608101000",
                "arrTime": "202608101110",
                "carrier": carrier,
                **({"operatingCarrier": operating_carrier} if operating_carrier is not None else {}),
                "flightNumber": "AK701",
                "duration": 70,
                "cabinClass": 1,
            }
        ],
        "retSegments": [],
        "refreshTime": "2026-08-10T01:00:00Z",
        "expireTime": "2026-08-10T02:00:00Z",
        "displayCurrency": "CNY",
        "displayAdultPrice": 999999,
        **({"ancillarySupported": ancillary_supported} if ancillary_supported is not None else {}),
    }


@dataclass
class FakeBusinessClient:
    response: BusinessResponse
    calls: list[tuple[SearchRoute, ApiCredential, dict[str, object]]] = field(default_factory=list)

    def post(
        self,
        selected_route: SearchRoute,
        selected_credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse:
        self.calls.append((selected_route, selected_credential, payload))
        return self.response


def response(*, status: int = 0, data: dict[str, object] | None = None) -> BusinessResponse:
    return BusinessResponse(status=status, msg=None, request_id="upstream-request", data=data or {})


def test_fare_search_is_reference_only_and_drops_booking_identifier() -> None:
    business = FakeBusinessClient(response(data={"routings": [routing()]}))
    adapter = FareSearchAdapter(business, request_id_factory=lambda: "client-request")

    result = adapter.search(route(fare=True), credential(), search_request())

    assert result.request_id == "upstream-request"
    assert len(result.offers) == 1
    offer = result.offers[0]
    assert offer.upstream_identifier is None
    assert offer.bookable is False
    assert offer.price_status == "reference"
    assert offer.total_price == 345.0
    assert offer.transaction_fee_total == 15.0
    assert offer.currency == "USD"
    assert [price.subtotal for price in offer.passenger_prices] == [240.0, 90.0]
    assert offer.segments[0].flight_number == "AK701"
    assert "requestId" not in business.calls[0][2]


@pytest.mark.parametrize("bookable", [False, True])
def test_standard_search_retains_identifier_and_uses_route_bookability(bookable: bool) -> None:
    business = FakeBusinessClient(response(data={"routings": [routing(identifier="booking-token")]}))
    adapter = BookingSearchAdapter(business, request_id_factory=lambda: "client-request")

    result = adapter.search(route(fare=False, bookable=bookable), credential(), search_request())

    assert result.offers[0].upstream_identifier == "booking-token"
    assert result.offers[0].bookable is bookable
    assert result.offers[0].price_status == "current"
    assert business.calls[0][2]["requestId"] == "client-request"


def test_standard_offer_normalizes_ancillary_capabilities() -> None:
    business = FakeBusinessClient(
        response(
            data={
                "routings": [
                    routing(ancillary_supported=["seat", "luggage"], carrier="SQ", operating_carrier="SQ")
                ]
            }
        )
    )

    offer = BookingSearchAdapter(business).search(route(fare=False), credential(), search_request()).offers[0]

    assert offer.ancillary_supported == ("baggage", "seat")
    assert offer.segments[0].operating_carrier == "SQ"
    assert offer.segments[0].direction == "outbound"


@pytest.mark.parametrize(
    ("carrier", "operating_carrier"),
    [("FR", "FR"), ("XX", "FR"), ("FR", "XX")],
)
def test_any_fr_marketing_or_operating_segment_filters_the_whole_offer(
    carrier: str,
    operating_carrier: str,
) -> None:
    business = FakeBusinessClient(
        response(data={"routings": [routing(carrier=carrier, operating_carrier=operating_carrier)]})
    )

    result = BookingSearchAdapter(business).search(route(fare=False), credential(), search_request())

    assert result.offers == []


def test_fr_routing_does_not_filter_other_routings() -> None:
    business = FakeBusinessClient(
        response(data={"routings": [routing(carrier="FR", operating_carrier="FR"), routing(identifier="allowed")]})
    )

    result = BookingSearchAdapter(business).search(route(fare=False), credential(), search_request())

    assert [offer.upstream_identifier for offer in result.offers] == ["allowed"]


def test_missing_ancillary_declaration_is_not_assumed_supported() -> None:
    business = FakeBusinessClient(response(data={"routings": [routing()]}))

    offer = BookingSearchAdapter(business).search(route(fare=False), credential(), search_request()).offers[0]

    assert offer.ancillary_supported == ()


@pytest.mark.parametrize(
    ("fee_mode", "fee", "separate_bookings", "expected_fee"),
    [
        ("PER_PAX", 2.0, False, 6.0),
        ("PER_SEGMENT", 2.0, False, 6.0),
        ("PER_TICKET", 2.0, True, 12.0),
        ("PER_BOOKING", 2.0, False, 2.0),
    ],
)
def test_transaction_fee_modes_are_included_in_settlement_total(
    fee_mode: str,
    fee: float,
    separate_bookings: bool,
    expected_fee: float,
) -> None:
    item = routing()
    item["transactionFeeMode"] = fee_mode
    item["transactionFee"] = fee
    item["separateBookings"] = separate_bookings
    business = FakeBusinessClient(response(data={"routings": [item]}))

    result = BookingSearchAdapter(business).search(route(fare=False), credential(), search_request())

    assert result.offers[0].transaction_fee_total == expected_fee
    assert result.offers[0].total_price == 330.0 + expected_fee


@pytest.mark.parametrize(
    ("reason_code", "reason"),
    [
        ("ROUTE_NOT_SUPPORTED", "route_not_supported"),
        ("AIRLINE_NO_FLIGHT", "no_flight"),
        ("FLIGHT_SOLD_OUT", "sold_out"),
    ],
)
def test_expected_no_result_reasons_are_successful_empty_searches(reason_code: str, reason: str) -> None:
    business = FakeBusinessClient(
        response(
            data={
                "routings": [],
                "noResultReason": {
                    "code": reason_code,
                    "message": "private text",
                    "recentFlightDates": ["20260809", "invalid", "20260811"],
                },
            }
        )
    )

    result = FareSearchAdapter(business).search(route(fare=True), credential(), search_request())

    assert result.offers == []
    assert result.reason == reason
    assert result.recent_flight_dates == ["2026-08-09", "2026-08-11"]


def test_price_fetch_failure_is_retryable() -> None:
    business = FakeBusinessClient(
        response(data={"routings": [], "noResultReason": {"code": "PRICE_FETCH_FAILED"}})
    )

    with pytest.raises(SearchAdapterError) as raised:
        FareSearchAdapter(business).search(route(fare=True), credential(), search_request())

    assert raised.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert raised.value.retryable is True


def test_status_109_is_search_limit_reached() -> None:
    business = FakeBusinessClient(response(status=109))

    with pytest.raises(SearchAdapterError) as raised:
        FareSearchAdapter(business).search(route(fare=True), credential(), search_request())

    assert raised.value.code == "SEARCH_LIMIT_REACHED"
    assert raised.value.retryable is False


@pytest.mark.parametrize("status", [110, 112, 9999])
def test_transient_business_status_is_retryable(status: int) -> None:
    business = FakeBusinessClient(response(status=status))

    with pytest.raises(SearchAdapterError) as raised:
        BookingSearchAdapter(business).search(route(fare=False), credential(), search_request())

    assert raised.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"routings": "not-a-list"},
        {"routings": [{}]},
        {"routings": [{**routing(), "currency": None}]},
        {"routings": [{**routing(), "fromSegments": [{}]}]},
    ],
)
def test_malformed_successful_response_is_invalid(data: dict[str, object]) -> None:
    business = FakeBusinessClient(response(data=data))

    with pytest.raises(SearchAdapterError) as raised:
        BookingSearchAdapter(business).search(route(fare=False), credential(), search_request())

    assert raised.value.code == "SERVICE_RESPONSE_INVALID"
    assert str(raised.value) == "Service response could not be processed"
