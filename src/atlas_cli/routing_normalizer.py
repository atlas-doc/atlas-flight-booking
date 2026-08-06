"""Normalize upstream routing values into the bounded offer contract."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal, NoReturn, cast

from atlas_cli.search_models import (
    NormalizedOffer,
    NormalizedPassengerPrice,
    NormalizedSegment,
    SearchRequest,
)

AIRLINE_CODE_PATTERN = re.compile(r"^[A-Z0-9]{2,3}$")


class RoutingRejected(RuntimeError):
    """A routing that must be omitted without exposing upstream data."""


class RoutingNormalizer:
    def normalize(
        self,
        value: object,
        request: SearchRequest,
        *,
        bookable: bool,
        price_status: Literal["reference", "current", "verified"],
        request_id: str | None,
        require_routing_identifier: bool,
    ) -> NormalizedOffer:
        raw = self._require_mapping(value, request_id)
        segments = self._segments(raw, request_id)
        return NormalizedOffer(
            upstream_identifier=self._identifier(raw, request_id, require_routing_identifier),
            currency=self._required_string(raw, "currency", request_id),
            total_price=self._total(raw, request, len(segments), request_id),
            transaction_fee_total=float(self._fee_total(raw, request, len(segments), request_id)),
            passenger_prices=self._passenger_prices(raw, request, request_id),
            segments=segments,
            ancillary_supported=self._ancillary_supported(raw.get("ancillarySupported")),
            bookable=bookable,
            price_status=price_status,
            refresh_time=self._optional_string(raw.get("refreshTime")),
            expire_time=self._optional_string(raw.get("expireTime")),
        )

    @staticmethod
    def _require_mapping(value: object, request_id: str | None) -> dict[str, object]:
        if not isinstance(value, dict):
            RoutingNormalizer._invalid(request_id)
        return cast(dict[str, object], value)

    def _identifier(
        self,
        value: dict[str, object],
        request_id: str | None,
        required: bool,
    ) -> str | None:
        if not required:
            return None
        return self._required_string(value, "routingIdentifier", request_id)

    def _total(
        self,
        value: dict[str, object],
        request: SearchRequest,
        segment_count: int,
        request_id: str | None,
    ) -> float:
        passenger_total = sum(
            (Decimal(str(item.subtotal)) for item in self._passenger_prices(value, request, request_id)),
            Decimal(),
        )
        return float(passenger_total + self._fee_total(value, request, segment_count, request_id))

    def _passenger_prices(
        self,
        value: dict[str, object],
        request: SearchRequest,
        request_id: str | None,
    ) -> list[NormalizedPassengerPrice]:
        definitions: tuple[
            tuple[Literal["adult", "child", "infant"], int, str, str],
            ...,
        ] = (
            ("adult", request.adults, "adultPrice", "adultTax"),
            ("child", request.children, "childPrice", "childTax"),
            ("infant", request.infants, "infantPrice", "infantTax"),
        )
        prices: list[NormalizedPassengerPrice] = []
        for passenger_type, count, fare_key, tax_key in definitions:
            if count == 0:
                continue
            fare = self._amount(value.get(fare_key), request_id)
            tax = self._amount(value.get(tax_key), request_id)
            prices.append(
                NormalizedPassengerPrice(
                    passenger_type=passenger_type,
                    count=count,
                    base_fare_per_passenger=float(fare),
                    tax_per_passenger=float(tax),
                    subtotal=float((fare + tax) * count),
                )
            )
        return prices

    def _segments(self, value: dict[str, object], request_id: str | None) -> list[NormalizedSegment]:
        outbound = value.get("fromSegments")
        inbound = value.get("retSegments", [])
        if not isinstance(outbound, list) or not isinstance(inbound, list) or not outbound:
            self._invalid(request_id)

        segments: list[NormalizedSegment] = []
        directions: tuple[tuple[Literal["outbound", "inbound"], list[object]], ...] = (
            ("outbound", outbound),
            ("inbound", inbound),
        )
        for _, raw_segments in directions:
            for raw_segment in raw_segments:
                self._reject_fr(self._require_mapping(raw_segment, request_id))
        for direction, raw_segments in directions:
            for raw_segment in raw_segments:
                raw = self._require_mapping(raw_segment, request_id)
                duration = raw.get("duration")
                if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
                    self._invalid(request_id)
                cabin_class = raw.get("cabinClass")
                if cabin_class is not None and (not isinstance(cabin_class, int) or isinstance(cabin_class, bool)):
                    self._invalid(request_id)
                segments.append(
                    NormalizedSegment(
                        departure_airport=self._required_string(raw, "depAirport", request_id),
                        arrival_airport=self._required_string(raw, "arrAirport", request_id),
                        departure_time=self._required_string(raw, "depTime", request_id),
                        arrival_time=self._required_string(raw, "arrTime", request_id),
                        carrier=self._required_string(raw, "carrier", request_id),
                        operating_carrier=self._operating_carrier(raw.get("operatingCarrier")),
                        flight_number=self._required_string(raw, "flightNumber", request_id),
                        duration_minutes=duration,
                        cabin_class=cabin_class,
                        direction=direction,
                    )
                )
        return segments

    @staticmethod
    def _reject_fr(segment: dict[str, object]) -> None:
        if segment.get("carrier") == "FR" or segment.get("operatingCarrier") == "FR":
            raise RoutingRejected()

    def _fee_total(
        self,
        value: dict[str, object],
        request: SearchRequest,
        segment_count: int,
        request_id: str | None,
    ) -> Decimal:
        fee = self._amount(value.get("transactionFee"), request_id)
        mode = value.get("transactionFeeMode")
        passengers = request.adults + request.children + request.infants
        if mode == "PER_PAX":
            return fee * passengers
        if mode == "PER_SEGMENT":
            return fee * passengers * segment_count
        if mode == "PER_TICKET":
            airline_orders = 2 if value.get("separateBookings") is True else 1
            return fee * passengers * airline_orders
        if mode == "PER_BOOKING":
            return fee
        self._invalid(request_id)

    @staticmethod
    def _ancillary_supported(value: object) -> tuple[Literal["baggage", "seat"], ...]:
        if not isinstance(value, list):
            return ()
        supported: set[Literal["baggage", "seat"]] = set()
        for item in value:
            if item == "seat":
                supported.add("seat")
            elif item == "luggage":
                supported.add("baggage")
        return tuple(sorted(supported))

    @staticmethod
    def _operating_carrier(value: object) -> str | None:
        if isinstance(value, str) and AIRLINE_CODE_PATTERN.fullmatch(value) is not None:
            return value
        return None

    @staticmethod
    def _required_string(value: dict[str, object], key: str, request_id: str | None) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result:
            RoutingNormalizer._invalid(request_id)
        return result

    @staticmethod
    def _amount(value: object, request_id: str | None) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            RoutingNormalizer._invalid(request_id)
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            RoutingNormalizer._invalid(request_id)
        if not amount.is_finite() or amount < 0:
            RoutingNormalizer._invalid(request_id)
        return amount

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _invalid(request_id: str | None) -> NoReturn:
        del request_id
        raise ValueError
