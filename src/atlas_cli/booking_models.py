"""Immutable, PII-free booking workflow state models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atlas_cli.search_models import NormalizedOffer, NormalizedSegment

_INVALID_MASK = "<invalid-mask>"
_INVALID_EXTRA = "<invalid-extra>"
_MASKED_NAME = re.compile(r"[^\s*/]\*{3}(?:/[^\s*/]\*{3})?")
_MASKED_DOCUMENT = re.compile(r"\*{4}[A-Z0-9]{4}")


class BookingModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def sanitize_extra_values(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        return {key: item if key in cls.model_fields else _INVALID_EXTRA for key, item in value.items()}


class AncillaryKind(StrEnum):
    BAGGAGE = "baggage"
    SEAT = "seat"


RequirementField = Literal[
    "name",
    "passenger_type",
    "gender",
    "birthday",
    "document.type",
    "document.number",
    "document.issuing_country",
    "document.expires",
    "nationality",
]


class BookingRequirements(BookingModel):
    required_fields: tuple[RequirementField, ...]


class TravelerSlot(BookingModel):
    traveler_id: str
    passenger_type: Literal["adult", "child", "infant"]


class SegmentSlot(BookingModel):
    segment_id: str
    segment_index: int
    direction: Literal["outbound", "inbound"]
    segment: NormalizedSegment


class BaggageOption(BookingModel):
    baggage_id: str
    product_code: str = Field(repr=False)
    segment_id: str
    segment_index: int
    piece: int
    weight_kg: int
    size: str | None = None
    category: str
    price: float
    currency: str


class SeatOption(BookingModel):
    seat_id: str
    product_code: str = Field(repr=False)
    segment_id: str
    segment_index: int
    row: int
    column: str
    characteristics: tuple[str, ...] = ()
    price: float
    currency: str


class AncillarySelection(BookingModel):
    kind: AncillaryKind
    traveler_id: str
    segment_id: str
    option_id: str
    product_code: str = Field(repr=False)
    segment_index: int


class MaskedPassengerSummary(BookingModel):
    traveler_id: str
    name: str = Field(repr=False, pattern=_MASKED_NAME.pattern)
    document: str | None = Field(default=None, repr=False, pattern=_MASKED_DOCUMENT.pattern)

    @field_validator("name", mode="before")
    @classmethod
    def sanitize_unmasked_name(cls, value: object) -> object:
        if isinstance(value, str) and _MASKED_NAME.fullmatch(value) is not None:
            return value
        return _INVALID_MASK

    @field_validator("document", mode="before")
    @classmethod
    def sanitize_unmasked_document(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and _MASKED_DOCUMENT.fullmatch(value) is not None):
            return value
        return _INVALID_MASK


class SelectedAncillarySummary(BookingModel):
    kind: AncillaryKind
    traveler_id: str
    segment_id: str
    description: str
    price: float
    currency: str


class PaymentSummary(BookingModel):
    ticket_price: float
    baggage_total: float
    seat_total: float
    total_price: float
    currency: str
    passengers: tuple[MaskedPassengerSummary, ...]
    ancillaries: tuple[SelectedAncillarySummary, ...] = ()
    price_change: Literal["unchanged", "decreased", "increased"] = "unchanged"
    previous_offer_total: float | None = None
    current_offer_total: float | None = None


class PaymentState(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    UNAVAILABLE = "unavailable"
    PAYING = "paying"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    UNKNOWN = "unknown"
    PAID = "paid"


class OrderAttemptState(StrEnum):
    READY = "ready"
    CREATING = "creating"
    CREATED = "created"
    UNKNOWN = "unknown"


class TicketingState(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    TICKETED = "ticketed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class VerifiedBookingSeed(BookingModel):
    search_id: str
    offer_id: str
    route_generation: str = Field(repr=False)
    routing_identifier: str = Field(repr=False)
    session_id: str = Field(repr=False)
    searched_offer: NormalizedOffer
    verified_offer: NormalizedOffer
    requirements: BookingRequirements
    travelers: tuple[TravelerSlot, ...]
    segments: tuple[SegmentSlot, ...]
    expires_at: datetime


class OrderState(BookingModel):
    order_no: str
    order_url: str
    total_price: float
    transaction_fee: float
    currency: str
    payment_deadline: datetime
    summary: PaymentSummary
    summary_digest: str
    payment_state: PaymentState
    ticketing_state: TicketingState = TicketingState.NOT_STARTED
    airline_pnrs: tuple[str, ...] = ()
    ticket_numbers: tuple[str, ...] = ()


class PaymentConfirmationSeed(BookingModel):
    order_no: str
    summary_digest: str
    expires_at: datetime


class PaymentConfirmation(BookingModel):
    confirmation_id: str
    order_no: str
    summary_digest: str
    expires_at: datetime
    consumed_at: datetime | None = None


def segment_to_upstream(slot: SegmentSlot) -> dict[str, object]:
    segment = slot.segment
    payload: dict[str, object] = {
        "segmentIndex": slot.segment_index,
        "carrier": segment.carrier,
        "flightNumber": segment.flight_number,
        "depAirport": segment.departure_airport,
        "arrAirport": segment.arrival_airport,
        "depTime": segment.departure_time,
        "arrTime": segment.arrival_time,
    }
    if segment.cabin_class is not None:
        payload["cabinClass"] = segment.cabin_class
    return payload


class BookingContext(BookingModel):
    booking_id: str
    search_id: str
    offer_id: str
    route_generation: str = Field(repr=False)
    secret_ref: str
    secret_revision: str
    session_id: str | None = Field(default=None, repr=False)
    searched_offer: NormalizedOffer
    verified_offer: NormalizedOffer
    price_change: Literal["unchanged", "decreased", "increased"]
    increased_price_confirmed: bool = False
    requirements: BookingRequirements
    travelers: tuple[TravelerSlot, ...]
    segments: tuple[SegmentSlot, ...]
    baggage_supported: bool
    seat_supported: bool
    baggage_options: tuple[BaggageOption, ...] = ()
    seat_options: tuple[SeatOption, ...] = ()
    selections: tuple[AncillarySelection, ...] = ()
    order_attempt_state: OrderAttemptState = OrderAttemptState.READY
    order: OrderState | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    def most_significant_carrier(self) -> str:
        return self.segments[0].segment.carrier

    def segment_payloads(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        outbound = [segment_to_upstream(item) for item in self.segments if item.direction == "outbound"]
        inbound = [segment_to_upstream(item) for item in self.segments if item.direction == "inbound"]
        return outbound, inbound


class BookingState(BookingModel):
    schema_version: Literal["1"] = "1"
    contexts: tuple[BookingContext, ...] = ()
    confirmations: tuple[PaymentConfirmation, ...] = ()
