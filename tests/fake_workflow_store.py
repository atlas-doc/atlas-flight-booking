from __future__ import annotations

from atlas_cli.secure_store import BookingSecrets, SearchSecrets, SecureStoreError


class FakeWorkflowSecretStore:
    def __init__(self) -> None:
        self.searches: dict[str, SearchSecrets] = {}
        self.bookings: dict[tuple[str, str], BookingSecrets] = {}
        self.events: list[str] = []
        self.fail_booking_save = False
        self.fail_booking_load = False
        self.fail_booking_clear = False

    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None:
        self.searches[secret_ref] = value

    def load_search_secrets(self, secret_ref: str) -> SearchSecrets | None:
        return self.searches.get(secret_ref)

    def clear_search_secrets(self, secret_ref: str) -> None:
        self.searches.pop(secret_ref, None)

    def save_booking_secrets(self, secret_ref: str, revision: str, value: BookingSecrets) -> None:
        self.events.append(f"save:{secret_ref}:{revision}")
        if self.fail_booking_save:
            raise SecureStoreError("private booking save failure")
        self.bookings[(secret_ref, revision)] = value

    def load_booking_secrets(self, secret_ref: str, revision: str) -> BookingSecrets | None:
        self.events.append(f"load:{secret_ref}:{revision}")
        if self.fail_booking_load:
            raise SecureStoreError("private booking load failure")
        return self.bookings.get((secret_ref, revision))

    def clear_booking_secrets(self, secret_ref: str, revision: str) -> None:
        self.events.append(f"clear:{secret_ref}:{revision}")
        if self.fail_booking_clear:
            raise SecureStoreError("private booking clear failure")
        self.bookings.pop((secret_ref, revision), None)
