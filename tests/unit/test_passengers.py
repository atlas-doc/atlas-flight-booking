from __future__ import annotations

import json
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BookingContext,
    BookingRequirements,
    SegmentSlot,
    TravelerSlot,
)
from atlas_cli.passengers import (
    PassengerInput,
    PassengerInputError,
    PassengerSource,
    load_passenger_input,
    masked_summary,
    to_order_payload,
    validate_requirements,
)
from atlas_cli.search_models import NormalizedOffer, NormalizedPassengerPrice, NormalizedSegment

TODAY = date(2026, 8, 5)


def passenger_data() -> dict[str, Any]:
    return {
        "passengers": [
            {
                "traveler_id": "trav_1",
                "name": " santos / maria ",
                "passenger_type": "adult",
                "gender": "F",
                "birthday": "1990-05-01",
                "nationality": "br",
                "document": {
                    "type": "PP",
                    "number": "P1234567",
                    "issuing_country": "pt",
                    "expires": "2030-12-31",
                },
            }
        ],
        "contact": {
            "name": " santos / maria ",
            "email": "maria@example.com",
            "mobile": "0055-11998765432",
        },
    }


def passenger_json(value: dict[str, Any] | None = None) -> str:
    return json.dumps(passenger_data() if value is None else value)


def requirements(*required: str) -> BookingRequirements:
    return BookingRequirements(required_fields=required)  # type: ignore[arg-type]


def traveler_slots() -> tuple[TravelerSlot, ...]:
    return (TravelerSlot(traveler_id="trav_1", passenger_type="adult"),)


def assert_safe_error(
    error: PassengerInputError,
    *,
    code: str,
    fields: tuple[str, ...],
    private: str,
) -> None:
    assert error.code == code
    assert error.fields == fields
    assert private not in str(error)
    assert private not in repr(error)
    assert private not in repr(error.fields)


def load_json(raw: str) -> PassengerInput:
    return load_passenger_input(PassengerSource(use_stdin=True, file_path=None, stdin=StringIO(raw)))


def test_stdin_and_file_are_mutually_exclusive(tmp_path: Path) -> None:
    passenger_file = tmp_path / "passengers.json"
    passenger_file.write_text(passenger_json(), encoding="utf-8")

    with pytest.raises(PassengerInputError) as raised:
        load_passenger_input(
            PassengerSource(
                use_stdin=True,
                file_path=passenger_file,
                stdin=StringIO(passenger_json()),
            )
        )

    assert raised.value.code == "INVALID_ARGUMENT"
    assert raised.value.fields == ("passenger_source",)


def test_stdin_is_read_exactly_once() -> None:
    class OneReadInput(StringIO):
        reads = 0

        def read(self, *args: object, **kwargs: object) -> str:
            self.reads += 1
            if self.reads > 1:
                raise AssertionError("stdin was read more than once")
            return super().read(*args, **kwargs)

    stdin = OneReadInput(passenger_json())

    loaded = load_passenger_input(PassengerSource(use_stdin=True, file_path=None, stdin=stdin))

    assert loaded.passengers[0].name == "SANTOS/MARIA"
    assert stdin.reads == 1


def test_absolute_file_is_read_without_modifying_or_deleting_it(tmp_path: Path) -> None:
    passenger_file = (tmp_path / "passengers.json").resolve()
    raw = passenger_json()
    passenger_file.write_text(raw, encoding="utf-8")
    before = passenger_file.stat()

    loaded = load_passenger_input(PassengerSource(use_stdin=False, file_path=passenger_file, stdin=StringIO("unused")))

    after = passenger_file.stat()
    assert loaded.contact.mobile == "0055-11998765432"
    assert passenger_file.read_text(encoding="utf-8") == raw
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )


@pytest.mark.parametrize(
    ("raw", "field", "private"),
    [
        ('{"passengers": ["INVALID_JSON_PRIVATE_81"', "passengers", "INVALID_JSON_PRIVATE_81"),
        (
            passenger_json(
                {
                    **passenger_data(),
                    "passengers": [{**passenger_data()["passengers"][0], "birthday": "NON_ISO_PRIVATE_82"}],
                }
            ),
            "passengers[0].birthday",
            "NON_ISO_PRIVATE_82",
        ),
        (
            passenger_json(
                {
                    **passenger_data(),
                    "passengers": [{**passenger_data()["passengers"][0], "nationality": "X_PRIVATE_83"}],
                }
            ),
            "passengers[0].nationality",
            "X_PRIVATE_83",
        ),
        (
            passenger_json(
                {
                    **passenger_data(),
                    "passengers": [
                        {
                            **passenger_data()["passengers"][0],
                            "document": {
                                **passenger_data()["passengers"][0]["document"],
                                "issuing_country": "X_PRIVATE_84",
                            },
                        }
                    ],
                }
            ),
            "passengers[0].document.issuing_country",
            "X_PRIVATE_84",
        ),
        (
            passenger_json(
                {
                    **passenger_data(),
                    "passengers": [{**passenger_data()["passengers"][0], "name": "NAME_PRIVATE_85"}],
                }
            ),
            "passengers[0].name",
            "NAME_PRIVATE_85",
        ),
        (
            passenger_json(
                {
                    **passenger_data(),
                    "contact": {**passenger_data()["contact"], "name": "CONTACT_PRIVATE_86"},
                }
            ),
            "contact.name",
            "CONTACT_PRIVATE_86",
        ),
        (
            passenger_json(
                {
                    **passenger_data(),
                    "contact": {
                        **passenger_data()["contact"],
                        "mobile": "+5511998765432",
                    },
                }
            ),
            "contact.mobile",
            "+5511998765432",
        ),
    ],
)
def test_invalid_input_has_exact_allowlisted_location_without_pii(
    raw: str,
    field: str,
    private: str,
) -> None:
    with pytest.raises(PassengerInputError) as raised:
        load_json(raw)

    assert_safe_error(
        raised.value,
        code="PASSENGER_INFO_INVALID",
        fields=(field,),
        private=private,
    )


@pytest.mark.parametrize("mode", ["relative", "missing"])
def test_invalid_file_path_is_neutral(mode: str, tmp_path: Path) -> None:
    private = f"PRIVATE_FILE_88_{mode.upper()}"
    path = Path(private) if mode == "relative" else (tmp_path / private).resolve()

    with pytest.raises(PassengerInputError) as raised:
        load_passenger_input(PassengerSource(use_stdin=False, file_path=path, stdin=StringIO("unused")))

    assert_safe_error(
        raised.value,
        code="INVALID_ARGUMENT",
        fields=("passengers_file",),
        private=private,
    )


@pytest.mark.parametrize(
    "mobile",
    [
        "001-87291810",
        "0086-13928109091",
        "00971-19201998",
    ],
)
def test_official_mobile_format_is_preserved_exactly(mobile: str) -> None:
    data = passenger_data()
    data["contact"]["mobile"] = mobile

    loaded = load_json(passenger_json(data))
    payload = to_order_payload(loaded, booking_context())

    assert loaded.contact.mobile == mobile
    assert payload["contact"]["mobile"] == mobile  # type: ignore[index]


def test_mixed_case_punctuated_document_is_preserved_for_atlas() -> None:
    private = "Ab-1234"
    data = passenger_data()
    data["passengers"][0]["document"]["number"] = private

    loaded = load_json(passenger_json(data))
    payload = to_order_payload(loaded, booking_context())

    assert loaded.passengers[0].document is not None
    assert loaded.passengers[0].document.number == private
    assert payload["passengers"][0]["cardNum"] == private  # type: ignore[index]
    assert private not in repr(loaded)


def test_empty_document_number_is_rejected_without_exposing_other_pii() -> None:
    private = "PRIVATE/DOCUMENT"
    data = passenger_data()
    data["passengers"][0]["name"] = private
    data["passengers"][0]["document"]["number"] = ""

    with pytest.raises(PassengerInputError) as raised:
        load_json(passenger_json(data))

    assert_safe_error(
        raised.value,
        code="PASSENGER_INFO_INVALID",
        fields=("passengers[0].document.number",),
        private=private,
    )


def validation_input(*passengers: dict[str, Any]) -> PassengerInput:
    data = passenger_data()
    data["passengers"] = list(passengers)
    return PassengerInput.model_validate(data)


@pytest.mark.parametrize(
    ("passengers", "travelers", "field", "private"),
    [
        (
            (
                {**passenger_data()["passengers"][0], "traveler_id": "PRIVATE_DUPLICATE_91"},
                {**passenger_data()["passengers"][0], "traveler_id": "PRIVATE_DUPLICATE_91"},
            ),
            (
                TravelerSlot(traveler_id="PRIVATE_DUPLICATE_91", passenger_type="adult"),
                TravelerSlot(traveler_id="trav_2", passenger_type="adult"),
            ),
            "passengers",
            "PRIVATE_DUPLICATE_91",
        ),
        (
            ({**passenger_data()["passengers"][0], "traveler_id": "PRIVATE_UNKNOWN_92"},),
            traveler_slots(),
            "passengers",
            "PRIVATE_UNKNOWN_92",
        ),
        (
            (passenger_data()["passengers"][0],),
            (
                *traveler_slots(),
                TravelerSlot(traveler_id="PRIVATE_MISSING_93", passenger_type="child"),
            ),
            "passengers",
            "PRIVATE_MISSING_93",
        ),
        (
            (
                {
                    **passenger_data()["passengers"][0],
                    "passenger_type": "child",
                    "name": "PRIVATE/MISMATCH",
                },
            ),
            traveler_slots(),
            "passengers[0].passenger_type",
            "PRIVATE/MISMATCH",
        ),
    ],
)
def test_travelers_match_one_to_one_without_exposing_input(
    passengers: tuple[dict[str, Any], ...],
    travelers: tuple[TravelerSlot, ...],
    field: str,
    private: str,
) -> None:
    value = validation_input(*passengers)

    with pytest.raises(PassengerInputError) as raised:
        validate_requirements(value, requirements("name"), travelers, today=TODAY)

    assert_safe_error(
        raised.value,
        code="PASSENGER_INFO_INVALID",
        fields=(field,),
        private=private,
    )


@pytest.mark.parametrize(
    ("updates", "field", "private"),
    [
        ({"birthday": "2026-08-05", "name": "PRIVATE/BIRTHDAY"}, "birthday", "PRIVATE/BIRTHDAY"),
        (
            {"birthday": "2026-08-06", "name": "PRIVATE/FUTURE"},
            "birthday",
            "PRIVATE/FUTURE",
        ),
        (
            {
                "name": "PRIVATE/EXPIRES",
                "document": {
                    **passenger_data()["passengers"][0]["document"],
                    "expires": "2026-08-05",
                },
            },
            "document.expires",
            "PRIVATE/EXPIRES",
        ),
    ],
)
def test_dates_must_be_on_the_safe_side_of_today(updates: dict[str, Any], field: str, private: str) -> None:
    passenger = {**passenger_data()["passengers"][0], **updates}
    value = validation_input(passenger)

    with pytest.raises(PassengerInputError) as raised:
        validate_requirements(value, requirements("name"), traveler_slots(), today=TODAY)

    assert_safe_error(
        raised.value,
        code="PASSENGER_INFO_INVALID",
        fields=(f"passengers[0].{field}",),
        private=private,
    )


def test_only_latest_required_fields_are_enforced() -> None:
    data = passenger_data()
    passenger = data["passengers"][0]
    passenger.pop("document")
    input_value = PassengerInput.model_validate(data)

    validate_requirements(
        input_value,
        requirements("name", "birthday"),
        traveler_slots(),
        today=TODAY,
    )
    with pytest.raises(PassengerInputError) as raised:
        validate_requirements(
            input_value,
            requirements("document.number"),
            traveler_slots(),
            today=TODAY,
        )

    assert raised.value.code == "PASSENGER_INFO_REQUIRED"
    assert raised.value.fields == ("passengers[0].document.number",)


@pytest.mark.parametrize(
    ("required", "document", "expected"),
    [
        (
            ("document.type", "document.number"),
            None,
            (
                "passengers[0].document.type",
                "passengers[0].document.number",
            ),
        ),
        (
            ("document.issuing_country", "document.expires"),
            {"type": "PP", "number": "P1234567"},
            (
                "passengers[0].document.issuing_country",
                "passengers[0].document.expires",
            ),
        ),
    ],
)
def test_missing_requested_document_fields_report_exact_locations(
    required: tuple[str, ...],
    document: dict[str, Any] | None,
    expected: tuple[str, ...],
) -> None:
    private = "PRIVATE/REQUIRED"
    passenger = {
        **passenger_data()["passengers"][0],
        "name": private,
        "document": document,
    }
    value = validation_input(passenger)

    with pytest.raises(PassengerInputError) as raised:
        validate_requirements(value, requirements(*required), traveler_slots(), today=TODAY)

    assert_safe_error(
        raised.value,
        code="PASSENGER_INFO_REQUIRED",
        fields=expected,
        private=private,
    )


def booking_context() -> BookingContext:
    now = datetime(2026, 8, 5, 8, tzinfo=UTC)
    segment = NormalizedSegment(
        departure_airport="KUL",
        arrival_airport="SIN",
        departure_time="202608101000",
        arrival_time="202608101110",
        carrier="AK",
        flight_number="AK701",
        duration_minutes=70,
        cabin_class=1,
    )
    offer = NormalizedOffer(
        upstream_identifier="opaque-route",
        currency="USD",
        total_price=100,
        transaction_fee_total=5,
        passenger_prices=(
            NormalizedPassengerPrice(
                passenger_type="adult",
                count=1,
                base_fare_per_passenger=75,
                tax_per_passenger=20,
                subtotal=95,
            ),
        ),
        segments=(segment,),
        ancillary_supported=("baggage", "seat"),
        bookable=True,
        price_status="verified",
    )
    return BookingContext(
        booking_id="book_1",
        search_id="srch_1",
        offer_id="off_1",
        route_generation="g" * 24,
        secret_ref="bsec_abcdefghijkl",
        secret_revision="rev_abcdefghijkl",
        session_id="private-session",
        searched_offer=offer,
        verified_offer=offer,
        price_change="unchanged",
        requirements=requirements("name"),
        travelers=traveler_slots(),
        segments=(
            SegmentSlot(
                segment_id="seg_1",
                segment_index=1,
                direction="outbound",
                segment=segment,
            ),
        ),
        baggage_supported=True,
        seat_supported=True,
        selections=(
            AncillarySelection(
                kind=AncillaryKind.BAGGAGE,
                traveler_id="trav_1",
                segment_id="seg_1",
                option_id="bag_1",
                product_code="BAG_PRIVATE_PRODUCT",
                segment_index=1,
            ),
            AncillarySelection(
                kind=AncillaryKind.SEAT,
                traveler_id="trav_1",
                segment_id="seg_1",
                option_id="seat_1",
                product_code="SEAT_PRIVATE_PRODUCT",
                segment_index=1,
            ),
        ),
        created_at=now,
        updated_at=now,
        expires_at=now,
    )


def test_order_payload_uses_atlas_fields_and_current_global_segment_selections() -> None:
    loaded = load_json(passenger_json())

    payload = to_order_payload(loaded, booking_context())

    assert payload == {
        "passengers": [
            {
                "name": "SANTOS/MARIA",
                "passengerType": 0,
                "gender": "F",
                "birthday": "19900501",
                "cardType": "PP",
                "cardNum": "P1234567",
                "cardIssuePlace": "PT",
                "cardExpired": "20301231",
                "nationality": "BR",
                "ancillaries": [
                    {"productCode": "BAG_PRIVATE_PRODUCT", "segmentIndex": 1},
                    {"productCode": "SEAT_PRIVATE_PRODUCT", "segmentIndex": 1},
                ],
            }
        ],
        "contact": {
            "name": "SANTOS/MARIA",
            "email": "maria@example.com",
            "mobile": "0055-11998765432",
        },
    }


@pytest.mark.parametrize(
    ("passenger_type", "expected"),
    [("adult", 0), ("child", 1), ("infant", 2)],
)
def test_order_payload_maps_each_passenger_type(passenger_type: str, expected: int) -> None:
    data = passenger_data()
    data["passengers"][0]["passenger_type"] = passenger_type
    value = PassengerInput.model_validate(data)
    context = booking_context().model_copy(
        update={
            "travelers": (TravelerSlot(traveler_id="trav_1", passenger_type=passenger_type),),
            "selections": (),
        }
    )

    payload = to_order_payload(value, context)

    assert payload["passengers"][0]["passengerType"] == expected  # type: ignore[index]


def test_masked_summary_and_repr_never_expose_pii() -> None:
    loaded = PassengerInput.model_validate_json(passenger_json())

    summary = masked_summary(loaded)

    exposed = f"{loaded!r} {summary!r} {summary}"
    assert "P1234567" not in exposed
    assert "maria@example.com" not in exposed
    assert "SANTOS/MARIA" not in exposed
    assert summary.passengers[0].name == "S***/M***"
    assert summary.passengers[0].document == "****4567"
