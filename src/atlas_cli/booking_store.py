"""Atomic, secret-free persistence for Atlas booking workflow state."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Literal, NoReturn, cast

import portalocker
from platformdirs import user_data_path
from pydantic import ValidationError

from atlas_cli.booking_models import (
    AncillaryKind,
    AncillarySelection,
    BaggageOption,
    BookingContext,
    OrderAttemptState,
    OrderState,
    PaymentConfirmation,
    PaymentConfirmationSeed,
    PaymentState,
    SeatOption,
    TicketingState,
    VerifiedBookingSeed,
)
from atlas_cli.booking_persistence import (
    BookingProjectionError,
    PersistedBookingContext,
    PersistedBookingState,
    hydrate_booking_context,
    project_booking_context,
    restore_terminal_booking_context,
)
from atlas_cli.durable_io import durable_replace
from atlas_cli.secure_store import (
    BookingSecrets,
    SecureRecordInvalidError,
    SecureStoreError,
    WorkflowSecretStore,
)

_RESTRICTED_KEYS = {
    "routing_identifier",
    "routingIdentifier",
    "upstream_identifier",
    "session_id",
    "sessionId",
    "product_code",
    "productCode",
}


class BookingStoreError(RuntimeError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _new_token() -> str:
    return token_hex(12)


def _now() -> datetime:
    return datetime.now(UTC)


class BookingStore:
    def __init__(
        self,
        directory: Path | None = None,
        *,
        secrets: WorkflowSecretStore,
        token_factory: Callable[[], str] = _new_token,
        workflow_token_factory: Callable[[], str] = _new_token,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.directory = directory or Path(user_data_path("atlas-flight-booking"))
        self.contexts_file = self.directory / "contexts.json"
        self._lock_file = self.directory / "contexts.lock"
        self._secrets = secrets
        self._token_factory = token_factory
        self._workflow_token_factory = workflow_token_factory
        self._now = now

    def create_from_verified(self, seed: VerifiedBookingSeed) -> BookingContext:
        timestamp = self._now()
        price_change: Literal["unchanged", "decreased", "increased"]
        if seed.verified_offer.total_price > seed.searched_offer.total_price:
            price_change = "increased"
        elif seed.verified_offer.total_price < seed.searched_offer.total_price:
            price_change = "decreased"
        else:
            price_change = "unchanged"
        common = set(seed.searched_offer.ancillary_supported).intersection(seed.verified_offer.ancillary_supported)
        with self._lock():
            state = self._read()
            booking_id = f"book_{self._token_factory()}"
            secret_ref = f"bsec_{self._workflow_token_factory()}"
            revision = f"rev_{self._workflow_token_factory()}"
            context = BookingContext(
                booking_id=booking_id,
                search_id=seed.search_id,
                offer_id=seed.offer_id,
                route_generation=seed.route_generation,
                secret_ref=secret_ref,
                secret_revision=revision,
                session_id=seed.session_id,
                searched_offer=seed.searched_offer.model_copy(update={"upstream_identifier": None}),
                verified_offer=seed.verified_offer.model_copy(update={"upstream_identifier": None}),
                price_change=price_change,
                requirements=seed.requirements,
                travelers=seed.travelers,
                segments=seed.segments,
                baggage_supported="baggage" in common,
                seat_supported="seat" in common,
                created_at=timestamp,
                updated_at=timestamp,
                expires_at=seed.expires_at,
            )
            secret = BookingSecrets(
                booking_id=booking_id,
                generation=seed.route_generation,
                revision=revision,
                session_id=seed.session_id,
                products={},
            )
            self._save_and_validate_secret(secret_ref, revision, secret)
            updated = state.model_copy(update={"contexts": (*state.contexts, project_booking_context(context))})
            try:
                self._atomic_write(updated)
            except Exception:
                self._clear_secret_best_effort(secret_ref, revision)
                raise
            return context

    def load(self, booking_id: str, *, generation: str) -> BookingContext:
        with self._lock():
            state = self._read()
            try:
                _, persisted = self._find_context(state, booking_id)
            except BookingStoreError:
                self._raise_expired()
            if persisted.route_generation != generation:
                self._raise_expired()
            if persisted.is_terminal():
                try:
                    return restore_terminal_booking_context(persisted)
                except BookingProjectionError:
                    self._raise_expired()
            if self._now() >= persisted.expires_at:
                self._raise_expired()
            return self._hydrate(persisted)

    def confirm_price(self, booking_id: str, *, generation: str) -> BookingContext:
        def confirm(context: BookingContext) -> BookingContext:
            self._require_ready(context)
            return context.model_copy(update={"increased_price_confirmed": True, "updated_at": self._now()})

        return self._mutate_active(booking_id, generation, confirm)

    def replace_options(
        self,
        booking_id: str,
        *,
        kind: AncillaryKind,
        options: tuple[BaggageOption | SeatOption, ...],
        generation: str,
    ) -> BookingContext:
        def replace(context: BookingContext, secret: BookingSecrets) -> tuple[BookingContext, dict[str, str]]:
            del secret
            self._require_ready(context)
            selections = tuple(item for item in context.selections if item.kind is not kind)
            if kind is AncillaryKind.BAGGAGE:
                if any(not isinstance(item, BaggageOption) for item in options):
                    self._raise_ancillary_selection_invalid()
                update: dict[str, object] = {
                    "baggage_options": cast(tuple[BaggageOption, ...], options),
                    "selections": selections,
                    "updated_at": self._now(),
                }
            else:
                if any(not isinstance(item, SeatOption) for item in options):
                    self._raise_ancillary_selection_invalid()
                update = {
                    "seat_options": cast(tuple[SeatOption, ...], options),
                    "selections": selections,
                    "updated_at": self._now(),
                }
            updated = context.model_copy(update=update)
            return updated, self._products(updated)

        return self._mutate_secrets(booking_id, generation, replace)

    def close_ancillary(self, booking_id: str, *, kind: AncillaryKind, generation: str) -> BookingContext:
        def close(context: BookingContext, secret: BookingSecrets) -> tuple[BookingContext, dict[str, str]]:
            del secret
            self._require_ready(context)
            selections = tuple(item for item in context.selections if item.kind is not kind)
            if kind is AncillaryKind.BAGGAGE:
                update: dict[str, object] = {
                    "baggage_supported": False,
                    "baggage_options": (),
                    "selections": selections,
                    "updated_at": self._now(),
                }
            else:
                update = {
                    "seat_supported": False,
                    "seat_options": (),
                    "selections": selections,
                    "updated_at": self._now(),
                }
            updated = context.model_copy(update=update)
            return updated, self._products(updated)

        return self._mutate_secrets(booking_id, generation, close)

    def select(self, booking_id: str, selection: AncillarySelection, *, generation: str) -> BookingContext:
        def select_one(context: BookingContext) -> BookingContext:
            self._require_ready(context)
            self._require_current_selection(context, selection)
            remaining = tuple(
                item
                for item in context.selections
                if not (
                    item.kind is selection.kind
                    and item.traveler_id == selection.traveler_id
                    and item.segment_id == selection.segment_id
                )
            )
            return context.model_copy(update={"selections": (*remaining, selection), "updated_at": self._now()})

        return self._mutate_active(booking_id, generation, select_one)

    def remove(
        self,
        booking_id: str,
        *,
        kind: AncillaryKind,
        traveler_id: str,
        segment_id: str,
        generation: str,
    ) -> BookingContext:
        def remove_one(context: BookingContext) -> BookingContext:
            self._require_ready(context)
            selections = tuple(
                item
                for item in context.selections
                if not (item.kind is kind and item.traveler_id == traveler_id and item.segment_id == segment_id)
            )
            return context.model_copy(update={"selections": selections, "updated_at": self._now()})

        return self._mutate_active(booking_id, generation, remove_one)

    def expire_context(self, booking_id: str, *, generation: str) -> BookingContext:
        with self._lock():
            state = self._read()
            index, context = self._active_context(state, booking_id, generation=generation)
            timestamp = self._now()
            updated = context.model_copy(
                update={
                    "session_id": None,
                    "baggage_options": (),
                    "seat_options": (),
                    "selections": (),
                    "expires_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            next_state = self._replace_context(state, index, project_booking_context(updated))
            self._atomic_write(next_state)
            self._clear_secret_best_effort(context.secret_ref, context.secret_revision)
            return updated

    def begin_order(self, booking_id: str, *, generation: str) -> BookingContext:
        def begin(context: BookingContext) -> BookingContext:
            self._require_ready(context)
            if context.price_change == "increased" and not context.increased_price_confirmed:
                raise BookingStoreError(
                    code="PRICE_CONFIRMATION_REQUIRED",
                    message="Confirm the increased price before creating an order",
                )
            return context.model_copy(
                update={"order_attempt_state": OrderAttemptState.CREATING, "updated_at": self._now()}
            )

        return self._mutate_active(booking_id, generation, begin)

    def reset_order_attempt(self, booking_id: str, *, generation: str) -> BookingContext:
        del generation
        with self._lock():
            state = self._read()
            index, persisted = self._find_context(state, booking_id)
            context = self._hydrate(persisted)
            if context.order_attempt_state is not OrderAttemptState.CREATING or context.order is not None:
                self._raise_order_state_invalid()
            updated = context.model_copy(
                update={"order_attempt_state": OrderAttemptState.READY, "updated_at": self._now()}
            )
            self._atomic_write(self._replace_context(state, index, project_booking_context(updated)))
            return updated

    def save_order(self, booking_id: str, order: OrderState, *, generation: str) -> BookingContext:
        del generation
        context, _ = self._finish_order(booking_id, order=order)
        return context

    def save_order_with_confirmation(
        self,
        booking_id: str,
        order: OrderState,
        seed: PaymentConfirmationSeed,
        *,
        generation: str,
    ) -> tuple[BookingContext, PaymentConfirmation]:
        del generation
        context, confirmation = self._finish_order(booking_id, order=order, confirmation_seed=seed)
        assert confirmation is not None
        return context, confirmation

    def mark_order_unknown(self, booking_id: str, *, generation: str) -> BookingContext:
        del generation
        context, _ = self._finish_order(booking_id, unknown=True)
        return context

    def issue_payment_confirmation(self, booking_id: str, seed: PaymentConfirmationSeed) -> PaymentConfirmation:
        with self._lock():
            state = self._read()
            index, context = self._find_context(state, booking_id)
            if context.order is None:
                self._raise_payment_confirmation_invalid()
            self._require_seed_bound_to_order(seed, context.order)
            if context.order.payment_state is not PaymentState.AWAITING_CONFIRMATION:
                self._raise_payment_confirmation_invalid()
            confirmation = self._new_confirmation(seed)
            updated = context.model_copy(update={"updated_at": self._now()})
            replaced = self._replace_context(state, index, updated)
            self._atomic_write(replaced.model_copy(update={"confirmations": (*replaced.confirmations, confirmation)}))
            return confirmation

    def consume_payment_confirmation(self, confirmation_id: str, *, now: datetime) -> OrderState:
        with self._lock():
            state = self._read()
            confirmation_index, confirmation = self._find_confirmation(state, confirmation_id)
            if confirmation.consumed_at is not None or now >= confirmation.expires_at:
                self._raise_payment_confirmation_invalid()
            context_index, context = self._find_order_context(state, confirmation.order_no)
            order = context.order
            if (
                order is None
                or order.summary_digest != confirmation.summary_digest
                or now >= order.payment_deadline
                or order.payment_state is not PaymentState.AWAITING_CONFIRMATION
            ):
                self._raise_payment_confirmation_invalid()
            updated_order = order.model_copy(update={"payment_state": PaymentState.PAYING})
            updated_context = context.model_copy(update={"order": updated_order, "updated_at": self._now()})
            contexts = list(state.contexts)
            contexts[context_index] = updated_context
            confirmations = list(state.confirmations)
            confirmations[confirmation_index] = confirmation.model_copy(update={"consumed_at": now})
            self._atomic_write(
                state.model_copy(update={"contexts": tuple(contexts), "confirmations": tuple(confirmations)})
            )
            return updated_order

    def update_payment(self, order_no: str, state: PaymentState) -> OrderState:
        with self._lock():
            saved = self._read()
            index, context = self._find_order_context(saved, order_no)
            assert context.order is not None
            order = context.order.model_copy(update={"payment_state": state})
            updated = context.model_copy(update={"order": order, "updated_at": self._now()})
            self._atomic_write(self._replace_context(saved, index, updated))
            return order

    def update_ticketing(
        self,
        order_no: str,
        state: TicketingState,
        *,
        airline_pnrs: tuple[str, ...] = (),
        ticket_numbers: tuple[str, ...] = (),
    ) -> OrderState:
        with self._lock():
            saved = self._read()
            index, context = self._find_order_context(saved, order_no)
            assert context.order is not None
            order = context.order.model_copy(
                update={
                    "ticketing_state": state,
                    "airline_pnrs": airline_pnrs,
                    "ticket_numbers": ticket_numbers,
                }
            )
            updated = context.model_copy(update={"order": order, "updated_at": self._now()})
            self._atomic_write(self._replace_context(saved, index, updated))
            return order

    def load_order(self, order_no: str) -> OrderState:
        with self._lock():
            _, context = self._find_order_context(self._read(), order_no)
            assert context.order is not None
            return context.order

    def _mutate_active(
        self,
        booking_id: str,
        generation: str,
        operation: Callable[[BookingContext], BookingContext],
    ) -> BookingContext:
        with self._lock():
            state = self._read()
            index, context = self._active_context(state, booking_id, generation=generation)
            updated = operation(context)
            self._atomic_write(self._replace_context(state, index, project_booking_context(updated)))
            return updated

    def _mutate_secrets(
        self,
        booking_id: str,
        generation: str,
        operation: Callable[[BookingContext, BookingSecrets], tuple[BookingContext, dict[str, str]]],
    ) -> BookingContext:
        with self._lock():
            state = self._read()
            index, persisted = self._find_context(state, booking_id)
            context, current_secret = self._active_context_with_secret(persisted, generation=generation)
            updated, products = operation(context, current_secret)
            revision = f"rev_{self._workflow_token_factory()}"
            new_secret = BookingSecrets(
                booking_id=context.booking_id,
                generation=context.route_generation,
                revision=revision,
                session_id=current_secret.session_id,
                products=products,
            )
            self._save_and_validate_secret(context.secret_ref, revision, new_secret)
            updated = updated.model_copy(update={"secret_revision": revision})
            try:
                self._atomic_write(self._replace_context(state, index, project_booking_context(updated)))
            except Exception:
                self._clear_secret_best_effort(context.secret_ref, revision)
                raise
            self._clear_secret_best_effort(context.secret_ref, context.secret_revision)
            return updated

    def _finish_order(
        self,
        booking_id: str,
        *,
        order: OrderState | None = None,
        confirmation_seed: PaymentConfirmationSeed | None = None,
        unknown: bool = False,
    ) -> tuple[BookingContext, PaymentConfirmation | None]:
        with self._lock():
            state = self._read()
            index, context = self._find_context(state, booking_id)
            if context.order_attempt_state is not OrderAttemptState.CREATING or context.order is not None:
                self._raise_order_state_invalid()
            validated = self._validated_order(order) if order is not None else None
            confirmation: PaymentConfirmation | None = None
            if confirmation_seed is not None:
                if validated is None:
                    self._raise_payment_confirmation_invalid()
                self._require_seed_bound_to_order(confirmation_seed, validated)
                confirmation = self._new_confirmation(confirmation_seed)
            updated = context.model_copy(
                update={
                    "order_attempt_state": (OrderAttemptState.UNKNOWN if unknown else OrderAttemptState.CREATED),
                    "order": validated,
                    "baggage_options": (),
                    "seat_options": (),
                    "selections": (),
                    "updated_at": self._now(),
                }
            )
            next_state = self._replace_context(state, index, updated)
            if confirmation is not None:
                next_state = next_state.model_copy(update={"confirmations": (*next_state.confirmations, confirmation)})
            self._atomic_write(next_state)
            self._clear_secret_best_effort(context.secret_ref, context.secret_revision)
            return restore_terminal_booking_context(updated), confirmation

    def _active_context(
        self, state: PersistedBookingState, booking_id: str, *, generation: str
    ) -> tuple[int, BookingContext]:
        try:
            index, persisted = self._find_context(state, booking_id)
        except BookingStoreError:
            self._raise_expired()
        if persisted.route_generation != generation or self._now() >= persisted.expires_at:
            self._raise_expired()
        if persisted.is_terminal():
            self._raise_order_state_invalid()
        return index, self._hydrate(persisted)

    def _active_context_with_secret(
        self, persisted: PersistedBookingContext, *, generation: str
    ) -> tuple[BookingContext, BookingSecrets]:
        if persisted.route_generation != generation or self._now() >= persisted.expires_at:
            self._raise_expired()
        secret = self._load_secret(persisted)
        try:
            return hydrate_booking_context(persisted, secret), secret
        except BookingProjectionError:
            self._raise_expired()

    def _hydrate(self, persisted: PersistedBookingContext) -> BookingContext:
        try:
            return hydrate_booking_context(persisted, self._load_secret(persisted))
        except BookingProjectionError:
            self._raise_expired()

    def _load_secret(self, persisted: PersistedBookingContext) -> BookingSecrets:
        try:
            secret = self._secrets.load_booking_secrets(persisted.secret_ref, persisted.secret_revision)
        except SecureRecordInvalidError:
            self._raise_expired()
        if secret is None:
            self._raise_expired()
        return secret

    def _save_and_validate_secret(self, secret_ref: str, revision: str, value: BookingSecrets) -> None:
        try:
            self._secrets.save_booking_secrets(secret_ref, revision, value)
            if self._secrets.load_booking_secrets(secret_ref, revision) != value:
                raise SecureStoreError("Secure credential storage is unavailable")
        except SecureStoreError:
            self._clear_secret_best_effort(secret_ref, revision)
            raise

    def _clear_secret_best_effort(self, secret_ref: str, revision: str) -> None:
        with suppress(SecureStoreError):
            self._secrets.clear_booking_secrets(secret_ref, revision)

    @staticmethod
    def _products(context: BookingContext) -> dict[str, str]:
        return {
            **{item.baggage_id: item.product_code for item in context.baggage_options},
            **{item.seat_id: item.product_code for item in context.seat_options},
        }

    def _lock(self) -> portalocker.Lock:
        self._ensure_directory()
        return portalocker.Lock(str(self._lock_file), mode="a", timeout=10)

    def _ensure_directory(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._tighten_permissions(self.directory, 0o700)
        descriptor = os.open(self._lock_file, os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        self._tighten_permissions(self._lock_file, 0o600)

    def _read(self) -> PersistedBookingState:
        if not self.contexts_file.exists():
            return PersistedBookingState()
        try:
            self._tighten_permissions(self.contexts_file, 0o600)
            loaded = json.loads(self.contexts_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._raise_state_invalid()
        if isinstance(loaded, dict) and (loaded.get("schema_version") != "2" or self._contains_restricted_key(loaded)):
            self._atomic_write(PersistedBookingState())
            self._raise_expired()
        try:
            return PersistedBookingState.model_validate(loaded)
        except ValidationError:
            self._raise_state_invalid()

    @classmethod
    def _contains_restricted_key(cls, value: object) -> bool:
        if isinstance(value, dict):
            return any(key in _RESTRICTED_KEYS or cls._contains_restricted_key(child) for key, child in value.items())
        if isinstance(value, list):
            return any(cls._contains_restricted_key(child) for child in value)
        return False

    def _atomic_write(self, state: PersistedBookingState) -> None:
        temporary_path: Path | None = None
        target_mode = 0o600
        if self.contexts_file.exists() and os.name == "posix":
            target_mode = self.contexts_file.stat().st_mode & 0o600
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".contexts-",
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
            if os.name == "posix":
                os.chmod(temporary_path, target_mode)
            durable_replace(temporary_path, self.contexts_file)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _tighten_permissions(path: Path, allowed: int) -> None:
        if os.name == "posix":
            current = path.stat().st_mode & 0o777
            secured = current & allowed
            if secured != current:
                os.chmod(path, secured)

    @staticmethod
    def _find_context(state: PersistedBookingState, booking_id: str) -> tuple[int, PersistedBookingContext]:
        for index in range(len(state.contexts) - 1, -1, -1):
            context = state.contexts[index]
            if context.booking_id == booking_id:
                return index, context
        raise BookingStoreError(code="BOOKING_NOT_FOUND", message="Booking was not found")

    @classmethod
    def _find_order_context(cls, state: PersistedBookingState, order_no: str) -> tuple[int, PersistedBookingContext]:
        for index in range(len(state.contexts) - 1, -1, -1):
            context = state.contexts[index]
            if context.order is not None and context.order.order_no == order_no:
                return index, context
        raise BookingStoreError(code="ORDER_NOT_FOUND", message="Order was not found")

    @staticmethod
    def _find_confirmation(state: PersistedBookingState, confirmation_id: str) -> tuple[int, PaymentConfirmation]:
        for index in range(len(state.confirmations) - 1, -1, -1):
            confirmation = state.confirmations[index]
            if confirmation.confirmation_id == confirmation_id:
                return index, confirmation
        BookingStore._raise_payment_confirmation_invalid()

    @staticmethod
    def _replace_context(
        state: PersistedBookingState, index: int, context: PersistedBookingContext
    ) -> PersistedBookingState:
        contexts = list(state.contexts)
        contexts[index] = context
        return state.model_copy(update={"contexts": tuple(contexts)})

    @classmethod
    def _require_current_selection(cls, context: BookingContext, selection: AncillarySelection) -> None:
        supported = context.baggage_supported if selection.kind is AncillaryKind.BAGGAGE else context.seat_supported
        if not supported or not any(traveler.traveler_id == selection.traveler_id for traveler in context.travelers):
            cls._raise_ancillary_selection_invalid()
        segment = next((item for item in context.segments if item.segment_id == selection.segment_id), None)
        if segment is None or segment.segment_index != selection.segment_index:
            cls._raise_ancillary_selection_invalid()
        options = context.baggage_options if selection.kind is AncillaryKind.BAGGAGE else context.seat_options
        option = next(
            (
                item
                for item in options
                if getattr(item, "baggage_id", getattr(item, "seat_id", None)) == selection.option_id
            ),
            None,
        )
        if (
            option is None
            or option.product_code != selection.product_code
            or option.segment_id != selection.segment_id
            or option.segment_index != selection.segment_index
        ):
            cls._raise_ancillary_selection_invalid()
        if isinstance(option, BaggageOption):
            cls._require_connected_baggage_consistency(context, selection, option, segment.direction)

    @classmethod
    def _require_connected_baggage_consistency(
        cls,
        context: BookingContext,
        selection: AncillarySelection,
        option: BaggageOption,
        direction: str,
    ) -> None:
        connected_ids = {item.segment_id for item in context.segments if item.direction == direction}
        signature = (option.piece, option.weight_kg, option.size, option.category)
        options_by_id = {item.baggage_id: item for item in context.baggage_options}
        for existing in context.selections:
            if (
                existing.kind is not AncillaryKind.BAGGAGE
                or existing.traveler_id != selection.traveler_id
                or existing.segment_id == selection.segment_id
                or existing.segment_id not in connected_ids
            ):
                continue
            previous = options_by_id.get(existing.option_id)
            if (
                previous is None
                or previous.product_code != existing.product_code
                or previous.segment_id != existing.segment_id
                or previous.segment_index != existing.segment_index
                or (previous.piece, previous.weight_kg, previous.size, previous.category) != signature
            ):
                cls._raise_ancillary_selection_invalid()

    @staticmethod
    def _require_ready(context: BookingContext) -> None:
        if context.order_attempt_state is not OrderAttemptState.READY or context.order is not None:
            BookingStore._raise_order_state_invalid()

    def _new_confirmation(self, seed: PaymentConfirmationSeed) -> PaymentConfirmation:
        return PaymentConfirmation(
            confirmation_id=f"paycfm_{self._token_factory()}",
            order_no=seed.order_no,
            summary_digest=seed.summary_digest,
            expires_at=seed.expires_at,
        )

    @staticmethod
    def _validated_order(order: OrderState) -> OrderState:
        try:
            return OrderState.model_validate(order.model_dump(mode="python"))
        except ValidationError:
            raise BookingStoreError(
                code="BOOKING_INPUT_INVALID",
                message="Booking information could not be accepted",
            ) from None

    @staticmethod
    def _require_seed_bound_to_order(seed: PaymentConfirmationSeed, order: OrderState) -> None:
        if seed.order_no != order.order_no or seed.summary_digest != order.summary_digest:
            BookingStore._raise_payment_confirmation_invalid()

    @staticmethod
    def _raise_ancillary_selection_invalid() -> NoReturn:
        raise BookingStoreError(
            code="ANCILLARY_SELECTION_INVALID",
            message="Selected optional service is no longer available",
        )

    @staticmethod
    def _raise_expired() -> NoReturn:
        raise BookingStoreError(code="OFFER_EXPIRED", message="Offer expired; search again")

    @staticmethod
    def _raise_state_invalid() -> NoReturn:
        raise BookingStoreError(code="BOOKING_STATE_INVALID", message="Saved booking state could not be processed")

    @staticmethod
    def _raise_order_state_invalid() -> NoReturn:
        raise BookingStoreError(code="ORDER_STATE_INVALID", message="Order state does not allow this operation")

    @staticmethod
    def _raise_payment_confirmation_invalid() -> NoReturn:
        raise BookingStoreError(
            code="PAYMENT_CONFIRMATION_INVALID",
            message="Payment confirmation is invalid or expired",
        )
