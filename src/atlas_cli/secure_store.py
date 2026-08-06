"""Keyring-only storage for Atlas authorization secrets."""

from __future__ import annotations

import re
from typing import Protocol, TypeVar
from uuid import uuid4

import keyring
import keyring.errors
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


class PendingAuth(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: str
    expires_at: str


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    jwt: str
    client_code: str
    cid: str


class ApiCredential(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_code: str | None = None
    ak: str = Field(repr=False)
    sk: str = Field(repr=False)


class ApiCredentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    pre: ApiCredential | None = None
    sandbox: ApiCredential | None = None
    production: ApiCredential | None = None


class SearchSecrets(BaseModel):
    model_config = ConfigDict(frozen=True)

    search_id: str
    generation: str
    offers: dict[str, str] = Field(repr=False)


class BookingSecrets(BaseModel):
    model_config = ConfigDict(frozen=True)

    booking_id: str
    generation: str
    revision: str
    session_id: str = Field(repr=False)
    products: dict[str, str] = Field(repr=False)


class SecureStoreError(RuntimeError):
    """Raised when secure secret persistence is unavailable or invalid."""


class SecureRecordInvalidError(SecureStoreError):
    """Raised when a secure record exists but cannot be validated."""


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretStore(Protocol):
    def save_pending_auth(self, pending: PendingAuth) -> None: ...

    def load_pending_auth(self) -> PendingAuth | None: ...

    def clear_pending_auth(self) -> None: ...

    def save_credentials(self, credentials: Credentials) -> None: ...

    def load_credentials(self) -> Credentials | None: ...

    def clear_credentials(self) -> None: ...

    def save_api_credentials(self, credentials: ApiCredentials) -> None: ...

    def load_api_credentials(self) -> ApiCredentials | None: ...

    def clear_api_credentials(self) -> None: ...

    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None: ...

    def load_search_secrets(self, secret_ref: str) -> SearchSecrets | None: ...

    def clear_search_secrets(self, secret_ref: str) -> None: ...

    def save_booking_secrets(self, secret_ref: str, revision: str, value: BookingSecrets) -> None: ...

    def load_booking_secrets(self, secret_ref: str, revision: str) -> BookingSecrets | None: ...

    def clear_booking_secrets(self, secret_ref: str, revision: str) -> None: ...

    def probe(self) -> bool: ...


class WorkflowSecretStore(Protocol):
    """Secure storage required by search and booking workflow state."""

    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None: ...

    def load_search_secrets(self, secret_ref: str) -> SearchSecrets | None: ...

    def clear_search_secrets(self, secret_ref: str) -> None: ...

    def save_booking_secrets(self, secret_ref: str, revision: str, value: BookingSecrets) -> None: ...

    def load_booking_secrets(self, secret_ref: str, revision: str) -> BookingSecrets | None: ...

    def clear_booking_secrets(self, secret_ref: str, revision: str) -> None: ...


class KeyringSecretStore:
    SERVICE = "atlas-flight-booking"
    LEGACY_SERVICE = "atlas-cli"
    PENDING_USERNAME = "pending-auth"
    CREDENTIALS_USERNAME = "credentials"
    API_CREDENTIALS_USERNAME = "api-credentials"
    SEARCH_SECRET_REF_PATTERN = re.compile(r"ssec_[A-Za-z0-9_-]{12,128}")
    BOOKING_SECRET_REF_PATTERN = re.compile(r"bsec_[A-Za-z0-9_-]{12,128}")
    BOOKING_SECRET_REVISION_PATTERN = re.compile(r"rev_[A-Za-z0-9_-]{12,128}")

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._backend: KeyringBackend = backend or keyring

    def save_pending_auth(self, pending: PendingAuth) -> None:
        self._save(self.PENDING_USERNAME, pending)

    def load_pending_auth(self) -> PendingAuth | None:
        return self._load(self.PENDING_USERNAME, PendingAuth)

    def clear_pending_auth(self) -> None:
        self._clear(self.PENDING_USERNAME)

    def save_credentials(self, credentials: Credentials) -> None:
        self._save(self.CREDENTIALS_USERNAME, credentials)

    def load_credentials(self) -> Credentials | None:
        return self._load(self.CREDENTIALS_USERNAME, Credentials)

    def clear_credentials(self) -> None:
        self._clear(self.CREDENTIALS_USERNAME)

    def save_api_credentials(self, credentials: ApiCredentials) -> None:
        self._save(self.API_CREDENTIALS_USERNAME, credentials)

    def load_api_credentials(self) -> ApiCredentials | None:
        return self._load(self.API_CREDENTIALS_USERNAME, ApiCredentials)

    def clear_api_credentials(self) -> None:
        self._clear(self.API_CREDENTIALS_USERNAME)

    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None:
        self._save(self._search_secrets_username(secret_ref), value)

    def load_search_secrets(self, secret_ref: str) -> SearchSecrets | None:
        return self._load(self._search_secrets_username(secret_ref), SearchSecrets)

    def clear_search_secrets(self, secret_ref: str) -> None:
        self._clear(self._search_secrets_username(secret_ref))

    def save_booking_secrets(self, secret_ref: str, revision: str, value: BookingSecrets) -> None:
        self._save(self._booking_secrets_username(secret_ref, revision), value)

    def load_booking_secrets(self, secret_ref: str, revision: str) -> BookingSecrets | None:
        return self._load(self._booking_secrets_username(secret_ref, revision), BookingSecrets)

    def clear_booking_secrets(self, secret_ref: str, revision: str) -> None:
        self._clear(self._booking_secrets_username(secret_ref, revision))

    def probe(self) -> bool:
        username = f"probe-{uuid4().hex}"
        value = uuid4().hex
        matches = False
        try:
            self._backend.set_password(self.SERVICE, username, value)
            matches = self._backend.get_password(self.SERVICE, username) == value
        except keyring.errors.KeyringError:
            matches = False
        finally:
            try:
                self._backend.delete_password(self.SERVICE, username)
            except keyring.errors.KeyringError:
                matches = False
        return matches

    def _search_secrets_username(self, secret_ref: str) -> str:
        self._validate_identifier(secret_ref, self.SEARCH_SECRET_REF_PATTERN)
        return f"search-secrets:{secret_ref}"

    def _booking_secrets_username(self, secret_ref: str, revision: str) -> str:
        self._validate_identifier(secret_ref, self.BOOKING_SECRET_REF_PATTERN)
        self._validate_identifier(revision, self.BOOKING_SECRET_REVISION_PATTERN)
        return f"booking-secrets:{secret_ref}:{revision}"

    @staticmethod
    def _validate_identifier(value: str, pattern: re.Pattern[str]) -> None:
        if pattern.fullmatch(value) is None:
            raise SecureStoreError("Secure credential storage is unavailable")

    def _save(self, username: str, value: BaseModel) -> None:
        try:
            self._backend.set_password(self.SERVICE, username, value.model_dump_json())
        except keyring.errors.KeyringError as error:
            raise SecureStoreError("Secure credential storage is unavailable") from error

    def _load(self, username: str, model: type[ModelT]) -> ModelT | None:
        try:
            serialized = self._backend.get_password(self.SERVICE, username)
            legacy = False
            if serialized is None:
                serialized = self._backend.get_password(self.LEGACY_SERVICE, username)
                legacy = serialized is not None
        except keyring.errors.KeyringError as error:
            raise SecureStoreError("Secure credential storage is unavailable") from error
        if serialized is None:
            return None
        try:
            parsed = model.model_validate_json(serialized)
        except (ValidationError, ValueError) as error:
            raise SecureRecordInvalidError("Secure credential storage is unavailable") from error
        if legacy:
            try:
                self._backend.set_password(self.SERVICE, username, serialized)
            except keyring.errors.KeyringError as error:
                raise SecureStoreError("Secure credential storage is unavailable") from error
        return parsed

    def _clear(self, username: str) -> None:
        try:
            for service in (self.SERVICE, self.LEGACY_SERVICE):
                if self._backend.get_password(service, username) is not None:
                    self._backend.delete_password(service, username)
        except keyring.errors.KeyringError as error:
            raise SecureStoreError("Secure credential storage is unavailable") from error
