from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_ISDIR
from types import TracebackType

import pytest

from atlas_cli.durable_io import durable_replace as real_durable_replace
from atlas_cli.search_models import (
    NormalizedOffer,
    NormalizedPassengerPrice,
    NormalizedSearch,
    NormalizedSegment,
    SearchRequest,
)
from atlas_cli.search_store import SearchStore, SearchStoreError
from atlas_cli.secure_store import SearchSecrets, SecureRecordInvalidError, SecureStoreError


def request(origin: str = "KUL") -> SearchRequest:
    return SearchRequest(origin=origin, destination="SIN", depart="2026-08-10", adults=1)


def offer(
    identifier: str | None,
    total: float,
    *,
    bookable: bool = True,
    price_status: str = "current",
) -> NormalizedOffer:
    return NormalizedOffer(
        upstream_identifier=identifier,
        currency="USD",
        total_price=total,
        transaction_fee_total=5.0,
        passenger_prices=[
            NormalizedPassengerPrice(
                passenger_type="adult",
                count=1,
                base_fare_per_passenger=total - 25,
                tax_per_passenger=20,
                subtotal=total - 5,
            )
        ],
        segments=[
            NormalizedSegment(
                departure_airport="KUL",
                arrival_airport="SIN",
                departure_time="202608101000",
                arrival_time="202608101110",
                carrier="AK",
                flight_number="AK701",
                duration_minutes=70,
                cabin_class=1,
            )
        ],
        bookable=bookable,
        price_status=price_status,
    )


def search(*items: NormalizedOffer) -> NormalizedSearch:
    return NormalizedSearch(offers=list(items), request_id="safe-request")


def id_factory(values: list[str]):
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, duration: timedelta) -> None:
        self.value += duration


class FakeWorkflowSecretStore:
    def __init__(self) -> None:
        self.searches: dict[str, SearchSecrets] = {}
        self.events: list[str] = []
        self.fail_save = False
        self.fail_load = False
        self.fail_clear = False

    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None:
        self.events.append(f"save:{secret_ref}")
        if self.fail_save:
            raise SecureStoreError("private save failure")
        self.searches[secret_ref] = value

    def load_search_secrets(self, secret_ref: str) -> SearchSecrets | None:
        self.events.append(f"load:{secret_ref}")
        if self.fail_load:
            raise SecureRecordInvalidError("private load failure")
        return self.searches.get(secret_ref)

    def clear_search_secrets(self, secret_ref: str) -> None:
        self.events.append(f"clear:{secret_ref}")
        if self.fail_clear:
            raise SecureStoreError("private clear failure")
        self.searches.pop(secret_ref, None)


def test_save_assigns_opaque_ids_and_preserves_normalized_order(tmp_path: Path) -> None:
    store = SearchStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone", "offertwo"]),
    )

    stored = store.save(request(), search(offer("route-1", 100), offer("route-2", 90)), "g" * 24)

    assert stored.search_id == "srch_searchtoken"
    assert [item.offer_id for item in stored.offers] == ["off_offerone", "off_offertwo"]
    listed = store.list_offers(stored.search_id, generation="g" * 24)
    assert [item.offer.total_price for item in listed] == [100, 90]
    assert all("prod" not in item.offer_id and "fare" not in item.offer_id for item in listed)


def test_default_ids_are_random_prefixed_and_do_not_contain_route_text(tmp_path: Path) -> None:
    stored = SearchStore(tmp_path, secrets=FakeWorkflowSecretStore()).save(
        request(), search(offer("route-1", 100)), "g" * 24
    )

    assert stored.search_id.startswith("srch_")
    assert len(stored.search_id) == len("srch_") + 24
    assert stored.offers[0].offer_id.startswith("off_")
    assert len(stored.offers[0].offer_id) == len("off_") + 24


def test_persisted_state_contains_no_access_or_routing_configuration(tmp_path: Path) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    stored = store.save(request(), search(offer("internal-booking-token", 100)), "g" * 24)

    serialized = (tmp_path / "searches.json").read_text(encoding="utf-8")

    for forbidden in (
        "client-" + "secret",
        "client-" + "id",
        "jwt-" + "value",
        "https://",
        "business.example.invalid",
        "credential_slot",
        "production",
        "fare_compare",
    ):
        assert forbidden not in serialized
    assert "internal-booking-token" not in serialized
    assert "upstream_identifier" not in serialized
    assert secrets.searches["ssec_secrettoken12"].offers == {stored.offers[0].offer_id: "internal-booking-token"}


def test_reference_offer_without_routing_secret_remains_listable(tmp_path: Path) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    stored = store.save(
        request(),
        search(offer(None, 100, bookable=False, price_status="reference")),
        "g" * 24,
    )

    listed = store.list_offers(stored.search_id, generation="g" * 24)

    assert listed[0].offer.upstream_identifier is None
    assert listed[0].offer.bookable is False
    assert secrets.searches["ssec_secrettoken12"].offers == {}


def test_current_nonbookable_offer_retains_routing_secret_for_later_verification(tmp_path: Path) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    stored = store.save(
        request(),
        search(offer("private-route", 100, bookable=False, price_status="current")),
        "g" * 24,
    )

    _, loaded = store.load_offer(stored.offers[0].offer_id, generation="g" * 24)

    assert loaded.offer.bookable is False
    assert loaded.offer.price_status == "current"
    assert loaded.offer.upstream_identifier == "private-route"
    assert secrets.searches["ssec_secrettoken12"].offers == {
        stored.offers[0].offer_id: "private-route"
    }
    assert "private-route" not in (tmp_path / "searches.json").read_text(encoding="utf-8")


def test_bookable_offer_is_hydrated_from_secure_record_only_in_memory(tmp_path: Path) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    stored = store.save(request(), search(offer("private-route", 100)), "g" * 24)

    _, loaded = store.load_offer(stored.offers[0].offer_id, generation="g" * 24)

    assert loaded.offer.upstream_identifier == "private-route"
    assert "private-route" not in (tmp_path / "searches.json").read_text(encoding="utf-8")


def test_secret_round_trip_precedes_public_json_commit(tmp_path: Path, monkeypatch) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    real_write = store._atomic_write

    def recording_write(state) -> None:
        secrets.events.append("json")
        real_write(state)

    monkeypatch.setattr(store, "_atomic_write", recording_write)

    store.save(request(), search(offer("private-route", 100)), "g" * 24)

    assert secrets.events == [
        "save:ssec_secrettoken12",
        "load:ssec_secrettoken12",
        "json",
    ]


def test_secret_failure_preserves_previous_public_json_bytes(tmp_path: Path) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchone", "secretone000", "offerone", "searchtwo", "secrettwo000", "offertwo"]),
    )
    store.save(request(), search(offer("route-one", 100)), "g" * 24)
    original = (tmp_path / "searches.json").read_bytes()
    secrets.fail_save = True

    with pytest.raises(SecureStoreError):
        store.save(request("PEN"), search(offer("route-two", 90)), "g" * 24)

    assert (tmp_path / "searches.json").read_bytes() == original


def test_public_json_failure_clears_new_secret_best_effort(tmp_path: Path, monkeypatch) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )

    def fail_write(state) -> None:
        raise OSError("private json failure")

    monkeypatch.setattr(store, "_atomic_write", fail_write)

    with pytest.raises(OSError, match="private json failure"):
        store.save(request(), search(offer("private-route", 100)), "g" * 24)

    assert secrets.searches == {}
    assert secrets.events[-1] == "clear:ssec_secrettoken12"


def test_history_eviction_clears_only_evicted_secret_after_json_commit(tmp_path: Path, monkeypatch) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchone", "secretone000", "offerone", "searchtwo", "secrettwo000", "offertwo"]),
        history_limit=1,
    )
    real_write = store._atomic_write

    def recording_write(state) -> None:
        secrets.events.append("json")
        real_write(state)

    monkeypatch.setattr(store, "_atomic_write", recording_write)
    store.save(request(), search(offer("route-one", 100)), "g" * 24)
    secrets.events.clear()

    store.save(request("PEN"), search(offer("route-two", 90)), "g" * 24)

    assert secrets.events == [
        "save:ssec_secrettwo000",
        "load:ssec_secrettwo000",
        "json",
        "clear:ssec_secretone000",
    ]
    assert set(secrets.searches) == {"ssec_secrettwo000"}


class RecordingLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> RecordingLock:
        self.events.append("lock_entered")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.events.append("lock_exited")


def test_write_uses_lock_fsync_and_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    real_fsync = __import__("os").fsync
    monkeypatch.setattr(
        "atlas_cli.search_store.portalocker.Lock",
        lambda *args, **kwargs: RecordingLock(events),
    )

    def recording_fsync(descriptor: int) -> None:
        if not S_ISDIR(__import__("os").fstat(descriptor).st_mode):
            events.append("fsynced")
        real_fsync(descriptor)

    def recording_replace(source: Path, destination: Path) -> None:
        events.append("replaced")
        real_durable_replace(source, destination)

    monkeypatch.setattr("atlas_cli.search_store.os.fsync", recording_fsync)
    monkeypatch.setattr("atlas_cli.search_store.durable_replace", recording_replace)
    store = SearchStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )

    store.save(request(), search(offer("route-1", 100)), "g" * 24)

    assert events == ["lock_entered", "fsynced", "replaced", "lock_exited"]
    assert list(tmp_path.glob("*.tmp")) == []


def test_generation_mismatch_returns_typed_stale_offer_condition(tmp_path: Path) -> None:
    store = SearchStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    stored = store.save(request(), search(offer("route-1", 100)), "a" * 24)

    with pytest.raises(SearchStoreError) as raised:
        store.load_search(stored.search_id, generation="b" * 24)

    assert raised.value.code == "OFFER_EXPIRED"
    assert str(raised.value) == "Offer expired; search again"


@pytest.mark.parametrize("failure", ["missing", "corrupt", "mismatched"])
def test_invalid_secure_binding_returns_neutral_expired_condition(tmp_path: Path, failure: str) -> None:
    secrets = FakeWorkflowSecretStore()
    store = SearchStore(
        tmp_path,
        secrets=secrets,
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    stored = store.save(request(), search(offer("never-expose-this-route", 100)), "g" * 24)
    if failure == "missing":
        secrets.searches.clear()
    elif failure == "corrupt":
        secrets.fail_load = True
    else:
        secrets.searches["ssec_secrettoken12"] = SearchSecrets(
            search_id="srch_different",
            generation="g" * 24,
            offers={stored.offers[0].offer_id: "never-expose-this-route"},
        )

    with pytest.raises(SearchStoreError) as raised:
        store.load_offer(stored.offers[0].offer_id, generation="g" * 24)

    assert raised.value.code == "OFFER_EXPIRED"
    assert "never-expose-this-route" not in str(raised.value)


def test_store_loads_offer_with_its_search_request(tmp_path: Path) -> None:
    store = SearchStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
    )
    saved = store.save(request(), search(offer("route-1", 100)), "g" * 24)

    stored_search, stored_offer = store.load_offer(saved.offers[0].offer_id, generation="g" * 24)

    assert stored_search.request == request()
    assert stored_offer.offer_id == saved.offers[0].offer_id


def test_store_rejects_offer_older_than_six_hours(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
    store = SearchStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=id_factory(["searchtoken", "secrettoken12", "offerone"]),
        now=clock.now,
    )
    saved = store.save(request(), search(offer("route-1", 100)), "g" * 24)
    clock.advance(timedelta(hours=6, microseconds=1))

    with pytest.raises(SearchStoreError) as raised:
        store.load_offer(saved.offers[0].offer_id, generation="g" * 24)

    assert raised.value.code == "OFFER_EXPIRED"


def test_replay_returns_latest_original_validated_request(tmp_path: Path) -> None:
    store = SearchStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=id_factory(["searchone", "secretone000", "offerone", "searchtwo", "secrettwo000", "offertwo"]),
    )
    store.save(request("KUL"), search(offer("route-1", 100)), "g" * 24)
    latest = store.save(request("PEN"), search(offer("route-2", 90)), "g" * 24)

    assert store.replay_request() == request("PEN")
    assert store.replay_request(latest.search_id) == request("PEN")


def test_missing_replay_and_search_use_stable_expired_condition(tmp_path: Path) -> None:
    store = SearchStore(tmp_path, secrets=FakeWorkflowSecretStore())

    with pytest.raises(SearchStoreError) as replay_error:
        store.replay_request()
    with pytest.raises(SearchStoreError) as search_error:
        store.load_search("srch_missing", generation="g" * 24)

    assert replay_error.value.code == "OFFER_EXPIRED"
    assert search_error.value.code == "OFFER_EXPIRED"


@pytest.mark.parametrize("malformed", ["not-json", "[]"])
def test_malformed_state_fails_closed_without_leaking_content(tmp_path: Path, malformed: str) -> None:
    (tmp_path / "searches.json").write_text(malformed, encoding="utf-8")
    store = SearchStore(tmp_path, secrets=FakeWorkflowSecretStore())

    with pytest.raises(SearchStoreError) as raised:
        store.replay_request()

    assert raised.value.code == "SEARCH_STATE_INVALID"
    assert str(raised.value) == "Saved searches could not be processed"
    assert malformed not in str(raised.value)


def test_legacy_state_is_cleared_and_returns_expired(tmp_path: Path) -> None:
    legacy = '{"schema_version":"1","searches":"private"}'
    (tmp_path / "searches.json").write_text(legacy, encoding="utf-8")
    store = SearchStore(tmp_path, secrets=FakeWorkflowSecretStore())

    with pytest.raises(SearchStoreError) as raised:
        store.replay_request()

    assert raised.value.code == "OFFER_EXPIRED"
    assert json.loads((tmp_path / "searches.json").read_text(encoding="utf-8")) == {
        "schema_version": "2",
        "searches": [],
    }


def test_restricted_key_nested_in_schema_two_is_cleared_and_returns_expired(tmp_path: Path) -> None:
    restricted = {
        "schema_version": "2",
        "searches": [{"nested": {"product_code": "private-product"}}],
    }
    (tmp_path / "searches.json").write_text(json.dumps(restricted), encoding="utf-8")
    store = SearchStore(tmp_path, secrets=FakeWorkflowSecretStore())

    with pytest.raises(SearchStoreError) as raised:
        store.replay_request()

    assert raised.value.code == "OFFER_EXPIRED"
    assert json.loads((tmp_path / "searches.json").read_text(encoding="utf-8")) == {
        "schema_version": "2",
        "searches": [],
    }


def test_store_retains_only_bounded_recent_history(tmp_path: Path) -> None:
    values = [value for index in range(25) for value in (f"search{index}", f"secret{index:06d}", f"offer{index}")]
    store = SearchStore(
        tmp_path,
        secrets=FakeWorkflowSecretStore(),
        token_factory=id_factory(values),
        history_limit=20,
    )

    for index in range(25):
        store.save(request(), search(offer(f"route-{index}", 100)), "g" * 24)

    data = json.loads((tmp_path / "searches.json").read_text(encoding="utf-8"))
    assert len(data["searches"]) == 20
