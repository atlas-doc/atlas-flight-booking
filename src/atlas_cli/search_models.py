"""Validated flight-search inputs and normalized public search values."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IATA_PATTERN = re.compile(r"^[A-Z]{3}$")


class SearchModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SearchRequest(SearchModel):
    origin: str
    destination: str
    depart: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=8)
    infants: int = Field(default=0, ge=0, le=9)
    airlines: tuple[str, ...] = ()
    currency: str | None = None
    include_multiple_fare_families: bool = False

    @field_validator("origin", "destination", "currency", mode="before")
    @classmethod
    def normalize_code(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("origin", "destination")
    @classmethod
    def validate_iata(cls, value: str) -> str:
        if IATA_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a three-letter IATA code")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is not None and IATA_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a three-letter currency code")
        return value

    @field_validator("airlines", mode="before")
    @classmethod
    def normalize_airlines(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(item.strip().upper() if isinstance(item, str) else item for item in value)
        return value

    @field_validator("airlines")
    @classmethod
    def validate_airlines(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"^[A-Z0-9]{2,3}$", item) is None for item in value):
            raise ValueError("must contain valid airline codes")
        return value

    @model_validator(mode="after")
    def validate_passengers_and_trip(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.adults + self.children > 9:
            raise ValueError("adult and child total cannot exceed 9")
        if self.infants > self.adults:
            raise ValueError("infants cannot exceed adults")
        if self.return_date is not None and self.return_date < self.depart:
            raise ValueError("return date cannot be before departure")
        return self

    def to_upstream_payload(self, request_id: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "tripType": "2" if self.return_date is not None else "1",
            "requestId": request_id,
            "adultNum": self.adults,
            "childNum": self.children,
            "infantNum": self.infants,
            "fromCity": self.origin,
            "toCity": self.destination,
            "fromDate": self.depart.strftime("%Y%m%d"),
        }
        if self.return_date is not None:
            payload["retDate"] = self.return_date.strftime("%Y%m%d")
        if self.airlines:
            payload["airlines"] = list(self.airlines)
        if self.currency is not None:
            payload["currency"] = self.currency
        payload["includeMultipleFareFamily"] = self.include_multiple_fare_families
        return payload


class NormalizedSegment(SearchModel):
    departure_airport: str
    arrival_airport: str
    departure_time: str
    arrival_time: str
    carrier: str
    operating_carrier: str | None = None
    flight_number: str
    duration_minutes: int = Field(ge=0)
    cabin_class: int | None = None
    direction: Literal["outbound", "inbound"] = "outbound"


class NormalizedPassengerPrice(SearchModel):
    passenger_type: Literal["adult", "child", "infant"]
    count: int = Field(ge=1)
    base_fare_per_passenger: float = Field(ge=0)
    tax_per_passenger: float = Field(ge=0)
    subtotal: float = Field(ge=0)


class NormalizedOffer(SearchModel):
    upstream_identifier: str | None = Field(default=None, repr=False)
    currency: str
    total_price: float = Field(ge=0)
    transaction_fee_total: float = Field(ge=0)
    passenger_prices: list[NormalizedPassengerPrice]
    segments: list[NormalizedSegment]
    ancillary_supported: tuple[Literal["baggage", "seat"], ...] = ()
    bookable: bool
    price_status: Literal["reference", "current", "verified"]
    refresh_time: str | None = None
    expire_time: str | None = None


class NormalizedSearch(SearchModel):
    offers: list[NormalizedOffer]
    reason: Literal["route_not_supported", "no_flight", "sold_out"] | None = None
    recent_flight_dates: list[str] = Field(default_factory=list)
    request_id: str | None = None
