"""Secret-free persisted projections for Atlas booking workflow state."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BaggageOption,
    BookingContext,
    BookingModel,
    BookingRequirements,
    BookingState,
    OrderAttemptState,
    OrderState,
    PaymentConfirmation,
    SeatOption,
    SegmentSlot,
    TravelerSlot,
)
from atlas_cli.search_models import NormalizedOffer, NormalizedPassengerPrice, NormalizedSegment
from atlas_cli.secure_store import BookingSecrets


class BookingProjectionError(RuntimeError):
    """Raised when public and secure booking records are not exactly bound."""


class PersistedNormalizedOffer(BookingModel):
    currency: str
    total_price: float
    transaction_fee_total: float
    passenger_prices: list[NormalizedPassengerPrice]
    segments: list[NormalizedSegment]
    ancillary_supported: tuple[Literal["baggage", "seat"], ...] = ()
    bookable: bool
    price_status: Literal["reference", "current", "verified"]
    refresh_time: str | None = None
    expire_time: str | None = None

    @classmethod
    def from_domain(cls, offer: NormalizedOffer) -> PersistedNormalizedOffer:
        return cls(
            currency=offer.currency,
            total_price=offer.total_price,
            transaction_fee_total=offer.transaction_fee_total,
            passenger_prices=offer.passenger_prices,
            segments=offer.segments,
            ancillary_supported=offer.ancillary_supported,
            bookable=offer.bookable,
            price_status=offer.price_status,
            refresh_time=offer.refresh_time,
            expire_time=offer.expire_time,
        )

    def to_domain(self) -> NormalizedOffer:
        return NormalizedOffer(
            upstream_identifier=None,
            currency=self.currency,
            total_price=self.total_price,
            transaction_fee_total=self.transaction_fee_total,
            passenger_prices=self.passenger_prices,
            segments=self.segments,
            ancillary_supported=self.ancillary_supported,
            bookable=self.bookable,
            price_status=self.price_status,
            refresh_time=self.refresh_time,
            expire_time=self.expire_time,
        )


class PersistedBaggageOption(BookingModel):
    baggage_id: str
    segment_id: str
    segment_index: int
    piece: int
    weight_kg: int
    size: str | None = None
    category: str
    price: float
    currency: str

    @classmethod
    def from_domain(cls, option: BaggageOption) -> PersistedBaggageOption:
        return cls(
            baggage_id=option.baggage_id,
            segment_id=option.segment_id,
            segment_index=option.segment_index,
            piece=option.piece,
            weight_kg=option.weight_kg,
            size=option.size,
            category=option.category,
            price=option.price,
            currency=option.currency,
        )

    def to_domain(self, product_code: str) -> BaggageOption:
        return BaggageOption(**self.model_dump(), product_code=product_code)


class PersistedSeatOption(BookingModel):
    seat_id: str
    segment_id: str
    segment_index: int
    row: int
    column: str
    characteristics: tuple[str, ...] = ()
    price: float
    currency: str

    @classmethod
    def from_domain(cls, option: SeatOption) -> PersistedSeatOption:
        return cls(
            seat_id=option.seat_id,
            segment_id=option.segment_id,
            segment_index=option.segment_index,
            row=option.row,
            column=option.column,
            characteristics=option.characteristics,
            price=option.price,
            currency=option.currency,
        )

    def to_domain(self, product_code: str) -> SeatOption:
        return SeatOption(**self.model_dump(), product_code=product_code)


class PersistedSelection(BookingModel):
    kind: AncillaryKind
    traveler_id: str
    segment_id: str
    option_id: str
    segment_index: int

    @classmethod
    def from_domain(cls, selection: AncillarySelection) -> PersistedSelection:
        return cls(
            kind=selection.kind,
            traveler_id=selection.traveler_id,
            segment_id=selection.segment_id,
            option_id=selection.option_id,
            segment_index=selection.segment_index,
        )

    def to_domain(self, product_code: str) -> AncillarySelection:
        return AncillarySelection(**self.model_dump(), product_code=product_code)


class PersistedBookingContext(BookingModel):
    booking_id: str
    search_id: str
    offer_id: str
    route_generation: str
    secret_ref: str
    secret_revision: str
    searched_offer: PersistedNormalizedOffer
    verified_offer: PersistedNormalizedOffer
    price_change: Literal["unchanged", "decreased", "increased"]
    increased_price_confirmed: bool = False
    requirements: BookingRequirements
    travelers: tuple[TravelerSlot, ...]
    segments: tuple[SegmentSlot, ...]
    baggage_supported: bool
    seat_supported: bool
    baggage_options: tuple[PersistedBaggageOption, ...] = ()
    seat_options: tuple[PersistedSeatOption, ...] = ()
    selections: tuple[PersistedSelection, ...] = ()
    order_attempt_state: OrderAttemptState = OrderAttemptState.READY
    order: OrderState | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    def is_terminal(self) -> bool:
        return (
            self.order is not None
            or self.order_attempt_state
            in {
                OrderAttemptState.CREATED,
                OrderAttemptState.UNKNOWN,
            }
            or self.expires_at <= self.updated_at
        )


class PersistedBookingState(BookingModel):
    schema_version: Literal["2"] = "2"
    contexts: tuple[PersistedBookingContext, ...] = ()
    confirmations: tuple[PaymentConfirmation, ...] = ()

    @classmethod
    def from_domain(cls, state: BookingState) -> PersistedBookingState:
        return cls(
            contexts=tuple(project_booking_context(context) for context in state.contexts),
            confirmations=state.confirmations,
        )


def project_booking_context(context: BookingContext) -> PersistedBookingContext:
    return PersistedBookingContext(
        booking_id=context.booking_id,
        search_id=context.search_id,
        offer_id=context.offer_id,
        route_generation=context.route_generation,
        secret_ref=context.secret_ref,
        secret_revision=context.secret_revision,
        searched_offer=PersistedNormalizedOffer.from_domain(context.searched_offer),
        verified_offer=PersistedNormalizedOffer.from_domain(context.verified_offer),
        price_change=context.price_change,
        increased_price_confirmed=context.increased_price_confirmed,
        requirements=context.requirements,
        travelers=context.travelers,
        segments=context.segments,
        baggage_supported=context.baggage_supported,
        seat_supported=context.seat_supported,
        baggage_options=tuple(PersistedBaggageOption.from_domain(item) for item in context.baggage_options),
        seat_options=tuple(PersistedSeatOption.from_domain(item) for item in context.seat_options),
        selections=tuple(PersistedSelection.from_domain(item) for item in context.selections),
        order_attempt_state=context.order_attempt_state,
        order=context.order,
        created_at=context.created_at,
        updated_at=context.updated_at,
        expires_at=context.expires_at,
    )


def hydrate_booking_context(
    context: PersistedBookingContext,
    secrets: BookingSecrets,
) -> BookingContext:
    option_ids = {item.baggage_id for item in context.baggage_options} | {item.seat_id for item in context.seat_options}
    if (
        context.is_terminal()
        or secrets.booking_id != context.booking_id
        or secrets.generation != context.route_generation
        or secrets.revision != context.secret_revision
        or set(secrets.products) != option_ids
        or not secrets.session_id
        or any(not value for value in secrets.products.values())
        or any(selection.option_id not in option_ids for selection in context.selections)
    ):
        raise BookingProjectionError("Saved booking secret binding is invalid")
    return BookingContext(
        booking_id=context.booking_id,
        search_id=context.search_id,
        offer_id=context.offer_id,
        route_generation=context.route_generation,
        secret_ref=context.secret_ref,
        secret_revision=context.secret_revision,
        session_id=secrets.session_id,
        searched_offer=context.searched_offer.to_domain(),
        verified_offer=context.verified_offer.to_domain(),
        price_change=context.price_change,
        increased_price_confirmed=context.increased_price_confirmed,
        requirements=context.requirements,
        travelers=context.travelers,
        segments=context.segments,
        baggage_supported=context.baggage_supported,
        seat_supported=context.seat_supported,
        baggage_options=tuple(item.to_domain(secrets.products[item.baggage_id]) for item in context.baggage_options),
        seat_options=tuple(item.to_domain(secrets.products[item.seat_id]) for item in context.seat_options),
        selections=tuple(item.to_domain(secrets.products[item.option_id]) for item in context.selections),
        order_attempt_state=context.order_attempt_state,
        order=context.order,
        created_at=context.created_at,
        updated_at=context.updated_at,
        expires_at=context.expires_at,
    )


def restore_terminal_booking_context(context: PersistedBookingContext) -> BookingContext:
    """Restore a terminal public context without consulting secure storage."""
    if not context.is_terminal() or context.baggage_options or context.seat_options or context.selections:
        raise BookingProjectionError("Saved booking terminal state is invalid")
    return BookingContext(
        booking_id=context.booking_id,
        search_id=context.search_id,
        offer_id=context.offer_id,
        route_generation=context.route_generation,
        secret_ref=context.secret_ref,
        secret_revision=context.secret_revision,
        session_id=None,
        searched_offer=context.searched_offer.to_domain(),
        verified_offer=context.verified_offer.to_domain(),
        price_change=context.price_change,
        increased_price_confirmed=context.increased_price_confirmed,
        requirements=context.requirements,
        travelers=context.travelers,
        segments=context.segments,
        baggage_supported=context.baggage_supported,
        seat_supported=context.seat_supported,
        order_attempt_state=context.order_attempt_state,
        order=context.order,
        created_at=context.created_at,
        updated_at=context.updated_at,
        expires_at=context.expires_at,
    )
