from pathlib import Path

import keyring.errors
import pytest

from atlas_cli.secure_store import (
    ApiCredential,
    ApiCredentials,
    BookingSecrets,
    Credentials,
    KeyringSecretStore,
    PendingAuth,
    SearchSecrets,
    SecureStoreError,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.events: list[str] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.events.append("get")
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.events.append("set")
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.events.append("delete")
        self.passwords.pop((service, username), None)


class FailingKeyring(FakeKeyring):
    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.KeyringError("backend unavailable")


class WrongProbeValueKeyring(FakeKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        super().get_password(service, username)
        return "wrong-probe-value"


class PartialSetFailureKeyring(FakeKeyring):
    def set_password(self, service: str, username: str, password: str) -> None:
        super().set_password(service, username, password)
        raise keyring.errors.KeyringError("private-set-failure")


class GetFailureKeyring(FakeKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        super().get_password(service, username)
        raise keyring.errors.KeyringError("private-get-failure")


class DeleteFailureKeyring(FakeKeyring):
    def delete_password(self, service: str, username: str) -> None:
        super().delete_password(service, username)
        raise keyring.errors.KeyringError("private-delete-failure")


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def failing_keyring() -> FailingKeyring:
    return FailingKeyring()


def test_pending_auth_round_trip(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)
    pending = PendingAuth(token="short-lived-token", expires_at="2026-08-03 19:00:00")
    store.save_pending_auth(pending)
    assert store.load_pending_auth() == pending
    store.clear_pending_auth()
    assert store.load_pending_auth() is None


def test_credentials_are_one_keyring_record(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)
    credentials = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    store.save_credentials(credentials)
    assert store.load_credentials() == credentials
    assert set(fake_keyring.passwords) == {("atlas-flight-booking", "credentials")}


def test_legacy_credentials_are_migrated_to_the_new_keyring_service(fake_keyring: FakeKeyring) -> None:
    credentials = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    fake_keyring.passwords[("atlas-cli", "credentials")] = credentials.model_dump_json()
    store = KeyringSecretStore(backend=fake_keyring)

    assert store.load_credentials() == credentials
    assert fake_keyring.passwords[("atlas-flight-booking", "credentials")] == credentials.model_dump_json()

    store.clear_credentials()
    assert ("atlas-cli", "credentials") not in fake_keyring.passwords
    assert ("atlas-flight-booking", "credentials") not in fake_keyring.passwords


def test_api_credentials_are_one_separate_keyring_record(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)
    credentials = ApiCredentials(
        pre=ApiCredential(client_code="CLIENT", ak="pre-" + "ak", sk="pre-" + "sk"),
        sandbox=ApiCredential(client_code=None, ak="box-" + "ak", sk="box-" + "sk"),
        production=None,
    )

    store.save_api_credentials(credentials)

    assert store.load_api_credentials() == credentials
    assert set(fake_keyring.passwords) == {("atlas-flight-booking", "api-credentials")}


def test_clearing_api_credentials_preserves_control_credentials(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)
    control = Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER")
    api = ApiCredentials(pre=ApiCredential(client_code="CLIENT", ak="pre-" + "ak", sk="pre-" + "sk"))
    store.save_credentials(control)
    store.save_api_credentials(api)

    store.clear_api_credentials()

    assert store.load_api_credentials() is None
    assert store.load_credentials() == control


def test_keyring_failure_never_writes_plaintext(tmp_path: Path, failing_keyring: FailingKeyring) -> None:
    store = KeyringSecretStore(backend=failing_keyring)
    with pytest.raises(SecureStoreError):
        store.save_credentials(Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER"))
    assert list(tmp_path.iterdir()) == []


def test_api_keyring_failure_never_writes_plaintext(tmp_path: Path, failing_keyring: FailingKeyring) -> None:
    store = KeyringSecretStore(backend=failing_keyring)
    api = ApiCredentials(pre=ApiCredential(client_code="CLIENT", ak="pre-" + "ak", sk="pre-" + "sk"))

    with pytest.raises(SecureStoreError):
        store.save_api_credentials(api)

    assert list(tmp_path.iterdir()) == []


def test_probe_cleans_up_its_keyring_record(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)

    assert store.probe() is True
    assert fake_keyring.events == ["set", "get", "delete"]
    assert fake_keyring.passwords == {}


def test_probe_rejects_wrong_read_back_and_still_cleans_up() -> None:
    backend = WrongProbeValueKeyring()
    store = KeyringSecretStore(backend=backend)

    assert store.probe() is False
    assert backend.events == ["set", "get", "delete"]
    assert backend.passwords == {}


def test_probe_cleans_up_after_partial_set_failure() -> None:
    backend = PartialSetFailureKeyring()
    store = KeyringSecretStore(backend=backend)

    assert store.probe() is False
    assert backend.events == ["set", "delete"]
    assert backend.passwords == {}


def test_probe_cleans_up_after_get_failure() -> None:
    backend = GetFailureKeyring()
    store = KeyringSecretStore(backend=backend)

    assert store.probe() is False
    assert backend.events == ["set", "get", "delete"]
    assert backend.passwords == {}


def test_probe_delete_failure_returns_false_without_escaping() -> None:
    backend = DeleteFailureKeyring()
    store = KeyringSecretStore(backend=backend)

    assert store.probe() is False
    assert backend.events == ["set", "get", "delete"]
    assert backend.passwords == {}


def test_search_secrets_round_trip_in_separate_keyring_record(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)
    secrets = SearchSecrets(
        search_id="srch_public",
        generation="g" * 24,
        offers={"off_public": "private-route"},
    )
    store.save_credentials(Credentials(jwt="jwt-value", client_code="CLIENT", cid="CUSTOMER"))
    store.save_api_credentials(
        ApiCredentials(pre=ApiCredential(client_code="CLIENT", ak="private-ak", sk="private-sk"))
    )

    store.save_search_secrets("ssec_abcdefghijkl", secrets)

    assert store.load_search_secrets("ssec_abcdefghijkl") == secrets
    assert set(fake_keyring.passwords) == {
        ("atlas-flight-booking", "credentials"),
        ("atlas-flight-booking", "api-credentials"),
        ("atlas-flight-booking", "search-secrets:ssec_abcdefghijkl"),
    }


def test_booking_secrets_round_trip_in_revision_specific_record(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)
    secrets = BookingSecrets(
        booking_id="book_public",
        generation="g" * 24,
        revision="rev_public",
        session_id="private-session",
        products={"seat_public": "private-product"},
    )

    store.save_booking_secrets("bsec_abcdefghijkl", "rev_abcdefghijkl", secrets)

    assert store.load_booking_secrets("bsec_abcdefghijkl", "rev_abcdefghijkl") == secrets
    assert set(fake_keyring.passwords) == {
        ("atlas-flight-booking", "booking-secrets:bsec_abcdefghijkl:rev_abcdefghijkl")
    }


def test_workflow_secret_reprs_hide_private_values() -> None:
    search_route = "private-search-route"
    booking_session = "private-booking-session"
    booking_product = "private-booking-product"
    search = SearchSecrets(
        search_id="srch_public",
        generation="g" * 24,
        offers={"off_public": search_route},
    )
    booking = BookingSecrets(
        booking_id="book_public",
        generation="g" * 24,
        revision="rev_public",
        session_id=booking_session,
        products={"seat_public": booking_product},
    )

    assert search_route not in repr(search)
    assert booking_session not in repr(booking)
    assert booking_product not in repr(booking)


@pytest.mark.parametrize("operation", ["save", "load", "clear"])
def test_workflow_backend_errors_are_sanitized(operation: str) -> None:
    backend_error = f"private-{operation}-backend-error"

    class OperationFailureKeyring(FakeKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            if operation in {"load", "clear"}:
                raise keyring.errors.KeyringError(backend_error)
            return super().get_password(service, username)

        def set_password(self, service: str, username: str, password: str) -> None:
            if operation == "save":
                raise keyring.errors.KeyringError(backend_error)
            super().set_password(service, username, password)

    secret_ref = "ssec_abcdefghijkl"
    store = KeyringSecretStore(backend=OperationFailureKeyring())
    search = SearchSecrets(
        search_id="srch_public",
        generation="g" * 24,
        offers={"off_public": "private-search-route"},
    )

    with pytest.raises(SecureStoreError) as raised:
        if operation == "save":
            store.save_search_secrets(secret_ref, search)
        elif operation == "load":
            store.load_search_secrets(secret_ref)
        else:
            store.clear_search_secrets(secret_ref)

    public_error = f"{raised.value!r} {raised.value}"
    assert secret_ref not in public_error
    assert backend_error not in public_error
    assert "private-search-route" not in public_error


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("save_search_secrets", ("private-invalid-ref", SearchSecrets(search_id="srch", generation="g", offers={}))),
        ("load_search_secrets", ("private-invalid-ref",)),
        ("clear_search_secrets", ("private-invalid-ref",)),
        (
            "save_booking_secrets",
            (
                "private-invalid-ref",
                "rev_abcdefghijkl",
                BookingSecrets(
                    booking_id="book",
                    generation="g",
                    revision="rev",
                    session_id="private-session",
                    products={},
                ),
            ),
        ),
        ("load_booking_secrets", ("bsec_abcdefghijkl", "private-invalid-revision")),
        ("clear_booking_secrets", ("bsec_abcdefghijkl", "private-invalid-revision")),
    ],
)
def test_invalid_workflow_refs_never_reach_keyring(
    fake_keyring: FakeKeyring, operation: str, arguments: tuple[object, ...]
) -> None:
    store = KeyringSecretStore(backend=fake_keyring)

    with pytest.raises(SecureStoreError) as raised:
        getattr(store, operation)(*arguments)

    assert fake_keyring.events == []
    assert "private-invalid" not in str(raised.value)


def test_corrupt_workflow_secret_json_raises_sanitized_error(fake_keyring: FakeKeyring) -> None:
    username = "search-secrets:ssec_abcdefghijkl"
    fake_keyring.passwords[("atlas-flight-booking", username)] = "private-corrupt-json"
    store = KeyringSecretStore(backend=fake_keyring)

    with pytest.raises(SecureStoreError) as raised:
        store.load_search_secrets("ssec_abcdefghijkl")

    assert "private-corrupt-json" not in str(raised.value)


def test_clear_booking_secrets_affects_only_requested_revision(fake_keyring: FakeKeyring) -> None:
    store = KeyringSecretStore(backend=fake_keyring)
    first = BookingSecrets(
        booking_id="book_public",
        generation="g" * 24,
        revision="rev_public_1",
        session_id="private-session-1",
        products={"seat_public": "private-product-1"},
    )
    second = first.model_copy(update={"revision": "rev_public_2", "session_id": "private-session-2"})
    store.save_booking_secrets("bsec_abcdefghijkl", "rev_abcdefghijkl", first)
    store.save_booking_secrets("bsec_abcdefghijkl", "rev_abcdefghijkm", second)

    store.clear_booking_secrets("bsec_abcdefghijkl", "rev_abcdefghijkl")

    assert store.load_booking_secrets("bsec_abcdefghijkl", "rev_abcdefghijkl") is None
    assert store.load_booking_secrets("bsec_abcdefghijkl", "rev_abcdefghijkm") == second
