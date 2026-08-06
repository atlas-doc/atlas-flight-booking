"""One-shot passenger input validation and Atlas order payload conversion."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from atlas_cli.booking_models import (
    BookingContext,
    BookingRequirements,
    MaskedPassengerSummary,
    RequirementField,
    TravelerSlot,
)

_NAME_PART = re.compile(r"[A-Z]+(?: [A-Z]+)*")
_COUNTRY = re.compile(r"[A-Z]{2}")
_MOBILE = re.compile(r"00[1-9][0-9]{0,2}-[0-9]{6,14}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PASSENGER_FIELDS = frozenset(
    {
        "passengers",
        "contact",
        "traveler_id",
        "name",
        "passenger_type",
        "gender",
        "birthday",
        "nationality",
        "document",
        "type",
        "number",
        "issuing_country",
        "expires",
        "email",
        "mobile",
    }
)
_PASSENGER_TYPE = {"adult": 0, "child": 1, "infant": 2}


class PassengerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def _normalize_name(value: object) -> object:
    if not isinstance(value, str):
        return value
    pieces = value.strip().upper().split("/")
    if len(pieces) != 2:
        raise ValueError("Name must use the required format")
    family, given = (piece.strip() for piece in pieces)
    if _NAME_PART.fullmatch(family) is None or _NAME_PART.fullmatch(given) is None:
        raise ValueError("Name must use the required format")
    return f"{family}/{given}"


def _normalize_country(value: object) -> object:
    if value is None or not isinstance(value, str):
        return value
    normalized = value.strip().upper()
    if _COUNTRY.fullmatch(normalized) is None:
        raise ValueError("Country must use the required format")
    return normalized


def _require_iso_date(value: object) -> object:
    if isinstance(value, datetime):
        raise ValueError("Date must use the required format")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise ValueError("Date must use the required format")
    return value


class PassengerDocument(PassengerModel):
    type: Literal["PP", "GA", "TW", "TB", "HY"]
    number: str = Field(repr=False)
    issuing_country: str | None = Field(default=None, repr=False)
    expires: date | None = Field(default=None, repr=False)

    @field_validator("number", mode="before")
    @classmethod
    def normalize_number(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if not value:
            raise ValueError("Document number must use the required format")
        return value

    @field_validator("issuing_country", mode="before")
    @classmethod
    def normalize_issuing_country(cls, value: object) -> object:
        return _normalize_country(value)

    @field_validator("expires", mode="before")
    @classmethod
    def require_iso_expiry(cls, value: object) -> object:
        if value is None:
            return None
        return _require_iso_date(value)


class Passenger(PassengerModel):
    traveler_id: str
    name: str = Field(repr=False)
    passenger_type: Literal["adult", "child", "infant"]
    gender: Literal["M", "F"] = Field(repr=False)
    birthday: date | None = Field(default=None, repr=False)
    nationality: str | None = Field(default=None, repr=False)
    document: PassengerDocument | None = Field(default=None, repr=False)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return _normalize_name(value)

    @field_validator("birthday", mode="before")
    @classmethod
    def require_iso_birthday(cls, value: object) -> object:
        if value is None:
            return None
        return _require_iso_date(value)

    @field_validator("nationality", mode="before")
    @classmethod
    def normalize_nationality(cls, value: object) -> object:
        return _normalize_country(value)


class Contact(PassengerModel):
    name: str = Field(repr=False)
    email: str | None = Field(default=None, repr=False)
    mobile: str | None = Field(default=None, repr=False)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return _normalize_name(value)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip()
        if _EMAIL.fullmatch(normalized) is None:
            raise ValueError("Email must use the required format")
        return normalized

    @field_validator("mobile", mode="before")
    @classmethod
    def normalize_mobile(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        if _MOBILE.fullmatch(value) is None:
            raise ValueError("Mobile must use the required format")
        return value


class PassengerInput(PassengerModel):
    passengers: tuple[Passenger, ...] = Field(repr=False)
    contact: Contact = Field(repr=False)


class MaskedPassengerInput(PassengerModel):
    passengers: tuple[MaskedPassengerSummary, ...]


class PassengerInputError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields


@dataclass(frozen=True)
class PassengerSource:
    use_stdin: bool
    file_path: Path | None
    stdin: TextIO


def safe_validation_locations(error: ValueError | UnicodeError) -> tuple[str, ...]:
    """Return only schema-owned locations from a rejected Pydantic value."""
    if not isinstance(error, ValidationError):
        return ("passengers",)
    locations: list[str] = []
    for detail in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        raw_location = detail.get("loc")
        if not isinstance(raw_location, tuple) or not raw_location:
            continue
        components: list[str | int] = []
        safe = True
        for component in raw_location:
            safe_index = isinstance(component, int) and component >= 0
            safe_field = isinstance(component, str) and component in _PASSENGER_FIELDS
            if safe_index or safe_field:
                components.append(component)
            else:
                safe = False
                break
        if not safe:
            continue
        location = _format_location(components)
        if location and location not in locations:
            locations.append(location)
    return tuple(locations) or ("passengers",)


def _format_location(components: Sequence[str | int]) -> str:
    location = ""
    for component in components:
        if isinstance(component, int):
            location += f"[{component}]"
        elif location:
            location += f".{component}"
        else:
            location = component
    return location


def load_passenger_input(source: PassengerSource) -> PassengerInput:
    if source.use_stdin == (source.file_path is not None):
        raise PassengerInputError(
            code="INVALID_ARGUMENT",
            message="Choose exactly one passenger input source",
            fields=("passenger_source",),
        )
    try:
        if source.use_stdin:
            raw = source.stdin.read()
        else:
            assert source.file_path is not None
            if not source.file_path.is_absolute() or not source.file_path.is_file():
                raise PassengerInputError(
                    code="INVALID_ARGUMENT",
                    message="Passenger file must be an absolute regular-file path",
                    fields=("passengers_file",),
                )
            raw = source.file_path.read_text(encoding="utf-8")
    except PassengerInputError:
        raise
    except (OSError, UnicodeError):
        raise PassengerInputError(
            code="PASSENGER_INFO_INVALID",
            message="Passenger information could not be read",
            fields=("passenger_source",),
        ) from None
    try:
        return PassengerInput.model_validate_json(raw)
    except (ValueError, UnicodeError) as error:
        raise PassengerInputError(
            code="PASSENGER_INFO_INVALID",
            message="Passenger information could not be accepted",
            fields=safe_validation_locations(error),
        ) from None


def validate_requirements(
    value: PassengerInput,
    requirements: BookingRequirements,
    travelers: tuple[TravelerSlot, ...],
    *,
    today: date,
) -> None:
    passenger_ids = [passenger.traveler_id for passenger in value.passengers]
    traveler_by_id = {traveler.traveler_id: traveler for traveler in travelers}
    if (
        len(passenger_ids) != len(set(passenger_ids))
        or len(traveler_by_id) != len(travelers)
        or set(passenger_ids) != set(traveler_by_id)
    ):
        _raise_invalid(("passengers",))

    type_mismatches = tuple(
        f"passengers[{index}].passenger_type"
        for index, passenger in enumerate(value.passengers)
        if passenger.passenger_type != traveler_by_id[passenger.traveler_id].passenger_type
    )
    if type_mismatches:
        _raise_invalid(type_mismatches)

    missing: list[str] = []
    for index, passenger in enumerate(value.passengers):
        for required in requirements.required_fields:
            if _required_value(passenger, required) is None:
                missing.append(f"passengers[{index}].{required}")
    if missing:
        raise PassengerInputError(
            code="PASSENGER_INFO_REQUIRED",
            message="Required passenger information is missing",
            fields=tuple(missing),
        )

    invalid_dates: list[str] = []
    for index, passenger in enumerate(value.passengers):
        if passenger.birthday is not None and passenger.birthday >= today:
            invalid_dates.append(f"passengers[{index}].birthday")
        if (
            passenger.document is not None
            and passenger.document.expires is not None
            and passenger.document.expires <= today
        ):
            invalid_dates.append(f"passengers[{index}].document.expires")
    if invalid_dates:
        _raise_invalid(tuple(invalid_dates))


def _required_value(passenger: Passenger, required: RequirementField) -> object | None:
    direct: dict[RequirementField, object | None] = {
        "name": passenger.name,
        "passenger_type": passenger.passenger_type,
        "gender": passenger.gender,
        "birthday": passenger.birthday,
        "nationality": passenger.nationality,
        "document.type": None,
        "document.number": None,
        "document.issuing_country": None,
        "document.expires": None,
    }
    if passenger.document is not None:
        direct.update(
            {
                "document.type": passenger.document.type,
                "document.number": passenger.document.number,
                "document.issuing_country": passenger.document.issuing_country,
                "document.expires": passenger.document.expires,
            }
        )
    return direct[required]


def _raise_invalid(fields: tuple[str, ...]) -> None:
    raise PassengerInputError(
        code="PASSENGER_INFO_INVALID",
        message="Passenger information is invalid",
        fields=fields,
    )


def to_order_payload(value: PassengerInput, context: BookingContext) -> dict[str, object]:
    passengers: list[dict[str, object]] = []
    for passenger in value.passengers:
        converted: dict[str, object] = {
            "name": passenger.name,
            "passengerType": _PASSENGER_TYPE[passenger.passenger_type],
            "gender": passenger.gender,
        }
        if passenger.birthday is not None:
            converted["birthday"] = passenger.birthday.strftime("%Y%m%d")
        if passenger.document is not None:
            converted["cardType"] = passenger.document.type
            converted["cardNum"] = passenger.document.number
            if passenger.document.issuing_country is not None:
                converted["cardIssuePlace"] = passenger.document.issuing_country
            if passenger.document.expires is not None:
                converted["cardExpired"] = passenger.document.expires.strftime("%Y%m%d")
        if passenger.nationality is not None:
            converted["nationality"] = passenger.nationality
        ancillaries = [
            {
                "productCode": selection.product_code,
                "segmentIndex": selection.segment_index,
            }
            for selection in context.selections
            if selection.traveler_id == passenger.traveler_id
        ]
        if ancillaries:
            converted["ancillaries"] = ancillaries
        passengers.append(converted)

    contact: dict[str, object] = {"name": value.contact.name}
    if value.contact.email is not None:
        contact["email"] = value.contact.email
    if value.contact.mobile is not None:
        contact["mobile"] = value.contact.mobile
    return {"passengers": passengers, "contact": contact}


def mask_name(value: str) -> str:
    family, separator, given = value.partition("/")
    if not separator:
        return value[:1] + "***"
    return family[:1] + "***/" + given[:1] + "***"


def mask_document(value: str | None) -> str | None:
    if value is None:
        return None
    return "****" + value[-4:]


def masked_summary(value: PassengerInput) -> MaskedPassengerInput:
    return MaskedPassengerInput(
        passengers=tuple(
            MaskedPassengerSummary(
                traveler_id=passenger.traveler_id,
                name=mask_name(passenger.name),
                document=mask_document(passenger.document.number if passenger.document is not None else None),
            )
            for passenger in value.passengers
        )
    )
