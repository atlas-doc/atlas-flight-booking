"""Offer verification and price-confirmation service boundaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from atlas_cli.access import AccessManager, AccessManagerError, TransactionAccess
from atlas_cli.api_client import ApiClientError
from atlas_cli.booking_models import (
    BookingContext,
    BookingRequirements,
    RequirementField,
    SegmentSlot,
    TravelerSlot,
    VerifiedBookingSeed,
)
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.business_client import AtlasBusinessClient, BusinessApiError, BusinessResponse
from atlas_cli.business_status import (
    BookingApiError,
    BusinessStage,
    StatusMeaning,
    booking_error_result,
    map_business_status,
)
from atlas_cli.endpoints import BusinessOperation, BusinessRoute
from atlas_cli.models import CommandResult, CommandStatus, action_required_result, success_result
from atlas_cli.routing_normalizer import RoutingNormalizer, RoutingRejected
from atlas_cli.search_models import NormalizedOffer, SearchRequest
from atlas_cli.search_store import SearchStore, SearchStoreError, StoredOffer, StoredSearch
from atlas_cli.secure_store import ApiCredential, Credentials, SecureStoreError

REQUIREMENT_FIELDS: dict[str, RequirementField] = {
    "name": "name",
    "passengerType": "passenger_type",
    "gender": "gender",
    "birthday": "birthday",
    "cardType": "document.type",
    "cardNum": "document.number",
    "cardIssuePlace": "document.issuing_country",
    "cardExpired": "document.expires",
    "nationality": "nationality",
}

_TRANSIENT_VERIFY_CODES = {
    "PRICE_VERIFICATION_UNAVAILABLE",
    "SERVICE_TEMPORARILY_UNAVAILABLE",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _new_token() -> str:
    return token_hex(12)


class VerifiedResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(repr=False)
    verified_offer: NormalizedOffer
    requirements: BookingRequirements
    ancillary_supported: tuple[Literal["baggage", "seat"], ...]
    request_id: str | None = None


def normalize_requirements(value: object) -> BookingRequirements:
    if not isinstance(value, dict) or not isinstance(value.get("passenger"), dict):
        raise BookingApiError.invalid_response()
    passenger = value["passenger"]
    required: set[RequirementField] = {"name", "passenger_type", "gender"}
    for upstream_key, public_key in REQUIREMENT_FIELDS.items():
        constraint = passenger.get(upstream_key)
        if not isinstance(constraint, dict) or not isinstance(constraint.get("required"), bool):
            raise BookingApiError.invalid_response()
        if constraint["required"]:
            required.add(public_key)
    ordered = tuple(field for field in REQUIREMENT_FIELDS.values() if field in required)
    return BookingRequirements(required_fields=ordered)


def required_private_string(
    data: dict[str, object],
    key: str,
    request_id: str | None,
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BookingApiError.invalid_response(request_id)
    return value


class BusinessClient(Protocol):
    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse: ...


class VerifyAdapter:
    def __init__(
        self,
        business: BusinessClient | AtlasBusinessClient,
        normalizer: RoutingNormalizer,
    ) -> None:
        self._business = business
        self._normalizer = normalizer

    def verify(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        *,
        routing_identifier: str,
        request: SearchRequest,
    ) -> VerifiedResponse:
        response = self._business.post(route, credential, {"routingIdentifier": routing_identifier})
        meaning = map_business_status(BusinessStage.VERIFY, response.status)
        if meaning is not None:
            raise BookingApiError.from_meaning(
                meaning,
                request_id=response.request_id,
                upstream_status=response.status,
            )
        session_id = required_private_string(response.data, "sessionId", response.request_id)
        try:
            verified_offer = self._normalizer.normalize(
                response.data.get("routing"),
                request,
                bookable=True,
                price_status="verified",
                request_id=response.request_id,
                require_routing_identifier=False,
            )
        except RoutingRejected:
            raise BookingApiError(
                StatusMeaning(
                    "OFFER_EXPIRED",
                    "Offer expired",
                    CommandStatus.TERMINAL_ERROR,
                ),
                request_id=response.request_id,
            ) from None
        except ValueError:
            raise BookingApiError.invalid_response(response.request_id) from None
        return VerifiedResponse(
            session_id=session_id,
            verified_offer=verified_offer,
            requirements=normalize_requirements(response.data.get("bookingRequirement")),
            ancillary_supported=verified_offer.ancillary_supported,
            request_id=response.request_id,
        )


class ControlCredentialStore(Protocol):
    def load_credentials(self) -> Credentials | None: ...


class TransactionAccessResolver(Protocol):
    def resolve_transaction_access(
        self,
        jwt: str,
        operation: BusinessOperation,
    ) -> TransactionAccess: ...


class VerifyGateway(Protocol):
    def verify(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        *,
        routing_identifier: str,
        request: SearchRequest,
    ) -> VerifiedResponse: ...


class SearchOfferStore(Protocol):
    def load_offer(
        self,
        offer_id: str,
        *,
        generation: str,
        max_age: timedelta = timedelta(hours=6),
    ) -> tuple[StoredSearch, StoredOffer]: ...


class BookingContextStore(Protocol):
    def create_from_verified(self, seed: VerifiedBookingSeed) -> BookingContext: ...

    def confirm_price(self, booking_id: str, *, generation: str) -> BookingContext: ...


class VerifyService:
    def __init__(
        self,
        *,
        secrets: ControlCredentialStore,
        access: TransactionAccessResolver | AccessManager,
        adapter: VerifyGateway | VerifyAdapter,
        search_store: SearchOfferStore | SearchStore,
        booking_store: BookingContextStore | BookingStore,
        now: Callable[[], datetime] = _now,
        token_factory: Callable[[], str] = _new_token,
    ) -> None:
        self._secrets = secrets
        self._access = access
        self._adapter = adapter
        self._search_store = search_store
        self._booking_store = booking_store
        self._now = now
        self._token_factory = token_factory

    def verify(self, offer_id: str) -> CommandResult:
        try:
            credentials = self._secrets.load_credentials()
            if credentials is None or not credentials.jwt.strip():
                return self._authorization_required()
            access = self._access.resolve_transaction_access(credentials.jwt, BusinessOperation.VERIFY)
            stored_search, stored_offer = self._search_store.load_offer(
                offer_id,
                generation=access.route.generation,
                max_age=timedelta(hours=6),
            )
            routing_identifier = self._routing_identifier(stored_offer.offer)
            verified = self._verify_with_retry(
                access,
                routing_identifier=routing_identifier,
                request=stored_search.request,
            )
            if verified.verified_offer.currency != stored_offer.offer.currency:
                raise BookingApiError.invalid_response(verified.request_id)
            context = self._booking_store.create_from_verified(
                VerifiedBookingSeed(
                    search_id=stored_search.search_id,
                    offer_id=stored_offer.offer_id,
                    route_generation=access.route.generation,
                    routing_identifier=routing_identifier,
                    session_id=verified.session_id,
                    searched_offer=stored_offer.offer,
                    verified_offer=verified.verified_offer,
                    requirements=verified.requirements,
                    travelers=self._traveler_slots(stored_search.request),
                    segments=self._segment_slots(verified.verified_offer),
                    expires_at=self._now() + timedelta(hours=2),
                )
            )
        except (
            AccessManagerError,
            ApiClientError,
            BookingApiError,
            BookingStoreError,
            BusinessApiError,
            SearchStoreError,
            SecureStoreError,
        ) as error:
            return self._error_result(error)

        data = self._public_context(context)
        if context.price_change == "increased":
            return action_required_result(
                "PRICE_CONFIRMATION_REQUIRED",
                "Price confirmation required",
                request_id=verified.request_id,
                data=data,
            )
        return success_result(
            "OFFER_VERIFIED",
            "Offer verified",
            request_id=verified.request_id,
            data=data,
        )

    def confirm_price(self, booking_id: str) -> CommandResult:
        try:
            credentials = self._secrets.load_credentials()
            if credentials is None or not credentials.jwt.strip():
                return self._authorization_required()
            access = self._access.resolve_transaction_access(credentials.jwt, BusinessOperation.VERIFY)
            context = self._booking_store.confirm_price(
                booking_id,
                generation=access.route.generation,
            )
        except (
            AccessManagerError,
            ApiClientError,
            BookingApiError,
            BookingStoreError,
            BusinessApiError,
            SearchStoreError,
            SecureStoreError,
        ) as error:
            return self._error_result(error)
        return success_result(
            "PRICE_CONFIRMED",
            "Price confirmed",
            data=self._public_context(context),
        )

    def _verify_with_retry(
        self,
        access: TransactionAccess,
        *,
        routing_identifier: str,
        request: SearchRequest,
    ) -> VerifiedResponse:
        try:
            return self._adapter.verify(
                access.route,
                access.credential,
                routing_identifier=routing_identifier,
                request=request,
            )
        except (BookingApiError, BusinessApiError) as error:
            if getattr(error, "code", None) not in _TRANSIENT_VERIFY_CODES:
                raise
        return self._adapter.verify(
            access.route,
            access.credential,
            routing_identifier=routing_identifier,
            request=request,
        )

    @staticmethod
    def _routing_identifier(offer: NormalizedOffer) -> str:
        if offer.price_status == "reference" or not offer.bookable:
            raise AccessManagerError(
                code="SUBSCRIPTION_REQUIRED",
                message="Subscription required",
            )
        if any(item.carrier == "FR" or item.operating_carrier == "FR" for item in offer.segments):
            raise SearchStoreError(code="OFFER_EXPIRED", message="Offer expired; search again")
        value = offer.upstream_identifier
        if not isinstance(value, str) or not value:
            raise SearchStoreError(code="OFFER_EXPIRED", message="Offer expired; search again")
        return value

    def _traveler_slots(self, request: SearchRequest) -> tuple[TravelerSlot, ...]:
        slots: list[TravelerSlot] = []
        counts: tuple[tuple[Literal["adult", "child", "infant"], int], ...] = (
            ("adult", request.adults),
            ("child", request.children),
            ("infant", request.infants),
        )
        for passenger_type, count in counts:
            for _ in range(count):
                slots.append(
                    TravelerSlot(
                        traveler_id=f"trav_{self._token_factory()}",
                        passenger_type=passenger_type,
                    )
                )
        return tuple(slots)

    def _segment_slots(self, offer: NormalizedOffer) -> tuple[SegmentSlot, ...]:
        slots: list[SegmentSlot] = []
        for segment_index, item in enumerate(offer.segments, start=1):
            slots.append(
                SegmentSlot(
                    segment_id=f"seg_{self._token_factory()}",
                    segment_index=segment_index,
                    direction=item.direction,
                    segment=item,
                )
            )
        return tuple(slots)

    @staticmethod
    def _public_context(context: BookingContext) -> dict[str, object]:
        segments: list[dict[str, object]] = []
        for slot in context.segments:
            segments.append(
                {
                    "segment_id": slot.segment_id,
                    "segment_index": slot.segment_index,
                    **slot.segment.model_dump(mode="json"),
                }
            )
        return {
            "booking_id": context.booking_id,
            "previous_price": context.searched_offer.total_price,
            "current_price": context.verified_offer.total_price,
            "currency": context.verified_offer.currency,
            "price_change": context.price_change,
            "requirements": context.requirements.model_dump(mode="json"),
            "travelers": [item.model_dump(mode="json") for item in context.travelers],
            "segments": segments,
            "baggage_supported": context.baggage_supported,
            "seat_supported": context.seat_supported,
        }

    @staticmethod
    def _authorization_required() -> CommandResult:
        return booking_error_result(
            AccessManagerError(
                code="AUTHORIZATION_REQUIRED",
                message="Authorization required",
            ),
            data={"authenticated": False},
        )

    @staticmethod
    def _error_result(error: Exception) -> CommandResult:
        if isinstance(error, SecureStoreError):
            error = AccessManagerError(
                code="SECURE_STORE_UNAVAILABLE",
                message="Secure credential storage is unavailable",
            )
        details: dict[str, object] = {}
        if isinstance(error, AccessManagerError):
            url = error.details.get("url")
            if isinstance(url, str) and url:
                details["url"] = url
        return booking_error_result(error, details=details)
