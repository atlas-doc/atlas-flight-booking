from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_cli import cli as cli_module
from atlas_cli.access import SearchAccess
from atlas_cli.api_client import ApiClientError
from atlas_cli.business_client import BusinessApiError
from atlas_cli.cli import app
from atlas_cli.endpoints import CredentialSlot, SearchProvider, SearchRoute
from atlas_cli.search import SearchService
from atlas_cli.search_adapters import SearchAdapterError
from atlas_cli.search_models import (
    NormalizedOffer,
    NormalizedPassengerPrice,
    NormalizedSearch,
    NormalizedSegment,
    SearchRequest,
)
from atlas_cli.search_store import SearchStore
from atlas_cli.secure_store import ApiCredential, Credentials, SearchSecrets, SecureStoreError

runner = CliRunner()
DEFAULT_CREDENTIALS = Credentials(jwt="jwt-" + "value", client_code="CLIENT", cid="CUSTOMER")


def test_search_composition_shares_one_secure_store(monkeypatch: pytest.MonkeyPatch) -> None:
    secure_store = object()
    monkeypatch.setattr(cli_module, "KeyringSecretStore", lambda: secure_store)

    service = cli_module.build_search_service()

    assert service._secrets is secure_store
    assert service._access._secrets is secure_store
    assert service._store._secrets is secure_store


def normalized_offer(*, bookable: bool = False) -> NormalizedOffer:
    return NormalizedOffer(
        upstream_identifier="private-routing-token",
        currency="USD",
        total_price=125.0,
        transaction_fee_total=5.0,
        passenger_prices=[
            NormalizedPassengerPrice(
                passenger_type="adult",
                count=1,
                base_fare_per_passenger=100.0,
                tax_per_passenger=20.0,
                subtotal=120.0,
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
        price_status="current",
        refresh_time="2026-08-10T01:00:00Z",
        expire_time="2026-08-10T02:00:00Z",
    )


def search_access(*, generation: str = "a" * 24, bookable: bool = False) -> SearchAccess:
    return SearchAccess(
        route=SearchRoute(
            base_url="https://private.example.invalid",
            path="/search.do",
            provider=SearchProvider.STANDARD,
            credential_slot=CredentialSlot.PRODUCTION,
            bookable=bookable,
            generation=generation,
        ),
        credential=ApiCredential(ak="private-" + "ak", sk="private-" + "sk"),
        activation_status=3,
        top_up_completed=bookable,
    )


@dataclass
class FakeSecrets:
    credentials: Credentials | None = field(
        default_factory=lambda: Credentials(jwt="jwt-" + "value", client_code="CLIENT", cid="CUSTOMER")
    )
    searches: dict[str, SearchSecrets] = field(default_factory=dict)
    workflow_unavailable: bool = False

    def load_credentials(self) -> Credentials | None:
        return self.credentials

    def clear_credentials(self) -> None:
        self.credentials = None

    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None:
        self.searches[secret_ref] = value

    def load_search_secrets(self, secret_ref: str) -> SearchSecrets | None:
        if self.workflow_unavailable:
            raise SecureStoreError("private backend detail")
        return self.searches.get(secret_ref)

    def clear_search_secrets(self, secret_ref: str) -> None:
        self.searches.pop(secret_ref, None)


class FailingWorkflowSecrets(FakeSecrets):
    def save_search_secrets(self, secret_ref: str, value: SearchSecrets) -> None:
        raise SecureStoreError("private backend detail")


@dataclass
class FakeAccessManager:
    access: SearchAccess = field(default_factory=search_access)
    calls: list[str] = field(default_factory=list)

    def resolve_search_access(self, jwt: str) -> SearchAccess:
        self.calls.append(jwt)
        return self.access


@dataclass
class SequentialAccessManager:
    outcomes: list[SearchAccess | ApiClientError]
    calls: list[str] = field(default_factory=list)

    def resolve_search_access(self, jwt: str) -> SearchAccess:
        self.calls.append(jwt)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ApiClientError):
            raise outcome
        return outcome


@dataclass
class FakeAdapter:
    outcome: NormalizedSearch | SearchAdapterError
    requests: list[SearchRequest] = field(default_factory=list)

    def search(
        self,
        route: SearchRoute,
        credential: ApiCredential,
        request: SearchRequest,
    ) -> NormalizedSearch:
        self.requests.append(request)
        if isinstance(self.outcome, SearchAdapterError):
            raise self.outcome
        return self.outcome


@dataclass
class SequentialAdapter:
    outcomes: list[NormalizedSearch | BusinessApiError]
    requests: list[SearchRequest] = field(default_factory=list)

    def search(
        self,
        route: SearchRoute,
        credential: ApiCredential,
        request: SearchRequest,
    ) -> NormalizedSearch:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BusinessApiError):
            raise outcome
        return outcome


def make_service(
    tmp_path: Path,
    *,
    outcome: NormalizedSearch | SearchAdapterError | None = None,
    credentials: Credentials | None = DEFAULT_CREDENTIALS,
    access: SearchAccess | None = None,
) -> tuple[SearchService, FakeAdapter, FakeAccessManager]:
    adapter = FakeAdapter(outcome or NormalizedSearch(offers=[normalized_offer()], request_id="safe-request"))
    manager = FakeAccessManager(access or search_access())
    secrets = FakeSecrets(credentials)
    service = SearchService(
        secrets=secrets,
        access=manager,
        fare_adapter=adapter,
        booking_adapter=adapter,
        store=SearchStore(tmp_path, secrets=secrets),
    )
    return service, adapter, manager


def invoke_search(monkeypatch: pytest.MonkeyPatch, service: SearchService, *args: str):
    monkeypatch.setattr("atlas_cli.cli.build_search_service", lambda: service)
    return runner.invoke(app, ["search", *args, "--json"])


def test_cli_search_and_offer_list_emit_one_safe_normalized_object(tmp_path: Path, monkeypatch) -> None:
    service, adapter, manager = make_service(tmp_path)
    monkeypatch.setattr("atlas_cli.cli.build_search_service", lambda: service)

    searched = runner.invoke(
        app,
        [
            "search",
            "--origin",
            "kul",
            "--destination",
            "sin",
            "--depart",
            "2026-08-10",
            "--adults",
            "1",
            "--json",
        ],
    )

    assert searched.exit_code == 0
    assert searched.stderr == ""
    assert len(searched.stdout.splitlines()) == 1
    payload = json.loads(searched.stdout)
    assert payload["code"] == "FLIGHT_SEARCHED"
    assert payload["data"]["search_id"].startswith("srch_")
    assert payload["data"]["offer_count"] == 1
    public_offer = payload["data"]["offers"][0]
    assert public_offer["offer_id"].startswith("off_")
    assert public_offer["total_price"] == 125.0
    assert public_offer["bookable"] is False
    exposed = searched.stdout.lower()
    for private in (
        "private-routing-token",
        "private.example.invalid",
        "upstream_identifier",
        "credential_slot",
        "production",
        "provider",
    ):
        assert private not in exposed

    listed = runner.invoke(
        app,
        ["offer", "list", "--search-id", payload["data"]["search_id"], "--json"],
    )
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.stdout)
    assert listed_payload["code"] == "OFFERS_LISTED"
    assert listed_payload["data"]["offers"] == payload["data"]["offers"]
    assert adapter.requests[0].origin == "KUL"
    assert len(manager.calls) == 2


def test_cli_search_without_arguments_replays_latest_request(tmp_path: Path, monkeypatch) -> None:
    service, adapter, _ = make_service(tmp_path)

    first = invoke_search(
        monkeypatch,
        service,
        "--origin",
        "KUL",
        "--destination",
        "SIN",
        "--depart",
        "2026-08-10",
        "--adults",
        "1",
    )
    replayed = invoke_search(monkeypatch, service)

    assert first.exit_code == 0
    assert replayed.exit_code == 0
    assert json.loads(replayed.stdout)["code"] == "FLIGHT_SEARCHED"
    assert len(adapter.requests) == 2
    assert adapter.requests[0] == adapter.requests[1]


@pytest.mark.parametrize(
    "args",
    [
        ("--origin", "KUL", "--json"),
        (
            "--origin",
            "KUL",
            "--destination",
            "SIN",
            "--depart",
            "invalid-date",
            "--adults",
            "1",
            "--json",
        ),
        (
            "--origin",
            "KUL",
            "--destination",
            "SIN",
            "--depart",
            "2026-08-10",
            "--adults",
            "0",
            "--json",
        ),
    ],
)
def test_cli_invalid_search_arguments_are_stable_json(tmp_path: Path, monkeypatch, args: tuple[str, ...]) -> None:
    service, _, _ = make_service(tmp_path)
    monkeypatch.setattr("atlas_cli.cli.build_search_service", lambda: service)

    result = runner.invoke(app, ["search", *args])

    assert result.exit_code == 2
    assert result.stderr == ""
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_search_without_authorization_returns_normal_auth_flow(tmp_path: Path, monkeypatch) -> None:
    service, _, manager = make_service(tmp_path, credentials=None)

    result = invoke_search(
        monkeypatch,
        service,
        "--origin",
        "KUL",
        "--destination",
        "SIN",
        "--depart",
        "2026-08-10",
        "--adults",
        "1",
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["code"] == "AUTHORIZATION_REQUIRED"
    assert payload["status"] == "action_required"
    assert manager.calls == []


def test_successful_empty_search_uses_search_no_results(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = make_service(
        tmp_path,
        outcome=NormalizedSearch(offers=[], reason="no_flight", recent_flight_dates=["2026-08-11"]),
    )

    result = invoke_search(
        monkeypatch,
        service,
        "--origin",
        "KUL",
        "--destination",
        "SIN",
        "--depart",
        "2026-08-10",
        "--adults",
        "1",
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["code"] == "SEARCH_NO_RESULTS"
    assert payload["data"]["offer_count"] == 0
    assert payload["data"]["reason"] == "no_flight"


def test_secure_search_state_failure_returns_stable_agent_error(tmp_path: Path) -> None:
    secrets = FailingWorkflowSecrets()
    adapter = FakeAdapter(NormalizedSearch(offers=[normalized_offer()], request_id="safe-request"))
    service = SearchService(
        secrets=secrets,
        access=FakeAccessManager(),
        fare_adapter=adapter,
        booking_adapter=adapter,
        store=SearchStore(tmp_path, secrets=secrets),
    )

    result = service.search(request_for_service())

    assert result.code == "SECURE_STORE_UNAVAILABLE"
    assert result.message == "Secure credential storage is unavailable"
    assert "private backend detail" not in json.dumps(result.model_dump(mode="json"))


def test_secure_backend_unavailable_during_offer_load_is_not_reported_as_expired(
    tmp_path: Path,
) -> None:
    secrets = FakeSecrets()
    adapter = FakeAdapter(NormalizedSearch(offers=[normalized_offer(bookable=True)], request_id="safe-request"))
    service = SearchService(
        secrets=secrets,
        access=FakeAccessManager(search_access(bookable=True)),
        fare_adapter=adapter,
        booking_adapter=adapter,
        store=SearchStore(tmp_path, secrets=secrets),
    )
    searched = service.search(request_for_service())
    secrets.workflow_unavailable = True

    result = service.list_offers(str(searched.data["search_id"]))

    assert result.code == "SECURE_STORE_UNAVAILABLE"
    assert result.message == "Secure credential storage is unavailable"
    assert "private backend detail" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("error", "expected_exit"),
    [
        (
            SearchAdapterError(
                code="SEARCH_LIMIT_REACHED",
                message="Flight search limit reached",
                retryable=False,
            ),
            30,
        ),
        (
            SearchAdapterError(
                code="SERVICE_TEMPORARILY_UNAVAILABLE",
                message="Flight search temporarily unavailable",
                retryable=True,
            ),
            20,
        ),
    ],
)
def test_search_errors_preserve_stable_codes_and_exit_mapping(
    tmp_path: Path,
    monkeypatch,
    error: SearchAdapterError,
    expected_exit: int,
) -> None:
    service, _, _ = make_service(tmp_path, outcome=error)

    result = invoke_search(
        monkeypatch,
        service,
        "--origin",
        "KUL",
        "--destination",
        "SIN",
        "--depart",
        "2026-08-10",
        "--adults",
        "1",
    )

    assert result.exit_code == expected_exit
    assert json.loads(result.stdout)["code"] == error.code


def test_offer_list_with_stale_search_id_is_neutral(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = make_service(tmp_path)
    monkeypatch.setattr("atlas_cli.cli.build_search_service", lambda: service)

    result = runner.invoke(app, ["offer", "list", "--search-id", "srch_missing", "--json"])

    assert result.exit_code == 30
    payload = json.loads(result.stdout)
    assert payload["code"] == "OFFER_EXPIRED"
    assert "environment" not in result.stdout.lower()


def test_replay_without_stored_input_is_neutral_expired_result(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = make_service(tmp_path)

    result = invoke_search(monkeypatch, service)

    assert result.exit_code == 30
    assert json.loads(result.stdout)["code"] == "OFFER_EXPIRED"


def credential_rejected() -> BusinessApiError:
    return BusinessApiError(
        code="CREDENTIAL_REJECTED",
        message="Service credentials need to be refreshed",
        retryable=True,
    )


def request_for_service() -> SearchRequest:
    return SearchRequest(origin="KUL", destination="SIN", depart="2026-08-10", adults=1)


def test_credential_rejection_refreshes_once_and_retries_same_read_only_search(tmp_path: Path) -> None:
    access = search_access()
    manager = SequentialAccessManager([access, access])
    adapter = SequentialAdapter(
        [credential_rejected(), NormalizedSearch(offers=[normalized_offer()], request_id="req-recovered")]
    )
    secrets = FakeSecrets()
    service = SearchService(
        secrets=secrets,
        access=manager,
        fare_adapter=adapter,
        booking_adapter=adapter,
        store=SearchStore(tmp_path, secrets=secrets),
    )
    request = request_for_service()

    result = service.search(request)

    assert result.code == "FLIGHT_SEARCHED"
    assert len(manager.calls) == 2
    assert adapter.requests == [request, request]


def test_refresh_that_requires_browser_authorization_clears_only_jwt(tmp_path: Path) -> None:
    auth_required = ApiClientError(
        code="AUTHORIZATION_REQUIRED",
        message="Authorization required",
        retryable=False,
    )
    manager = SequentialAccessManager([search_access(), auth_required])
    adapter = SequentialAdapter([credential_rejected()])
    secrets = FakeSecrets()
    service = SearchService(
        secrets=secrets,
        access=manager,
        fare_adapter=adapter,
        booking_adapter=adapter,
        store=SearchStore(tmp_path, secrets=secrets),
    )

    result = service.search(request_for_service())

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert result.status.value == "action_required"
    assert secrets.credentials is None
    assert not hasattr(secrets, "clear_api_credentials")


def test_second_credential_rejection_stops_without_unbounded_retry(tmp_path: Path) -> None:
    access = search_access()
    manager = SequentialAccessManager([access, access])
    adapter = SequentialAdapter([credential_rejected(), credential_rejected()])
    secrets = FakeSecrets()
    service = SearchService(
        secrets=secrets,
        access=manager,
        fare_adapter=adapter,
        booking_adapter=adapter,
        store=SearchStore(tmp_path, secrets=secrets),
    )

    result = service.search(request_for_service())

    assert result.code == "CREDENTIAL_REJECTED"
    assert result.retryable is False
    assert len(manager.calls) == 2
    assert len(adapter.requests) == 2


def test_initial_protected_auth_failure_clears_jwt_and_never_calls_adapter(tmp_path: Path) -> None:
    auth_required = ApiClientError(
        code="AUTHORIZATION_REQUIRED",
        message="Authorization required",
        retryable=False,
    )
    manager = SequentialAccessManager([auth_required])
    adapter = SequentialAdapter([])
    secrets = FakeSecrets()
    service = SearchService(
        secrets=secrets,
        access=manager,
        fare_adapter=adapter,
        booking_adapter=adapter,
        store=SearchStore(tmp_path, secrets=secrets),
    )

    result = service.search(request_for_service())

    assert result.code == "AUTHORIZATION_REQUIRED"
    assert secrets.credentials is None
    assert adapter.requests == []
