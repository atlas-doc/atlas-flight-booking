from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from atlas_cli.access import TransactionAccess
from atlas_cli.ancillaries import AncillaryAdapter, AncillaryService
from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.booking_store import BookingStore
from atlas_cli.business_client import AtlasBusinessClient
from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import BUSINESS_PATHS, BusinessOperation, BusinessRoute, CredentialSlot
from atlas_cli.orders import OrderAdapter, OrderService
from atlas_cli.payments import PaymentAdapter, PaymentService
from atlas_cli.routing_normalizer import RoutingNormalizer
from atlas_cli.search_models import (
    NormalizedOffer,
    NormalizedPassengerPrice,
    NormalizedSearch,
    NormalizedSegment,
    SearchRequest,
)
from atlas_cli.search_store import SearchStore
from atlas_cli.secure_store import ApiCredential, Credentials
from atlas_cli.ticketing import QueryOrderAdapter, TicketingService
from atlas_cli.verify import VerifyAdapter, VerifyService
from tests.fake_workflow_store import FakeWorkflowSecretStore

RUNNER = CliRunner()
GENERATION = "integration-generation-001"
BASE_URL = "https://booking.test.invalid"
ORDER_NO = "ATAXA202608050001"


class StaticSecrets(FakeWorkflowSecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.credentials = Credentials(jwt="fixture-jwt", client_code="fixture-client", cid="fixture-cid")

    def load_credentials(self) -> Credentials:
        return self.credentials


@dataclass
class StaticAccess:
    generation: str = GENERATION

    def resolve_transaction_access(
        self,
        jwt: str,
        operation: BusinessOperation,
        **_: object,
    ) -> TransactionAccess:
        assert jwt == "fixture-jwt"
        return TransactionAccess(
            route=BusinessRoute(
                base_url=BASE_URL,
                path=BUSINESS_PATHS[operation],
                operation=operation,
                credential_slot=CredentialSlot.PRODUCTION,
                generation=self.generation,
            ),
            credential=ApiCredential(ak="fixture-ak", sk="fixture-sk"),
        )


@dataclass
class ScriptedBusiness:
    responses: dict[str, list[dict[str, object] | tuple[int, dict[str, object]]]]
    paths: list[str] = field(default_factory=list)
    payloads: list[dict[str, object]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        self.payloads.append(json.loads(request.content))
        try:
            response = self.responses[path].pop(0)
        except (KeyError, IndexError) as error:
            raise AssertionError(f"unexpected mocked request path: {path}") from error
        if isinstance(response, tuple):
            status_code, body = response
            return httpx.Response(status_code, json=body, request=request)
        return httpx.Response(200, json=response, request=request)


def _counter() -> Callable[[], str]:
    value = 0

    def next_token() -> str:
        nonlocal value
        value += 1
        return str(value)

    return next_token


def _segment() -> NormalizedSegment:
    return NormalizedSegment(
        departure_airport="KUL",
        arrival_airport="SIN",
        departure_time="202608101000",
        arrival_time="202608101110",
        carrier="AK",
        flight_number="AK701",
        duration_minutes=70,
        cabin_class=1,
        direction="outbound",
    )


def _offer(
    *,
    price: float = 100.0,
    ancillary_supported: tuple[str, ...] = ("baggage", "seat"),
    carrier: str = "AK",
) -> NormalizedOffer:
    return NormalizedOffer(
        upstream_identifier="routing-private-to-store",
        currency="USD",
        total_price=price,
        transaction_fee_total=5.0,
        passenger_prices=(
            NormalizedPassengerPrice(
                passenger_type="adult", count=1, base_fare_per_passenger=75, tax_per_passenger=20, subtotal=95
            ),
        ),
        segments=(_segment().model_copy(update={"carrier": carrier, "flight_number": f"{carrier}701"}),),
        ancillary_supported=ancillary_supported,
        bookable=True,
        price_status="current",
    )


def _routing(
    *, price: float = 100.0, ancillary_supported: list[str] | None = None, carrier: str = "AK"
) -> dict[str, object]:
    routing: dict[str, object] = {
        "currency": "USD",
        "adultPrice": price - 25,
        "adultTax": 20,
        "transactionFee": 5,
        "transactionFeeMode": "PER_BOOKING",
        "fromSegments": [
            {
                "depAirport": "KUL",
                "arrAirport": "SIN",
                "depTime": "202608101000",
                "arrTime": "202608101110",
                "carrier": carrier,
                "flightNumber": f"{carrier}701",
                "duration": 70,
                "cabinClass": 1,
            }
        ],
        "retSegments": [],
    }
    if ancillary_supported is not None:
        routing["ancillarySupported"] = ancillary_supported
    return routing


def _verify_response(
    *, price: float = 100.0, ancillary_supported: list[str] | None = None, carrier: str = "AK"
) -> dict[str, object]:
    return {
        "status": 0,
        "requestId": "verify-request",
        "sessionId": "private-session-value",
        "routing": _routing(price=price, ancillary_supported=ancillary_supported, carrier=carrier),
        "bookingRequirement": {
            "passenger": {
                "name": {"required": True},
                "passengerType": {"required": True},
                "gender": {"required": True},
                "birthday": {"required": False},
                "cardType": {"required": True},
                "cardNum": {"required": True},
                "cardIssuePlace": {"required": True},
                "cardExpired": {"required": True},
                "nationality": {"required": True},
            }
        },
    }


def _baggage_response() -> dict[str, object]:
    return {
        "status": 0,
        "data": {
            "ancillaryProductElements": [
                {
                    "auxBaggageElement": {"piece": 1, "weight": 20, "size": "158CM"},
                    "categoryCode": "StandardCheckInBaggage",
                    "currency": "USD",
                    "price": 30,
                    "productCode": "private-baggage-product",
                    "segmentIndex": 1,
                }
            ]
        },
    }


def _seat_response() -> dict[str, object]:
    return {
        "status": 0,
        "cabins": [
            {
                "segmentIndex": 1,
                "cabin": {
                    "rows": [
                        {
                            "number": 5,
                            "seats": [
                                {
                                    "column": "A",
                                    "seatStatus": "F",
                                    "seatCharacteristics": ["W"],
                                    "price": 12,
                                    "currency": "USD",
                                    "productCode": "private-seat-product",
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    }


def _created_order_response() -> dict[str, object]:
    return {
        "status": 0,
        "orderNo": ORDER_NO,
        "totalPrice": 142,
        "totalTransactionFee": 5,
        "currency": "USD",
        "tktLimitTime": "2099-08-05 12:00:00",
        "paymentOptions": [{"paymentMethod": 1}],
    }


def _ticketed_response() -> dict[str, object]:
    return {
        "status": 0,
        "orderNo": ORDER_NO,
        "orderStatus": "2",
        "ticketStatus": "1",
        "paxTicketInfos": [{"airlinePNRs": ["PNR001"], "ticketNos": ["7811234567890"]}],
    }


def _script(
    *,
    include_ancillaries: bool = True,
    verify_price: float = 100.0,
    verify_carrier: str = "AK",
    baggage_response: dict[str, object] | None = None,
    seat_response: dict[str, object] | None = None,
    order_response: dict[str, object] | tuple[int, dict[str, object]] | None = None,
    pay_response: dict[str, object] | tuple[int, dict[str, object]] | None = None,
    query_response: dict[str, object] | None = None,
) -> ScriptedBusiness:
    responses: dict[str, list[dict[str, object] | tuple[int, dict[str, object]]]] = {
        "/verify.do": [
            _verify_response(
                price=verify_price,
                ancillary_supported=["luggage", "seat"] if include_ancillaries else [],
                carrier=verify_carrier,
            )
        ],
        "/order.do": [order_response or _created_order_response()],
        "/pay.do": [pay_response or {"status": 0, "orderNo": ORDER_NO}],
        "/queryOrderDetails.do": [query_response or _ticketed_response()],
    }
    if include_ancillaries:
        responses["/getLuggage.do"] = [baggage_response or _baggage_response()]
        responses["/seatAvailability.do"] = [seat_response or _seat_response()]
    return ScriptedBusiness(responses)


def _runtime(
    tmp_path: Path,
    script: ScriptedBusiness,
    *,
    offer: NormalizedOffer | None = None,
    access: StaticAccess | None = None,
    monotonic: Callable[[], float] | None = None,
    secrets: StaticSecrets | None = None,
    seed_search: bool = True,
) -> BookingRuntime:
    token = _counter()
    selected_secrets = secrets or StaticSecrets()
    selected_access = access or StaticAccess()
    store = BookingStore(tmp_path / "booking", secrets=selected_secrets, token_factory=token)
    search_store = SearchStore(tmp_path / "search", secrets=selected_secrets, token_factory=token)
    if seed_search:
        stored = search_store.save(
            SearchRequest(origin="KUL", destination="SIN", depart="2026-08-10", adults=1),
            NormalizedSearch(offers=(offer or _offer(),), request_id="search-request"),
            selected_access.generation,
        )
        assert stored.offers[0].offer_id == "off_3"
    client = httpx.Client(transport=httpx.MockTransport(script.handler))
    business = AtlasBusinessClient(InternalSettings(), client=client)
    verify = VerifyService(
        secrets=selected_secrets,
        access=selected_access,
        adapter=VerifyAdapter(business, RoutingNormalizer()),
        search_store=search_store,
        booking_store=store,
        token_factory=token,
    )
    ancillaries = AncillaryService(
        secrets=selected_secrets,
        access=selected_access,
        adapter=AncillaryAdapter(business),
        booking_store=store,
    )
    ticketing = TicketingService(
        secrets=selected_secrets,
        access=selected_access,
        adapter=QueryOrderAdapter(business),
        booking_store=store,
        sleep=lambda _: None,
        monotonic=monotonic,
    )
    return BookingRuntime(
        verify=verify,
        ancillaries=ancillaries,
        orders=OrderService(
            secrets=selected_secrets,
            access=selected_access,
            adapter=OrderAdapter(business),
            booking_store=store,
        ),
        ticketing=ticketing,
        payments=PaymentService(
            secrets=selected_secrets,
            access=selected_access,
            adapter=PaymentAdapter(business),
            booking_store=store,
            ticketing=ticketing,
        ),
    )


def _invoke_json(runner: CliRunner, args: list[str], *, expected_exit: int = 0) -> dict[str, object]:
    result = runner.invoke(__import__("atlas_cli.cli", fromlist=["app"]).app, [*args, "--json"])
    assert result.exit_code == expected_exit
    assert result.stderr == ""
    return json.loads(result.stdout)


def _passengers(traveler_id: str) -> str:
    return json.dumps(
        {
            "passengers": [
                {
                    "traveler_id": traveler_id,
                    "name": "SYNTHETIC/EXAMPLE",
                    "passenger_type": "adult",
                    "gender": "F",
                    "nationality": "SG",
                    "document": {
                        "type": "PP",
                        "number": "SYNTHETICDOC0001",
                        "issuing_country": "SG",
                        "expires": "2099-08-05",
                    },
                }
            ],
            "contact": {"name": "SYNTHETIC/EXAMPLE", "email": "synthetic@example.invalid"},
        }
    )


def _create_order(
    booking_id: str, traveler_id: str, *, seat_policy: str | None = None, expected_exit: int = 0
) -> dict[str, object]:
    args = ["order", "create", "--booking-id", booking_id, "--passengers-stdin"]
    if seat_policy is not None:
        args.extend(["--seat-policy", seat_policy])
    result = RUNNER.invoke(
        __import__("atlas_cli.cli", fromlist=["app"]).app,
        [*args, "--json"],
        input=_passengers(traveler_id),
    )
    assert result.exit_code == expected_exit
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_flow_survives_independent_cli_service_instances(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each command rebuilds stores/services while public files and secure records carry the flow."""
    script = _script(include_ancillaries=False)
    secrets = StaticSecrets()
    verify_runtime = _runtime(
        tmp_path,
        script,
        offer=_offer(ancillary_supported=()),
        secrets=secrets,
    )
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: verify_runtime)
    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    booking_id = str(verified["data"]["booking_id"])
    traveler_id = str(verified["data"]["travelers"][0]["traveler_id"])

    order_runtime = _runtime(tmp_path, script, secrets=secrets, seed_search=False)
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: order_runtime)
    created = _create_order(booking_id, traveler_id)
    confirmation_id = str(created["data"]["payment_confirmation_id"])

    payment_runtime = _runtime(tmp_path, script, secrets=secrets, seed_search=False)
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: payment_runtime)
    paid = _invoke_json(
        RUNNER,
        ["order", "pay", "--confirmation-id", confirmation_id],
    )

    assert paid["code"] == "TICKETED"
    assert script.paths == ["/verify.do", "/order.do", "/pay.do", "/queryOrderDetails.do"]


def test_complete_flow_with_baggage_and_seat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A broken shared store or adapter path must prevent the CLI flow from reaching ticketing."""
    script = _script()
    runtime = _runtime(tmp_path, script)
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    booking_id = str(verified["data"]["booking_id"])
    baggage = _invoke_json(RUNNER, ["booking", "baggage", "list", "--booking-id", booking_id])
    seats = _invoke_json(RUNNER, ["booking", "seat", "list", "--booking-id", booking_id])
    traveler_id = str(verified["data"]["travelers"][0]["traveler_id"])
    segment_id = str(verified["data"]["segments"][0]["segment_id"])
    _invoke_json(
        RUNNER,
        [
            "booking",
            "baggage",
            "select",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
            "--baggage-id",
            str(baggage["data"]["options"][0]["baggage_id"]),
        ],
    )
    _invoke_json(
        RUNNER,
        [
            "booking",
            "seat",
            "select",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
            "--seat-id",
            str(seats["data"]["options"][0]["seat_id"]),
        ],
    )
    created = _create_order(booking_id, traveler_id, seat_policy="accept-similar-seat")
    assert created["code"] == "PAYMENT_CONFIRMATION_REQUIRED"
    paid = _invoke_json(
        RUNNER,
        ["order", "pay", "--confirmation-id", str(created["data"]["payment_confirmation_id"])],
    )

    assert paid["code"] in {"TICKETED", "TICKETING_PENDING"}
    assert "next_action" not in paid
    assert script.paths == [
        "/verify.do",
        "/getLuggage.do",
        "/seatAvailability.do",
        "/order.do",
        "/pay.do",
        "/queryOrderDetails.do",
    ]
    assert "/orderCommit.do" not in script.paths
    assert script.payloads[3]["ifSeatOccupied"] == "SIMILAR_SEAT"


@pytest.mark.parametrize(
    ("supported", "expected_calls"),
    [
        ((), ["/verify.do", "/order.do", "/pay.do", "/queryOrderDetails.do"]),
        (("baggage",), ["/verify.do", "/getLuggage.do", "/order.do", "/pay.do", "/queryOrderDetails.do"]),
        (("seat",), ["/verify.do", "/seatAvailability.do", "/order.do", "/pay.do", "/queryOrderDetails.do"]),
    ],
)
def test_flow_supports_each_optional_ancillary_combination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    supported: tuple[str, ...],
    expected_calls: list[str],
) -> None:
    """A wrong capability intersection must not trigger an optional upstream request."""
    script = _script(include_ancillaries=bool(supported))
    runtime = _runtime(tmp_path, script, offer=_offer(ancillary_supported=supported))
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    booking_id = str(verified["data"]["booking_id"])
    traveler_id = str(verified["data"]["travelers"][0]["traveler_id"])
    if "baggage" in supported:
        listed = _invoke_json(RUNNER, ["booking", "baggage", "list", "--booking-id", booking_id])
        assert listed["code"] == "BAGGAGE_OPTIONS_LISTED"
    else:
        unavailable = _invoke_json(RUNNER, ["booking", "baggage", "list", "--booking-id", booking_id])
        assert unavailable["code"] == "BAGGAGE_UNAVAILABLE"
    if "seat" in supported:
        listed = _invoke_json(RUNNER, ["booking", "seat", "list", "--booking-id", booking_id])
        assert listed["code"] == "SEAT_OPTIONS_LISTED"
    else:
        unavailable = _invoke_json(RUNNER, ["booking", "seat", "list", "--booking-id", booking_id])
        assert unavailable["code"] == "SEAT_UNAVAILABLE"
    created = _create_order(booking_id, traveler_id)
    paid = _invoke_json(
        RUNNER,
        ["order", "pay", "--confirmation-id", str(created["data"]["payment_confirmation_id"])],
    )

    assert created["code"] == "PAYMENT_CONFIRMATION_REQUIRED"
    assert paid["code"] == "TICKETED"
    assert script.paths == expected_calls


def test_verify_decrease_reports_both_prices_without_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A price decrease must remain immediately usable while preserving comparison data."""
    script = _script(include_ancillaries=False, verify_price=90.0)
    runtime = _runtime(tmp_path, script, offer=_offer(price=100.0, ancillary_supported=()))
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    result = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])

    assert result["code"] == "OFFER_VERIFIED"
    assert result["data"]["price_change"] == "decreased"
    assert result["data"]["previous_price"] == 100.0
    assert result["data"]["current_price"] == 90.0


def test_verify_increase_requires_then_records_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An order must not be possible until an observed price increase is confirmed."""
    script = _script(include_ancillaries=False, verify_price=120.0)
    runtime = _runtime(tmp_path, script, offer=_offer(price=100.0, ancillary_supported=()))
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    confirmed = _invoke_json(
        RUNNER,
        ["booking", "confirm-price", "--booking-id", str(verified["data"]["booking_id"])],
    )

    assert verified["code"] == "PRICE_CONFIRMATION_REQUIRED"
    assert confirmed["code"] == "PRICE_CONFIRMED"
    assert confirmed["data"] == verified["data"]


@pytest.mark.parametrize(
    ("policy", "upstream"),
    [
        ("continue-without-seat", "STOP_SEAT"),
        ("cancel-order", "STOP_TICKET"),
        ("accept-similar-seat", "SIMILAR_SEAT"),
    ],
)
def test_each_seat_policy_is_forwarded_only_after_a_seat_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, policy: str, upstream: str
) -> None:
    """Changing the seat policy mapping must change the actual order boundary payload."""
    script = _script()
    runtime = _runtime(tmp_path, script)
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)
    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    booking_id = str(verified["data"]["booking_id"])
    traveler_id = str(verified["data"]["travelers"][0]["traveler_id"])
    segment_id = str(verified["data"]["segments"][0]["segment_id"])
    seats = _invoke_json(RUNNER, ["booking", "seat", "list", "--booking-id", booking_id])
    _invoke_json(
        RUNNER,
        [
            "booking",
            "seat",
            "select",
            "--booking-id",
            booking_id,
            "--traveler-id",
            traveler_id,
            "--segment-id",
            segment_id,
            "--seat-id",
            str(seats["data"]["options"][0]["seat_id"]),
        ],
    )

    created = _create_order(booking_id, traveler_id, seat_policy=policy)

    assert created["code"] == "PAYMENT_CONFIRMATION_REQUIRED"
    assert script.payloads[-1]["ifSeatOccupied"] == upstream


def test_payment_uncertainty_recovers_by_query_without_a_second_payment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transport failure after payment starts must query status rather than submit again."""
    script = _script(
        include_ancillaries=False,
        pay_response=(503, {"private": "unavailable"}),
    )
    runtime = _runtime(tmp_path, script, offer=_offer(ancillary_supported=()))
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)
    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    created = _create_order(str(verified["data"]["booking_id"]), str(verified["data"]["travelers"][0]["traveler_id"]))

    paid = _invoke_json(
        RUNNER,
        ["order", "pay", "--confirmation-id", str(created["data"]["payment_confirmation_id"])],
    )

    assert paid["code"] == "TICKETED"
    assert script.paths == ["/verify.do", "/order.do", "/pay.do", "/queryOrderDetails.do"]


def test_polling_timeout_does_not_start_an_upstream_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A depleted poll budget must return pending before an HTTP request can start."""
    ticks = iter((0.0, 121.0))
    script = _script(include_ancillaries=False)
    runtime = _runtime(tmp_path, script, monotonic=ticks.__next__)
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    result = _invoke_json(RUNNER, ["order", "status", "--order-no", ORDER_NO])

    assert result["code"] == "TICKETING_PENDING"
    assert script.paths == []


def test_fr_offer_and_generation_invalidation_never_reach_business_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bypassed FR offers and changed routing generations must fail before their next adapter call."""
    fr_script = _script(include_ancillaries=False, verify_carrier="FR")
    fr_runtime = _runtime(tmp_path / "fr", fr_script, offer=_offer(ancillary_supported=(), carrier="FR"))
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: fr_runtime)
    fr = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"], expected_exit=30)

    access = StaticAccess()
    generation_script = _script()
    generation_runtime = _runtime(tmp_path / "generation", generation_script, access=access)
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: generation_runtime)
    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    access.generation = "replacement-generation"
    invalidated = _invoke_json(
        RUNNER,
        ["booking", "baggage", "list", "--booking-id", str(verified["data"]["booking_id"])],
        expected_exit=30,
    )

    assert fr["code"] == "OFFER_EXPIRED"
    assert fr_script.paths == []
    assert invalidated["code"] == "OFFER_EXPIRED"
    assert generation_script.paths == ["/verify.do"]


def test_order_create_uncertainty_is_not_replayed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed order response after dispatch must lock the context instead of replaying the order."""
    script = _script(include_ancillaries=False, order_response=(503, {"private": "unavailable"}))
    runtime = _runtime(tmp_path, script, offer=_offer(ancillary_supported=()))
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)
    verified = _invoke_json(RUNNER, ["offer", "verify", "--offer-id", "off_3"])
    booking_id = str(verified["data"]["booking_id"])
    traveler_id = str(verified["data"]["travelers"][0]["traveler_id"])

    first = _create_order(booking_id, traveler_id)
    second = _create_order(booking_id, traveler_id, expected_exit=30)

    assert first["code"] == "ORDER_CREATION_UNKNOWN"
    assert second["code"] == "ORDER_STATE_INVALID"
    assert script.paths == ["/verify.do", "/order.do"]
