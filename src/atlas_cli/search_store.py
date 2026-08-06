"""Atomic, non-secret persistence for normalized Atlas searches."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Literal, NoReturn

import portalocker
from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from atlas_cli.durable_io import durable_replace
from atlas_cli.search_models import (
    NormalizedOffer,
    NormalizedPassengerPrice,
    NormalizedSearch,
    NormalizedSegment,
    SearchRequest,
)
from atlas_cli.secure_store import (
    SearchSecrets,
    SecureRecordInvalidError,
    SecureStoreError,
    WorkflowSecretStore,
)

_RESTRICTED_KEYS = {
    "upstream_identifier",
    "routingIdentifier",
    "sessionId",
    "productCode",
    "product_code",
}


class StoredModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StoredOffer(StoredModel):
    offer_id: str
    offer: NormalizedOffer


class StoredSearch(StoredModel):
    search_id: str
    request: SearchRequest
    offers: list[StoredOffer]
    route_generation: str = Field(repr=False)
    reason: str | None = None
    recent_flight_dates: list[str] = Field(default_factory=list)
    request_id: str | None = None
    created_at: datetime


class PersistedOffer(StoredModel):
    offer_id: str
    currency: str
    total_price: float
    transaction_fee_total: float
    passenger_prices: list[NormalizedPassengerPrice]
    segments: list[NormalizedSegment]
    ancillary_supported: tuple[Literal["baggage", "seat"], ...]
    bookable: bool
    price_status: Literal["reference", "current", "verified"]
    refresh_time: str | None = None
    expire_time: str | None = None

    @classmethod
    def from_stored(cls, stored: StoredOffer) -> PersistedOffer:
        offer = stored.offer
        return cls(
            offer_id=stored.offer_id,
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

    def to_stored(self, routing_identifier: str | None) -> StoredOffer:
        return StoredOffer(
            offer_id=self.offer_id,
            offer=NormalizedOffer(
                upstream_identifier=routing_identifier,
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
            ),
        )


class PersistedSearch(StoredModel):
    search_id: str
    secret_ref: str
    request: SearchRequest
    offers: list[PersistedOffer]
    route_generation: str
    reason: str | None = None
    recent_flight_dates: list[str] = Field(default_factory=list)
    request_id: str | None = None
    created_at: datetime


class SearchState(StoredModel):
    schema_version: Literal["2"] = "2"
    searches: list[PersistedSearch] = Field(default_factory=list)


class SearchStoreError(RuntimeError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _new_token() -> str:
    return token_hex(12)


def _now() -> datetime:
    return datetime.now(UTC)


class SearchStore:
    def __init__(
        self,
        directory: Path | None = None,
        *,
        secrets: WorkflowSecretStore,
        token_factory: Callable[[], str] = _new_token,
        now: Callable[[], datetime] = _now,
        history_limit: int = 20,
    ) -> None:
        self.directory = directory or Path(user_data_path("atlas-flight-booking"))
        self.searches_file = self.directory / "searches.json"
        self._lock_file = self.directory / "searches.lock"
        self._secrets = secrets
        self._token_factory = token_factory
        self._now = now
        self._history_limit = max(1, history_limit)

    def save(
        self,
        request: SearchRequest,
        search: NormalizedSearch,
        route_generation: str,
    ) -> StoredSearch:
        self.directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self._lock_file), mode="a", timeout=10):
            state = self._read()
            search_id = f"srch_{self._token_factory()}"
            secret_ref = f"ssec_{self._token_factory()}"
            stored = StoredSearch(
                search_id=search_id,
                request=request,
                offers=[StoredOffer(offer_id=f"off_{self._token_factory()}", offer=offer) for offer in search.offers],
                route_generation=route_generation,
                reason=search.reason,
                recent_flight_dates=search.recent_flight_dates,
                request_id=search.request_id,
                created_at=self._now(),
            )
            workflow_secret = SearchSecrets(
                search_id=search_id,
                generation=route_generation,
                offers={
                    item.offer_id: item.offer.upstream_identifier
                    for item in stored.offers
                    if item.offer.bookable and item.offer.upstream_identifier is not None
                },
            )
            self._save_and_validate_secret(secret_ref, workflow_secret)
            persisted = self._to_persisted(stored, secret_ref)
            searches = [*state.searches, persisted][-self._history_limit :]
            retained_refs = {item.secret_ref for item in searches}
            evicted_refs = [item.secret_ref for item in state.searches if item.secret_ref not in retained_refs]
            try:
                self._atomic_write(SearchState(searches=searches))
            except Exception:
                self._clear_secret_best_effort(secret_ref)
                raise
            for evicted_ref in evicted_refs:
                self._clear_secret_best_effort(evicted_ref)
            return stored

    def load_search(self, search_id: str, *, generation: str) -> StoredSearch:
        self.directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self._lock_file), mode="a", timeout=10):
            persisted = self._find(self._read(), search_id)
            if persisted.route_generation != generation:
                self._raise_expired()
            return self._hydrate(persisted)

    def list_offers(self, search_id: str, *, generation: str) -> list[StoredOffer]:
        return list(self.load_search(search_id, generation=generation).offers)

    def load_offer(
        self,
        offer_id: str,
        *,
        generation: str,
        max_age: timedelta = timedelta(hours=6),
    ) -> tuple[StoredSearch, StoredOffer]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self._lock_file), mode="a", timeout=10):
            state = self._read()
            for persisted in reversed(state.searches):
                if persisted.route_generation != generation:
                    continue
                if self._now() - persisted.created_at > max_age:
                    continue
                if any(offer.offer_id == offer_id for offer in persisted.offers):
                    stored = self._hydrate(persisted)
                    for offer in stored.offers:
                        if offer.offer_id == offer_id:
                            return stored, offer
        self._raise_expired()

    def replay_request(self, search_id: str | None = None) -> SearchRequest:
        self.directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self._lock_file), mode="a", timeout=10):
            state = self._read()
            if search_id is None:
                if not state.searches:
                    self._raise_expired()
                return state.searches[-1].request
            return self._find(state, search_id).request

    @staticmethod
    def _find(state: SearchState, search_id: str) -> PersistedSearch:
        for persisted in reversed(state.searches):
            if persisted.search_id == search_id:
                return persisted
        SearchStore._raise_expired()

    @staticmethod
    def _to_persisted(stored: StoredSearch, secret_ref: str) -> PersistedSearch:
        return PersistedSearch(
            search_id=stored.search_id,
            secret_ref=secret_ref,
            request=stored.request,
            offers=[PersistedOffer.from_stored(offer) for offer in stored.offers],
            route_generation=stored.route_generation,
            reason=stored.reason,
            recent_flight_dates=stored.recent_flight_dates,
            request_id=stored.request_id,
            created_at=stored.created_at,
        )

    def _hydrate(self, persisted: PersistedSearch) -> StoredSearch:
        try:
            secret = self._secrets.load_search_secrets(persisted.secret_ref)
        except SecureRecordInvalidError:
            self._raise_expired()
        if secret is None:
            self._raise_expired()
        expected_keys = {offer.offer_id for offer in persisted.offers if offer.bookable}
        if (
            secret.search_id != persisted.search_id
            or secret.generation != persisted.route_generation
            or set(secret.offers) != expected_keys
            or any(not value for value in secret.offers.values())
        ):
            self._raise_expired()
        return StoredSearch(
            search_id=persisted.search_id,
            request=persisted.request,
            offers=[offer.to_stored(secret.offers.get(offer.offer_id)) for offer in persisted.offers],
            route_generation=persisted.route_generation,
            reason=persisted.reason,
            recent_flight_dates=persisted.recent_flight_dates,
            request_id=persisted.request_id,
            created_at=persisted.created_at,
        )

    def _save_and_validate_secret(self, secret_ref: str, value: SearchSecrets) -> None:
        try:
            self._secrets.save_search_secrets(secret_ref, value)
            if self._secrets.load_search_secrets(secret_ref) != value:
                raise SecureStoreError("Secure credential storage is unavailable")
        except SecureStoreError:
            self._clear_secret_best_effort(secret_ref)
            raise

    def _clear_secret_best_effort(self, secret_ref: str) -> None:
        with suppress(SecureStoreError):
            self._secrets.clear_search_secrets(secret_ref)

    def _read(self) -> SearchState:
        if not self.searches_file.exists():
            return SearchState()
        try:
            loaded = json.loads(self.searches_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._raise_state_invalid()
        if isinstance(loaded, dict) and (loaded.get("schema_version") != "2" or self._contains_restricted_key(loaded)):
            self._atomic_write(SearchState())
            self._raise_expired()
        try:
            return SearchState.model_validate(loaded)
        except ValidationError:
            self._raise_state_invalid()

    @classmethod
    def _contains_restricted_key(cls, value: object) -> bool:
        if isinstance(value, dict):
            return any(key in _RESTRICTED_KEYS or cls._contains_restricted_key(child) for key, child in value.items())
        if isinstance(value, list):
            return any(cls._contains_restricted_key(child) for child in value)
        return False

    def _atomic_write(self, state: SearchState) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".searches-",
                suffix=".tmp",
                dir=self.directory,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    state.model_dump(mode="json"),
                    temporary,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            durable_replace(temporary_path, self.searches_file)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _raise_expired() -> NoReturn:
        raise SearchStoreError(code="OFFER_EXPIRED", message="Offer expired; search again")

    @staticmethod
    def _raise_state_invalid() -> NoReturn:
        raise SearchStoreError(
            code="SEARCH_STATE_INVALID",
            message="Saved searches could not be processed",
        )
