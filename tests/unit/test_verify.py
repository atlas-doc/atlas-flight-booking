from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atlas_cli.access import AccessManagerError, TransactionAccess
from atlas_cli.booking_models import BookingRequirements
from atlas_cli.booking_store import BookingStore
from atlas_cli.business_client import BusinessApiError, BusinessResponse
from atlas_cli.business_status import BookingApiError, BusinessStage, map_business_status
from atlas_cli.endpoints import BusinessOperation, BusinessRoute, CredentialSlot
from atlas_cli.models import CommandStatus
from atlas_cli.routing_normalizer import RoutingNormalizer
from atlas_cli.search_models import (
    NormalizedOffer,
    NormalizedPassengerPrice,
    NormalizedSearch,
    NormalizedSegment,
    SearchRequest,
)
from atlas_cli.search_store import SearchStore
from atlas_cli.secure_store import ApiCredential, Credentials, SearchSecrets
from atlas_cli.verify import VerifiedResponse, VerifyAdapter, VerifyService, normalize_requirements
from tests.fake_workflow_store import FakeWorkflowSecretStore

NOW = datetime(2026, 8, 5, 8, tzinfo=UTC)
GENERATION = "g" * 24
DEFAULT_CREDENTIALS = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")


def request(*, children: int = 0, infants: int = 0, return_date: str | None = None) -> SearchRequest:
    return SearchRequest(
        origin="KUL",
        destination="SIN",
        depart="2026-08-10",
        return_date=return_date,
        adults=1,
        children=children,
        infants=infants,
    )


def segment(
    *,
    direction: str = "outbound",
    carrier: str = "AK",
    flight_number: str = "AK701",
) -> NormalizedSegment:
    return NormalizedSegment(
        departure_airport="KUL" if direction == "outbound" else "SIN",
        arrival_airport="SIN" if direction == "outbound" else "KUL",
        departure_time="202608101000",
        arrival_time="202608101110",
        carrier=carrier,
        flight_number=flight_number,
        duration_minutes=70,
        cabin_class=1,
        direction=direction,
    )


def offer(
    total: float,
    *,
    currency: str = "USD",
    bookable: bool = True,
    price_status: str = "current",
    identifier: str | None = "private-routing-token",
    supported: tuple[str, ...] = ("baggage", "seat"),
    segments: list[NormalizedSegment] | None = None,
) -> NormalizedOffer:
    return NormalizedOffer(
        upstream_identifier=identifier,
        currency=currency,
        total_price=total,
        transaction_fee_total=5,
        passenger_prices=[
            NormalizedPassengerPrice(
                passenger_type="adult",
                count=1,
                base_fare_per_passenger=total - 25,
                tax_per_passenger=20,
                subtotal=total - 5,
            )
        ],
        segments=segments or [segment()],
        ancillary_supported=supported,
        bookable=bookable,
        price_status=price_status,
    )


def transaction_access(*, generation: str = GENERATION) -> TransactionAccess:
    return TransactionAccess(
        route=BusinessRoute(
            base_url="https://business.example.invalid",
            path="/verify.do",
            operation=BusinessOperation.VERIFY,
            credential_slot=CredentialSlot.PRODUCTION,
            generation=generation,
        ),
        credential=ApiCredential(ak="private-" + "ak", sk="private-" + "sk"),
        request_id="access-request",
    )


@dataclass
class FakeSecrets:
    credentials: Credentials | None = field(
        default_factory=lambda: Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    )
    searches: dict[str, SearchSecrets] = field(default_factory=dict)

    def load_credentials(self) -> Credentials | None:
        return self.credentials

    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None:
        self.searches[secret_ref] = value

    def load_search_secrets(self, secret_ref: str) -> SearchSecrets | None:
        return self.searches.get(secret_ref)

    def clear_search_secrets(self, secret_ref: str) -> None:
        self.searches.pop(secret_ref, None)


@dataclass
class FakeAccess:
    outcome: TransactionAccess | AccessManagerError = field(default_factory=transaction_access)
    calls: list[tuple[str, BusinessOperation]] = field(default_factory=list)

    def resolve_transaction_access(self, jwt: str, operation: BusinessOperation) -> TransactionAccess:
        self.calls.append((jwt, operation))
        if isinstance(self.outcome, AccessManagerError):
            raise self.outcome
        return self.outcome


@dataclass
class FakeVerifyAdapter:
    outcomes: list[VerifiedResponse | BookingApiError | BusinessApiError]
    calls: list[tuple[BusinessRoute, ApiCredential, str, SearchRequest]] = field(default_factory=list)

    def verify(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        *,
        routing_identifier: str,
        request: SearchRequest,
    ) -> VerifiedResponse:
        self.calls.append((route, credential, routing_identifier, request))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, (BookingApiError, BusinessApiError)):
            raise outcome
        return outcome


def response_for(
    verified: NormalizedOffer,
    *,
    requirements: BookingRequirements | None = None,
) -> VerifiedResponse:
    return VerifiedResponse(
        session_id="private-session",
        verified_offer=verified,
        requirements=requirements or BookingRequirements(required_fields=("name", "passenger_type", "gender")),
        ancillary_supported=verified.ancillary_supported,
        request_id="verify-request",
    )


def make_service(
    tmp_path: Path,
    *,
    searched: NormalizedOffer | None = None,
    verified: NormalizedOffer | None = None,
    saved_request: SearchRequest | None = None,
    credentials: Credentials | None = DEFAULT_CREDENTIALS,
    access: FakeAccess | None = None,
    outcomes: list[VerifiedResponse | BookingApiError | BusinessApiError] | None = None,
    now=lambda: NOW,
) -> tuple[VerifyService, FakeVerifyAdapter, FakeAccess, SearchStore, BookingStore, str]:
    secrets = FakeSecrets(credentials)
    search_store = SearchStore(
        tmp_path / "search",
        secrets=secrets,
        token_factory=iter(("search", "secretvalid00", "valid")).__next__,
        now=now,
    )
    selected_searched = searched or offer(100)
    stored = search_store.save(
        saved_request or request(),
        NormalizedSearch(offers=[selected_searched], request_id="search-request"),
        GENERATION,
    )
    manager = access or FakeAccess()
    selected_verified = verified or offer(100, price_status="verified", identifier=None)
    adapter = FakeVerifyAdapter(outcomes or [response_for(selected_verified)])
    booking_store = BookingStore(
        tmp_path / "booking", secrets=FakeWorkflowSecretStore(), token_factory=iter(("booking",)).__next__, now=now
    )
    service = VerifyService(
        secrets=secrets,
        access=manager,
        adapter=adapter,
        search_store=search_store,
        booking_store=booking_store,
        now=now,
    )
    return service, adapter, manager, search_store, booking_store, stored.offers[0].offer_id


def test_verify_decrease_returns_success_and_both_prices(tmp_path: Path) -> None:
    service, _, _, _, _, offer_id = make_service(
        tmp_path,
        searched=offer(120),
        verified=offer(100, price_status="verified", identifier=None),
    )

    result = service.verify(offer_id)

    assert result.code == "OFFER_VERIFIED"
    assert result.status is CommandStatus.SUCCESS
    assert result.message == "Offer verified"
    assert result.data["price_change"] == "decreased"
    assert result.data["previous_price"] == 120.0
    assert result.data["current_price"] == 100.0


def test_verify_increase_requires_fresh_confirmation(tmp_path: Path) -> None:
    service, _, _, _, _, offer_id = make_service(
        tmp_path,
        searched=offer(100),
        verified=offer(120, price_status="verified", identifier=None),
    )

    result = service.verify(offer_id)

    assert result.code == "PRICE_CONFIRMATION_REQUIRED"
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.message == "Price confirmation required"
    booking_id = str(result.data["booking_id"])
    confirmed = service.confirm_price(booking_id)
    assert confirmed.code == "PRICE_CONFIRMED"
    assert confirmed.status is CommandStatus.SUCCESS
    assert confirmed.message == "Price confirmed"
    assert confirmed.data == result.data


def test_current_nonbookable_offer_can_verify_after_transaction_access_is_enabled(tmp_path: Path) -> None:
    service, adapter, _, _, _, offer_id = make_service(
        tmp_path,
        searched=offer(100, bookable=False, price_status="current"),
    )

    result = service.verify(offer_id)

    assert result.code == "OFFER_VERIFIED"
    assert len(adapter.calls) == 1
    assert adapter.calls[0][2] == "private-routing-token"


def test_reference_offer_never_calls_verify(tmp_path: Path) -> None:
    service, adapter, _, _, _, offer_id = make_service(
        tmp_path,
        searched=offer(100, bookable=False, price_status="reference", identifier=None),
    )

    result = service.verify(offer_id)

    assert result.code == "SUBSCRIPTION_REQUIRED"
    assert adapter.calls == []


def test_verified_context_never_lives_longer_than_two_hours(tmp_path: Path) -> None:
    service, _, _, _, booking_store, offer_id = make_service(tmp_path)

    result = service.verify(offer_id)
    context = booking_store.load(str(result.data["booking_id"]), generation=GENERATION)

    assert context.expires_at == NOW + timedelta(hours=2)


def test_missing_authorization_does_not_resolve_access_or_call_verify(tmp_path: Path) -> None:
    service, adapter, access, _, _, offer_id = make_service(tmp_path, credentials=None)

    result = service.verify(offer_id)

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.data == {"authenticated": False}
    assert access.calls == []
    assert adapter.calls == []


@pytest.mark.parametrize("jwt", ["", " \t\n"])
def test_blank_authorization_does_not_resolve_access_or_call_verify(tmp_path: Path, jwt: str) -> None:
    credentials = Credentials(jwt=jwt, client_code="CLIENT", cid="CUSTOMER")
    service, adapter, access, _, _, offer_id = make_service(tmp_path, credentials=credentials)

    result = service.verify(offer_id)

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.data == {"authenticated": False}
    assert access.calls == []
    assert adapter.calls == []


@pytest.mark.parametrize("jwt", ["", " \t\n"])
def test_blank_authorization_does_not_resolve_access_for_price_confirmation(
    tmp_path: Path,
    jwt: str,
) -> None:
    credentials = Credentials(jwt=jwt, client_code="CLIENT", cid="CUSTOMER")
    service, adapter, access, _, _, _ = make_service(tmp_path, credentials=credentials)

    result = service.confirm_price("book_missing")

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.status is CommandStatus.ACTION_REQUIRED
    assert result.data == {"authenticated": False}
    assert access.calls == []
    assert adapter.calls == []


def test_transaction_access_is_checked_before_loading_offer(tmp_path: Path) -> None:
    denied = AccessManagerError(
        code="SUBSCRIPTION_REQUIRED",
        message="Subscription required",
        details={"url": "https://subscribe.example.invalid", "private": "do-not-copy"},
    )
    manager = FakeAccess(denied)
    service, adapter, _, _, _, _ = make_service(tmp_path, access=manager)

    result = service.verify("off_missing")

    assert result.code == "SUBSCRIPTION_REQUIRED"
    assert result.details == {"url": "https://subscribe.example.invalid"}
    assert adapter.calls == []


def test_generation_mismatch_never_calls_verify(tmp_path: Path) -> None:
    manager = FakeAccess(transaction_access(generation="h" * 24))
    service, adapter, _, _, _, offer_id = make_service(tmp_path, access=manager)

    result = service.verify(offer_id)

    assert result.code == "OFFER_EXPIRED"
    assert adapter.calls == []


@pytest.mark.parametrize("code", ["PRICE_VERIFICATION_UNAVAILABLE", "SERVICE_TEMPORARILY_UNAVAILABLE"])
def test_documented_transient_verify_failure_retries_exactly_once(tmp_path: Path, code: str) -> None:
    if code == "PRICE_VERIFICATION_UNAVAILABLE":
        meaning = map_business_status(BusinessStage.VERIFY, 205)
        assert meaning is not None
        first: BookingApiError | BusinessApiError = BookingApiError.from_meaning(meaning)
    else:
        first = BusinessApiError(code=code, message="Service temporarily unavailable", retryable=True)
    verified = offer(100, price_status="verified", identifier=None)
    service, adapter, _, _, _, offer_id = make_service(
        tmp_path,
        outcomes=[first, response_for(verified)],
    )

    result = service.verify(offer_id)

    assert result.code == "OFFER_VERIFIED"
    assert len(adapter.calls) == 2


def test_repeat_transient_failure_stops_after_second_attempt(tmp_path: Path) -> None:
    failures = [
        BusinessApiError(
            code="SERVICE_TEMPORARILY_UNAVAILABLE",
            message="Service temporarily unavailable",
            retryable=True,
        )
        for _ in range(2)
    ]
    service, adapter, _, _, _, offer_id = make_service(tmp_path, outcomes=failures)

    result = service.verify(offer_id)

    assert result.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert result.status is CommandStatus.RETRYABLE_ERROR
    assert len(adapter.calls) == 2


def test_fr_offer_defense_rejects_bypassed_state_before_verify(tmp_path: Path) -> None:
    searched = offer(100, segments=[segment(carrier="FR")])
    service, adapter, _, _, _, offer_id = make_service(tmp_path, searched=searched)

    result = service.verify(offer_id)

    assert result.code == "OFFER_EXPIRED"
    assert adapter.calls == []


def test_currency_mismatch_is_invalid_without_persisting_a_context(tmp_path: Path) -> None:
    service, _, _, _, booking_store, offer_id = make_service(
        tmp_path,
        searched=offer(100, currency="USD"),
        verified=offer(100, currency="EUR", price_status="verified", identifier=None),
    )

    result = service.verify(offer_id)

    assert result.code == "SERVICE_RESPONSE_INVALID"
    assert not booking_store.contexts_file.exists()


def test_public_verify_data_is_allowlisted_and_uses_opaque_slots(tmp_path: Path) -> None:
    itinerary = [segment(flight_number="AK701"), segment(flight_number="AK702")]
    itinerary.append(segment(direction="inbound", flight_number="AK703"))
    service, _, _, _, _, offer_id = make_service(
        tmp_path,
        searched=offer(100, segments=itinerary),
        verified=offer(100, price_status="verified", identifier=None, segments=itinerary),
        saved_request=request(children=1, infants=1, return_date="2026-08-12"),
    )

    result = service.verify(offer_id)

    assert result.data["travelers"] == [
        {"traveler_id": result.data["travelers"][0]["traveler_id"], "passenger_type": "adult"},
        {"traveler_id": result.data["travelers"][1]["traveler_id"], "passenger_type": "child"},
        {"traveler_id": result.data["travelers"][2]["traveler_id"], "passenger_type": "infant"},
    ]
    assert [item["segment_index"] for item in result.data["segments"]] == [1, 2, 3]
    assert [item["direction"] for item in result.data["segments"]] == ["outbound", "outbound", "inbound"]
    assert all(str(item["traveler_id"]).startswith("trav_") for item in result.data["travelers"])
    assert all(str(item["segment_id"]).startswith("seg_") for item in result.data["segments"])
    serialized = json.dumps(result.model_dump(mode="json"))
    for private in (
        "private-routing-token",
        "private-session",
        "search_id",
        "offer_id",
        "route_generation",
        "upstream_identifier",
        "routingIdentifier",
        "sessionId",
        "business.example.invalid",
    ):
        assert private not in serialized


def raw_routing(*, ancillary_supported: list[str] | None = None, carrier: str = "AK") -> dict[str, object]:
    value: dict[str, object] = {
        "currency": "USD",
        "adultPrice": 75,
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
        value["ancillarySupported"] = ancillary_supported
    return value


def raw_requirements(**required: bool) -> dict[str, object]:
    fields = (
        "name",
        "passengerType",
        "gender",
        "birthday",
        "cardType",
        "cardNum",
        "cardIssuePlace",
        "cardExpired",
        "nationality",
    )
    return {"passenger": {field: {"required": required.get(field, False)} for field in fields}}


@dataclass
class FakeBusiness:
    response: BusinessResponse
    calls: list[tuple[BusinessRoute, ApiCredential, dict[str, object]]] = field(default_factory=list)

    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse:
        self.calls.append((route, credential, payload))
        return self.response


def adapter_response(**data: object) -> BusinessResponse:
    return BusinessResponse(status=0, msg="private success text", request_id="verify-request", data=data)


def test_verify_adapter_uses_status_and_minimal_routing_payload() -> None:
    business = FakeBusiness(
        adapter_response(
            sessionId="private-session",
            routing=raw_routing(),
            bookingRequirement=raw_requirements(),
        )
    )
    adapter = VerifyAdapter(business, RoutingNormalizer())
    access = transaction_access()

    result = adapter.verify(
        access.route,
        access.credential,
        routing_identifier="private-routing-token",
        request=request(),
    )

    assert result.session_id == "private-session"
    assert result.request_id == "verify-request"
    assert business.calls[0][2] == {"routingIdentifier": "private-routing-token"}


def test_verify_adapter_rejects_missing_session_without_echoing_private_value() -> None:
    rejected = {"nested": "private-session-value"}
    business = FakeBusiness(
        adapter_response(
            sessionId=rejected,
            routing=raw_routing(),
            bookingRequirement=raw_requirements(),
        )
    )

    with pytest.raises(BookingApiError) as raised:
        VerifyAdapter(business, RoutingNormalizer()).verify(
            transaction_access().route,
            transaction_access().credential,
            routing_identifier="private-routing-token",
            request=request(),
        )

    assert raised.value.code == "SERVICE_RESPONSE_INVALID"
    assert "private-session-value" not in f"{raised.value} {raised.value!r}"


def test_missing_capability_declaration_is_not_assumed_supported() -> None:
    business = FakeBusiness(
        adapter_response(
            sessionId="private-session",
            routing=raw_routing(),
            bookingRequirement=raw_requirements(),
        )
    )

    result = VerifyAdapter(business, RoutingNormalizer()).verify(
        transaction_access().route,
        transaction_access().credential,
        routing_identifier="private-routing-token",
        request=request(),
    )

    assert result.ancillary_supported == ()


def test_requirements_normalization_uses_exact_allowlisted_names() -> None:
    normalized = normalize_requirements(
        raw_requirements(birthday=True, cardNum=True, cardIssuePlace=True, nationality=True)
    )

    assert normalized.required_fields == (
        "name",
        "passenger_type",
        "gender",
        "birthday",
        "document.number",
        "document.issuing_country",
        "nationality",
    )


def test_requirements_reject_missing_constraint_declarations() -> None:
    malformed = raw_requirements()
    del malformed["passenger"]["cardNum"]

    with pytest.raises(BookingApiError) as raised:
        normalize_requirements(malformed)

    assert raised.value.code == "SERVICE_RESPONSE_INVALID"


def test_verify_adapter_fr_response_becomes_neutral_offer_expiry() -> None:
    business = FakeBusiness(
        adapter_response(
            sessionId="private-session",
            routing=raw_routing(carrier="FR"),
            bookingRequirement=raw_requirements(),
        )
    )

    with pytest.raises(BookingApiError) as raised:
        VerifyAdapter(business, RoutingNormalizer()).verify(
            transaction_access().route,
            transaction_access().credential,
            routing_identifier="private-routing-token",
            request=request(),
        )

    assert raised.value.code == "OFFER_EXPIRED"


def test_verify_adapter_maps_status_without_branching_on_msg() -> None:
    response = BusinessResponse(
        status=205,
        msg="everything succeeded",
        request_id="verify-request",
        data={"private": "response"},
    )

    with pytest.raises(BookingApiError) as raised:
        VerifyAdapter(FakeBusiness(response), RoutingNormalizer()).verify(
            transaction_access().route,
            transaction_access().credential,
            routing_identifier="private-routing-token",
            request=request(),
        )

    assert raised.value.code == "PRICE_VERIFICATION_UNAVAILABLE"
    assert raised.value.request_id == "verify-request"
