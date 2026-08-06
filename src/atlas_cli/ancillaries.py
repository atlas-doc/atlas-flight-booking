"""In-flow baggage and seat lookup with opaque, booking-bound selections."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from secrets import token_hex
from typing import NoReturn, Protocol

from pydantic import ValidationError

from atlas_cli.access import AccessManager, AccessManagerError, TransactionAccess
from atlas_cli.api_client import ApiClientError
from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BaggageOption,
    BookingContext,
    SeatOption,
    SegmentSlot,
)
from atlas_cli.booking_store import BookingStore, BookingStoreError
from atlas_cli.business_client import AtlasBusinessClient, BusinessApiError, BusinessResponse
from atlas_cli.business_status import BookingApiError, BusinessStage, booking_error_result, map_business_status
from atlas_cli.endpoints import BusinessOperation, BusinessRoute
from atlas_cli.models import CommandResult, success_result
from atlas_cli.secure_store import ApiCredential, Credentials, SecureStoreError

_TRANSIENT_CODES = {
    "BAGGAGE_UNAVAILABLE",
    "SEAT_UNAVAILABLE",
    "SERVICE_TEMPORARILY_UNAVAILABLE",
}
_BAGGAGE_DOWNGRADE_STATUSES = {205, 214, 299, 9999}
_SEAT_DOWNGRADE_STATUSES = {216, 217, 218, 219, 221, 223, 225}


def _new_token() -> str:
    return token_hex(12)


@dataclass(frozen=True)
class NormalizedAncillaryResponse:
    options: tuple[BaggageOption | SeatOption, ...]
    request_id: str | None = None


class BusinessClient(Protocol):
    def post(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        payload: dict[str, object],
    ) -> BusinessResponse: ...


class AncillaryAdapter:
    def __init__(
        self,
        business: BusinessClient | AtlasBusinessClient,
        *,
        token_factory: Callable[[], str] = _new_token,
        default_retry_seconds: float = 2.0,
    ) -> None:
        self._business = business
        self._token_factory = token_factory
        self._default_retry_seconds = default_retry_seconds

    def list_baggage(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        context: BookingContext,
    ) -> NormalizedAncillaryResponse:
        response = self._business.post(route, credential, {"offerId": context.session_id})
        self._raise_for_status(BusinessStage.BAGGAGE, response)
        return self._normalize_baggage(response, context)

    def list_seats(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        context: BookingContext,
    ) -> NormalizedAncillaryResponse:
        outbound, inbound = context.segment_payloads()
        payload: dict[str, object] = {
            "sessionId": context.session_id,
            "carrier": context.most_significant_carrier(),
            "outboundSegments": outbound,
        }
        if inbound:
            payload["inboundSegments"] = inbound
        response = self._business.post(route, credential, payload)
        self._raise_for_status(BusinessStage.SEAT, response)
        return self._normalize_seats(response, context)

    def _raise_for_status(self, stage: BusinessStage, response: BusinessResponse) -> None:
        meaning = map_business_status(stage, response.status)
        if meaning is None:
            return
        retry_after = response.data.get("retryAfter")
        safe_delay = (
            float(retry_after)
            if isinstance(retry_after, (int, float))
            and not isinstance(retry_after, bool)
            and 0 <= retry_after <= 60
            else self._default_retry_seconds
        )
        raise BookingApiError.from_meaning(
            meaning,
            request_id=response.request_id,
            retry_after_seconds=safe_delay if meaning.retryable else None,
            upstream_status=response.status,
        )

    def _normalize_baggage(
        self,
        response: BusinessResponse,
        context: BookingContext,
    ) -> NormalizedAncillaryResponse:
        data = response.data.get("data")
        if not isinstance(data, dict):
            raise BookingApiError.invalid_response(response.request_id)
        elements = data.get("ancillaryProductElements")
        if not isinstance(elements, list):
            raise BookingApiError.invalid_response(response.request_id)
        options: list[BaggageOption] = []
        try:
            for value in elements:
                if not isinstance(value, dict):
                    raise ValueError
                baggage = value.get("auxBaggageElement")
                if not isinstance(baggage, dict):
                    raise ValueError
                segment_index = self._required_int(value.get("segmentIndex"), minimum=1)
                slot = self._segment(context, segment_index)
                product_code = self._required_string(value.get("productCode"))
                piece = self._required_int(baggage.get("piece"), minimum=0)
                weight = self._required_int(baggage.get("weight"), minimum=0)
                size_value = baggage.get("size")
                if size_value is not None and not isinstance(size_value, str):
                    raise ValueError
                options.append(
                    BaggageOption(
                        baggage_id=f"bag_{self._token_factory()}",
                        product_code=product_code,
                        segment_id=slot.segment_id,
                        segment_index=segment_index,
                        piece=piece,
                        weight_kg=weight,
                        size=size_value,
                        category=self._required_string(value.get("categoryCode")),
                        price=self._required_number(value.get("price")),
                        currency=self._required_string(value.get("currency")),
                    )
                )
        except (ValidationError, ValueError):
            raise BookingApiError.invalid_response(response.request_id) from None
        return NormalizedAncillaryResponse(options=tuple(options), request_id=response.request_id)

    def _normalize_seats(
        self,
        response: BusinessResponse,
        context: BookingContext,
    ) -> NormalizedAncillaryResponse:
        cabins = response.data.get("cabins")
        if cabins is None:
            cabins = []
        if not isinstance(cabins, list):
            raise BookingApiError.invalid_response(response.request_id)
        options: list[SeatOption] = []
        try:
            for cabin_value in cabins:
                if not isinstance(cabin_value, dict):
                    raise ValueError
                segment_index = self._required_int(cabin_value.get("segmentIndex"), minimum=1)
                slot = self._segment(context, segment_index)
                cabin = cabin_value.get("cabin")
                if not isinstance(cabin, dict) or not isinstance(cabin.get("rows"), list):
                    raise ValueError
                for row_value in cabin["rows"]:
                    if not isinstance(row_value, dict) or not isinstance(row_value.get("seats"), list):
                        raise ValueError
                    row = self._required_int(row_value.get("number"), minimum=1)
                    for seat in row_value["seats"]:
                        if not isinstance(seat, dict):
                            raise ValueError
                        if seat.get("seatStatus") != "F":
                            continue
                        characteristics = seat.get("seatCharacteristics")
                        if characteristics is None:
                            characteristics = []
                        if not isinstance(characteristics, list) or any(
                            not isinstance(item, str) for item in characteristics
                        ):
                            raise ValueError
                        options.append(
                            SeatOption(
                                seat_id=f"seat_{self._token_factory()}",
                                product_code=self._required_string(seat.get("productCode")),
                                segment_id=slot.segment_id,
                                segment_index=segment_index,
                                row=row,
                                column=self._required_string(seat.get("column")),
                                characteristics=tuple(characteristics),
                                price=self._required_number(seat.get("price")),
                                currency=self._required_string(seat.get("currency")),
                            )
                        )
        except (ValidationError, ValueError):
            raise BookingApiError.invalid_response(response.request_id) from None
        return NormalizedAncillaryResponse(options=tuple(options), request_id=response.request_id)

    @staticmethod
    def _segment(context: BookingContext, segment_index: int) -> SegmentSlot:
        matches = [item for item in context.segments if item.segment_index == segment_index]
        if len(matches) != 1:
            raise ValueError
        return matches[0]

    @staticmethod
    def _required_string(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError
        return value

    @staticmethod
    def _required_int(value: object, *, minimum: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError
        return value

    @staticmethod
    def _required_number(value: object) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(value)
            or value < 0
        ):
            raise ValueError
        return float(value)


class ControlCredentialStore(Protocol):
    def load_credentials(self) -> Credentials | None: ...


class TransactionAccessResolver(Protocol):
    def resolve_transaction_access(
        self,
        jwt: str,
        operation: BusinessOperation,
    ) -> TransactionAccess: ...


class AncillaryGateway(Protocol):
    def list_baggage(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        context: BookingContext,
    ) -> NormalizedAncillaryResponse: ...

    def list_seats(
        self,
        route: BusinessRoute,
        credential: ApiCredential,
        context: BookingContext,
    ) -> NormalizedAncillaryResponse: ...


class AncillaryContextStore(Protocol):
    def load(self, booking_id: str, *, generation: str) -> BookingContext: ...

    def replace_options(
        self,
        booking_id: str,
        *,
        kind: AncillaryKind,
        options: tuple[BaggageOption | SeatOption, ...],
        generation: str,
    ) -> BookingContext: ...

    def close_ancillary(
        self,
        booking_id: str,
        *,
        kind: AncillaryKind,
        generation: str,
    ) -> BookingContext: ...

    def select(
        self,
        booking_id: str,
        selection: AncillarySelection,
        *,
        generation: str,
    ) -> BookingContext: ...

    def remove(
        self,
        booking_id: str,
        *,
        kind: AncillaryKind,
        traveler_id: str,
        segment_id: str,
        generation: str,
    ) -> BookingContext: ...


class AncillaryService:
    def __init__(
        self,
        *,
        secrets: ControlCredentialStore,
        access: TransactionAccessResolver | AccessManager,
        adapter: AncillaryGateway | AncillaryAdapter,
        booking_store: AncillaryContextStore | BookingStore,
        sleep: Callable[[float], None] = time.sleep,
        default_retry_seconds: float = 2.0,
    ) -> None:
        self._secrets = secrets
        self._access = access
        self._adapter = adapter
        self._booking_store = booking_store
        self._sleep = sleep
        self._default_retry_seconds = default_retry_seconds

    def list_baggage(self, booking_id: str) -> CommandResult:
        return self._list(booking_id, AncillaryKind.BAGGAGE)

    def list_seats(self, booking_id: str) -> CommandResult:
        return self._list(booking_id, AncillaryKind.SEAT)

    def select_baggage(
        self,
        booking_id: str,
        traveler_id: str,
        segment_id: str,
        baggage_id: str,
    ) -> CommandResult:
        return self._select(booking_id, AncillaryKind.BAGGAGE, traveler_id, segment_id, baggage_id)

    def select_seat(
        self,
        booking_id: str,
        traveler_id: str,
        segment_id: str,
        seat_id: str,
    ) -> CommandResult:
        return self._select(booking_id, AncillaryKind.SEAT, traveler_id, segment_id, seat_id)

    def remove_baggage(self, booking_id: str, traveler_id: str, segment_id: str) -> CommandResult:
        return self._remove(booking_id, AncillaryKind.BAGGAGE, traveler_id, segment_id)

    def remove_seat(self, booking_id: str, traveler_id: str, segment_id: str) -> CommandResult:
        return self._remove(booking_id, AncillaryKind.SEAT, traveler_id, segment_id)

    def _list(self, booking_id: str, kind: AncillaryKind) -> CommandResult:
        try:
            access, context = self._access_context(booking_id, kind)
            if not self._supported(context, kind):
                return self._unavailable(kind, booking_id)
            try:
                normalized = self._read_with_retry(access, context, kind)
            except (BookingApiError, BusinessApiError) as error:
                if self._downgrades(kind, error):
                    self._booking_store.close_ancillary(
                        booking_id,
                        kind=kind,
                        generation=access.route.generation,
                    )
                    if kind is AncillaryKind.SEAT and getattr(error, "upstream_status", None) == 214:
                        return self._error_result(error)
                    return self._unavailable(kind, booking_id, request_id=getattr(error, "request_id", None))
                raise
            if not normalized.options:
                self._booking_store.close_ancillary(
                    booking_id,
                    kind=kind,
                    generation=access.route.generation,
                )
                return self._unavailable(kind, booking_id, request_id=normalized.request_id)
            self._booking_store.replace_options(
                booking_id,
                kind=kind,
                options=normalized.options,
                generation=access.route.generation,
            )
        except (
            AccessManagerError,
            ApiClientError,
            BookingApiError,
            BookingStoreError,
            BusinessApiError,
            SecureStoreError,
        ) as error:
            return self._error_result(error)
        return success_result(
            f"{kind.value.upper()}_OPTIONS_LISTED",
            f"{kind.value.capitalize()} options listed",
            request_id=normalized.request_id,
            data={"booking_id": booking_id, "options": self._public_options(kind, normalized.options)},
        )

    def _select(
        self,
        booking_id: str,
        kind: AncillaryKind,
        traveler_id: str,
        segment_id: str,
        option_id: str,
    ) -> CommandResult:
        try:
            access, context = self._access_context(booking_id, kind)
            option = self._bound_option(context, kind, traveler_id, segment_id, option_id)
            selection = AncillarySelection(
                kind=kind,
                traveler_id=traveler_id,
                segment_id=segment_id,
                option_id=option_id,
                product_code=option.product_code,
                segment_index=option.segment_index,
            )
            if kind is AncillaryKind.BAGGAGE:
                if not isinstance(option, BaggageOption):
                    self._raise_selection_invalid()
                self._require_connected_baggage_consistency(context, option, traveler_id, segment_id)
            self._booking_store.select(
                booking_id,
                selection,
                generation=access.route.generation,
            )
        except (
            AccessManagerError,
            ApiClientError,
            BookingApiError,
            BookingStoreError,
            BusinessApiError,
            SecureStoreError,
        ) as error:
            return self._error_result(error)
        public_name = "baggage_id" if kind is AncillaryKind.BAGGAGE else "seat_id"
        return success_result(
            f"{kind.value.upper()}_SELECTED",
            f"{kind.value.capitalize()} selected",
            data={
                "booking_id": booking_id,
                "traveler_id": traveler_id,
                "segment_id": segment_id,
                public_name: option_id,
            },
        )

    def _remove(
        self,
        booking_id: str,
        kind: AncillaryKind,
        traveler_id: str,
        segment_id: str,
    ) -> CommandResult:
        try:
            access, context = self._access_context(booking_id, kind)
            self._require_slot(context, traveler_id, segment_id)
            self._booking_store.remove(
                booking_id,
                kind=kind,
                traveler_id=traveler_id,
                segment_id=segment_id,
                generation=access.route.generation,
            )
        except (
            AccessManagerError,
            ApiClientError,
            BookingApiError,
            BookingStoreError,
            BusinessApiError,
            SecureStoreError,
        ) as error:
            return self._error_result(error)
        return success_result(
            f"{kind.value.upper()}_REMOVED",
            f"{kind.value.capitalize()} removed",
            data={"booking_id": booking_id, "traveler_id": traveler_id, "segment_id": segment_id},
        )

    def _access_context(
        self,
        booking_id: str,
        kind: AncillaryKind,
    ) -> tuple[TransactionAccess, BookingContext]:
        credentials = self._secrets.load_credentials()
        if credentials is None or not credentials.jwt.strip():
            raise AccessManagerError(code="AUTHORIZATION_REQUIRED", message="Authorization required")
        operation = BusinessOperation.BAGGAGE if kind is AncillaryKind.BAGGAGE else BusinessOperation.SEAT
        access = self._access.resolve_transaction_access(credentials.jwt, operation)
        context = self._booking_store.load(booking_id, generation=access.route.generation)
        return access, context

    def _read_with_retry(
        self,
        access: TransactionAccess,
        context: BookingContext,
        kind: AncillaryKind,
    ) -> NormalizedAncillaryResponse:
        try:
            return self._read(access, context, kind)
        except (BookingApiError, BusinessApiError) as error:
            if not self._transient(error):
                raise
            delay = getattr(error, "retry_after_seconds", None)
            self._sleep(delay if isinstance(delay, (int, float)) else self._default_retry_seconds)
        return self._read(access, context, kind)

    def _read(
        self,
        access: TransactionAccess,
        context: BookingContext,
        kind: AncillaryKind,
    ) -> NormalizedAncillaryResponse:
        if kind is AncillaryKind.BAGGAGE:
            return self._adapter.list_baggage(access.route, access.credential, context)
        return self._adapter.list_seats(access.route, access.credential, context)

    @staticmethod
    def _transient(error: Exception) -> bool:
        return bool(getattr(error, "retryable", False)) and getattr(error, "code", None) in _TRANSIENT_CODES

    @staticmethod
    def _downgrades(kind: AncillaryKind, error: Exception) -> bool:
        upstream_status = getattr(error, "upstream_status", None)
        if kind is AncillaryKind.BAGGAGE:
            return upstream_status in _BAGGAGE_DOWNGRADE_STATUSES
        return upstream_status in _SEAT_DOWNGRADE_STATUSES or upstream_status == 214

    @staticmethod
    def _supported(context: BookingContext, kind: AncillaryKind) -> bool:
        return context.baggage_supported if kind is AncillaryKind.BAGGAGE else context.seat_supported

    @classmethod
    def _bound_option(
        cls,
        context: BookingContext,
        kind: AncillaryKind,
        traveler_id: str,
        segment_id: str,
        option_id: str,
    ) -> BaggageOption | SeatOption:
        cls._require_slot(context, traveler_id, segment_id)
        if not cls._supported(context, kind):
            cls._raise_selection_invalid()
        options: tuple[BaggageOption | SeatOption, ...] = (
            context.baggage_options if kind is AncillaryKind.BAGGAGE else context.seat_options
        )
        for option in options:
            current_id = option.baggage_id if isinstance(option, BaggageOption) else option.seat_id
            if current_id == option_id and option.segment_id == segment_id:
                return option
        cls._raise_selection_invalid()

    @staticmethod
    def _require_slot(context: BookingContext, traveler_id: str, segment_id: str) -> None:
        if not any(item.traveler_id == traveler_id for item in context.travelers) or not any(
            item.segment_id == segment_id for item in context.segments
        ):
            AncillaryService._raise_selection_invalid()

    @classmethod
    def _require_connected_baggage_consistency(
        cls,
        context: BookingContext,
        selected: BaggageOption,
        traveler_id: str,
        segment_id: str,
    ) -> None:
        selected_segment = next(item for item in context.segments if item.segment_id == segment_id)
        connected_ids = {
            item.segment_id for item in context.segments if item.direction == selected_segment.direction
        }
        signature = (selected.piece, selected.weight_kg, selected.size, selected.category)
        options_by_id = {item.baggage_id: item for item in context.baggage_options}
        for selection in context.selections:
            if (
                selection.kind is not AncillaryKind.BAGGAGE
                or selection.traveler_id != traveler_id
                or selection.segment_id == segment_id
                or selection.segment_id not in connected_ids
            ):
                continue
            previous = options_by_id.get(selection.option_id)
            if previous is None or (previous.piece, previous.weight_kg, previous.size, previous.category) != signature:
                cls._raise_selection_invalid()

    @staticmethod
    def _raise_selection_invalid() -> NoReturn:
        raise BookingStoreError(
            code="ANCILLARY_SELECTION_INVALID",
            message="Selected optional service is no longer available",
        )

    @staticmethod
    def _public_options(
        kind: AncillaryKind,
        options: tuple[BaggageOption | SeatOption, ...],
    ) -> list[dict[str, object]]:
        public: list[dict[str, object]] = []
        if kind is AncillaryKind.BAGGAGE:
            for option in options:
                if not isinstance(option, BaggageOption):
                    continue
                public.append(
                    {
                        "baggage_id": option.baggage_id,
                        "segment_id": option.segment_id,
                        "piece": option.piece,
                        "weight_kg": option.weight_kg,
                        "size": option.size,
                        "category": option.category,
                        "price": option.price,
                        "currency": option.currency,
                    }
                )
        else:
            for option in options:
                if not isinstance(option, SeatOption):
                    continue
                public.append(
                    {
                        "seat_id": option.seat_id,
                        "segment_id": option.segment_id,
                        "row": option.row,
                        "column": option.column,
                        "characteristics": list(option.characteristics),
                        "price": option.price,
                        "currency": option.currency,
                    }
                )
        return public

    @staticmethod
    def _unavailable(
        kind: AncillaryKind,
        booking_id: str,
        *,
        request_id: str | None = None,
    ) -> CommandResult:
        label = kind.value.capitalize()
        return success_result(
            f"{kind.value.upper()}_UNAVAILABLE",
            f"{label} selection is unavailable",
            request_id=request_id,
            data={"booking_id": booking_id, "options": []},
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
