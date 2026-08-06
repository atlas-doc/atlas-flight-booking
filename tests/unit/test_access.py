from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from atlas_cli.access import AccessManager, AccessManagerError
from atlas_cli.api_models import (
    AccessCredentialRecord,
    AccessInfo,
    PreProductionAccessInfos,
    ProductionAccessInfos,
)
from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import BusinessOperation, CredentialSlot, CustomerMode, EndpointResolver
from atlas_cli.secure_store import ApiCredential, ApiCredentials


def record(label: str, *, client_code: str | None = "CLIENT") -> AccessCredentialRecord:
    return AccessCredentialRecord(clientCode=client_code, ak=f"{label}-" + "ak", sk=f"{label}-" + "sk")


@dataclass
class FakeAccessApi:
    access: AccessInfo
    preproduction: PreProductionAccessInfos
    production: ProductionAccessInfos = field(default_factory=ProductionAccessInfos)
    events: list[str] = field(default_factory=list)

    def check_access_info(self, jwt: str) -> AccessInfo:
        self.events.append("access_checked")
        return self.access

    def get_preproduction_access_infos(self, jwt: str) -> PreProductionAccessInfos:
        self.events.append("preproduction_fetched")
        return self.preproduction

    def get_or_create_production_access_infos(self, jwt: str) -> ProductionAccessInfos:
        self.events.append("production_fetched")
        return self.production


@dataclass
class FakeAccessSecrets:
    credentials: ApiCredentials | None = None
    events: list[str] = field(default_factory=list)

    def load_api_credentials(self) -> ApiCredentials | None:
        return self.credentials

    def save_api_credentials(self, credentials: ApiCredentials) -> None:
        self.events.append("credentials_saved")
        self.credentials = credentials


def make_manager(
    api: FakeAccessApi,
    secrets: FakeAccessSecrets,
    *,
    mode: CustomerMode = CustomerMode.PROD,
) -> AccessManager:
    return AccessManager(
        api=api,
        secrets=secrets,
        resolver=EndpointResolver(
            InternalSettings(
                prod_api_base_url="https://prod.example.invalid",
                sandbox_api_base_url="https://sandbox.example.invalid",
            )
        ),
        mode=mode,
    )


def make_access_manager(*, activation_status: int, top_up_completed: bool) -> AccessManager:
    return make_manager(
        FakeAccessApi(
            access=AccessInfo(
                activation_status=activation_status,
                top_up_completed=top_up_completed,
                access_info_exists=False,
            ),
            preproduction=preproduction(),
            production=ProductionAccessInfos(
                prd=[record("product")],
                sandbox=[record("box", client_code=None)],
            ),
        ),
        FakeAccessSecrets(),
    )


def preproduction(*, pre: list[AccessCredentialRecord] | None = None) -> PreProductionAccessInfos:
    return PreProductionAccessInfos(
        pre=[record("pre")] if pre is None else pre,
        sandbox=[record("box", client_code=None)],
    )


@pytest.mark.parametrize(
    ("activation_status", "top_up_completed"),
    [(1, False), (2, False), (3, False), (4, True)],
)
def test_transaction_access_requires_live_and_top_up(
    activation_status: int,
    top_up_completed: bool,
) -> None:
    manager = make_access_manager(
        activation_status=activation_status,
        top_up_completed=top_up_completed,
    )

    with pytest.raises(AccessManagerError) as raised:
        manager.resolve_transaction_access("jwt-value", BusinessOperation.VERIFY)

    assert raised.value.code == "SUBSCRIPTION_REQUIRED"
    assert raised.value.details == {"url": "https://www.atriptech.com/#/skill-entry"}


def test_transaction_access_uses_production_credential_without_exposing_it() -> None:
    manager = make_access_manager(activation_status=3, top_up_completed=True)

    access = manager.resolve_transaction_access("jwt-value", BusinessOperation.ORDER)

    assert access.route.operation is BusinessOperation.ORDER
    assert access.route.credential_slot is CredentialSlot.PRODUCTION
    assert (
        access.route.generation
        == EndpointResolver(InternalSettings(prod_api_base_url="https://prod.example.invalid"))
        .resolve_search(
            activation_status=3,
            top_up_completed=True,
            mode=CustomerMode.PROD,
        )
        .generation
    )
    assert access.credential.ak == "product-ak"
    assert access.credential.sk == "product-sk"


def test_bounded_transaction_access_passes_decreasing_remaining_time_to_each_control_request() -> None:
    now = 0.0

    class BoundedApi(FakeAccessApi):
        def __init__(self) -> None:
            super().__init__(
                access=AccessInfo(activation_status=3, top_up_completed=True, access_info_exists=True),
                preproduction=preproduction(),
                production=ProductionAccessInfos(prd=[record("product")]),
            )
            self.timeouts: list[float] = []

        def check_access_info(self, jwt: str, *, timeout_seconds: float | None = None) -> AccessInfo:
            nonlocal now
            assert timeout_seconds is not None
            self.timeouts.append(timeout_seconds)
            now += 2
            return super().check_access_info(jwt)

        def get_or_create_production_access_infos(
            self, jwt: str, *, timeout_seconds: float | None = None
        ) -> ProductionAccessInfos:
            nonlocal now
            assert timeout_seconds is not None
            self.timeouts.append(timeout_seconds)
            now += 1
            return super().get_or_create_production_access_infos(jwt)

    api = BoundedApi()

    access = make_manager(api, FakeAccessSecrets()).resolve_transaction_access(
        "jwt-value", BusinessOperation.QUERY_ORDER, deadline=10, monotonic=lambda: now
    )

    assert access.route.operation is BusinessOperation.QUERY_ORDER
    assert api.timeouts == [10, 8]


def test_bounded_transaction_access_does_not_start_a_control_request_after_deadline() -> None:
    now = 0.0

    class DeadlineConsumingApi(FakeAccessApi):
        def __init__(self) -> None:
            super().__init__(
                access=AccessInfo(activation_status=3, top_up_completed=True, access_info_exists=True),
                preproduction=preproduction(),
            )
            self.production_calls = 0

        def check_access_info(self, jwt: str, *, timeout_seconds: float | None = None) -> AccessInfo:
            nonlocal now
            assert timeout_seconds == 5
            now += 5
            return super().check_access_info(jwt)

        def get_or_create_production_access_infos(
            self, jwt: str, *, timeout_seconds: float | None = None
        ) -> ProductionAccessInfos:
            del jwt, timeout_seconds
            self.production_calls += 1
            raise AssertionError("control request must not begin after its deadline")

    api = DeadlineConsumingApi()

    with pytest.raises(AccessManagerError) as raised:
        make_manager(api, FakeAccessSecrets()).resolve_transaction_access(
            "jwt-value", BusinessOperation.QUERY_ORDER, deadline=5, monotonic=lambda: now
        )

    assert raised.value.code == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert api.production_calls == 0


def test_ineligible_transaction_with_missing_search_credential_requires_subscription() -> None:
    manager = make_manager(
        FakeAccessApi(
            access=AccessInfo(
                activation_status=1,
                top_up_completed=False,
                access_info_exists=False,
                request_id="access-request-id",
            ),
            preproduction=preproduction(pre=[]),
        ),
        FakeAccessSecrets(),
    )

    with pytest.raises(AccessManagerError) as raised:
        manager.resolve_transaction_access("jwt-value", BusinessOperation.VERIFY)

    assert raised.value.code == "SUBSCRIPTION_REQUIRED"
    assert raised.value.message == ("出票需订阅套餐，详见 https://www.atriptech.com/#/skill-entry")
    assert raised.value.details == {"url": "https://www.atriptech.com/#/skill-entry"}
    assert raised.value.request_id == "access-request-id"


def test_pre_sale_fetches_grouped_credentials_without_creating_production() -> None:
    api = FakeAccessApi(
        access=AccessInfo(activation_status=1, top_up_completed=False, access_info_exists=False),
        preproduction=preproduction(),
    )
    secrets = FakeAccessSecrets()

    access = make_manager(api, secrets).resolve_search_access("jwt-" + "value")

    assert api.events == ["access_checked", "preproduction_fetched"]
    assert access.route.credential_slot is CredentialSlot.PRE
    assert access.credential == ApiCredential(client_code="CLIENT", ak="pre-" + "ak", sk="pre-" + "sk")
    assert secrets.events == ["credentials_saved"]
    assert secrets.credentials is not None
    assert secrets.credentials.sandbox is not None


def test_live_gets_or_creates_production_even_when_access_info_is_absent() -> None:
    api = FakeAccessApi(
        access=AccessInfo(
            activation_status=3,
            top_up_completed=False,
            access_info_exists=False,
        ),
        preproduction=preproduction(),
        production=ProductionAccessInfos(
            prd=[record("prod")],
            sandbox=[record("box", client_code=None)],
        ),
    )
    secrets = FakeAccessSecrets()

    access = make_manager(api, secrets).resolve_search_access("jwt-" + "value")

    assert api.events == ["access_checked", "production_fetched"]
    assert access.route.credential_slot is CredentialSlot.PRODUCTION
    assert access.credential.ak == "prod-" + "ak"


@pytest.mark.parametrize("activation_status", [1, 2, 4])
def test_non_live_fetches_preproduction_fare_search_credentials(
    activation_status: int,
) -> None:
    api = FakeAccessApi(
        access=AccessInfo(
            activation_status=activation_status,
            top_up_completed=False,
            access_info_exists=False,
        ),
        preproduction=preproduction(),
        production=ProductionAccessInfos(
            prd=[record("must-not-use")],
            sandbox=[record("must-not-use-box")],
        ),
    )

    access = make_manager(api, FakeAccessSecrets()).resolve_search_access("jwt-" + "value")

    assert api.events == ["access_checked", "preproduction_fetched"]
    assert access.route.credential_slot is CredentialSlot.PRE
    assert access.credential.ak == "pre-" + "ak"


def test_first_complete_production_item_is_selected_and_saved_before_return() -> None:
    api = FakeAccessApi(
        access=AccessInfo(activation_status=3, top_up_completed=True, access_info_exists=False),
        preproduction=preproduction(),
        production=ProductionAccessInfos(
            prd=[
                AccessCredentialRecord(clientCode="SKIP", ak="", sk="missing-" + "ak"),
                AccessCredentialRecord(clientCode="USE", ak="selected-" + "ak", sk="selected-" + "sk"),
                record("later"),
            ]
        ),
    )
    secrets = FakeAccessSecrets()

    access = make_manager(api, secrets).resolve_search_access("jwt-" + "value")

    assert access.credential.client_code == "USE"
    assert access.credential.ak == "selected-" + "ak"
    assert access.route.bookable is True
    assert secrets.events == ["credentials_saved"]
    assert secrets.credentials is not None
    assert secrets.credentials.production == access.credential


def test_empty_required_credential_fails_with_stable_error_and_no_raw_detail() -> None:
    api = FakeAccessApi(
        access=AccessInfo(activation_status=1, top_up_completed=False, access_info_exists=False),
        preproduction=preproduction(pre=[]),
    )

    with pytest.raises(AccessManagerError) as raised:
        make_manager(api, FakeAccessSecrets()).resolve_search_access("jwt-" + "value")

    assert raised.value.code == "SERVICE_RESPONSE_INVALID"
    assert str(raised.value) == "Service response could not be processed"
    assert "pre" not in str(raised.value).lower()


@pytest.mark.parametrize(
    ("activation_status", "top_up_completed", "expected"),
    [
        (1, False, False),
        (1, True, False),
        (2, False, False),
        (2, True, False),
        (3, False, False),
        (3, True, True),
        (4, False, False),
        (4, True, False),
    ],
)
def test_snapshot_ticketing_capability_depends_only_on_live_and_top_up(
    activation_status: int,
    top_up_completed: bool,
    expected: bool,
) -> None:
    api = FakeAccessApi(
        access=AccessInfo(
            activation_status=activation_status,
            top_up_completed=top_up_completed,
            access_info_exists=False,
        ),
        preproduction=preproduction(),
        production=ProductionAccessInfos(
            prd=[record("prod")],
            sandbox=[record("box", client_code=None)],
        ),
    )

    snapshot = make_manager(api, FakeAccessSecrets()).synchronize("jwt-" + "value")

    assert snapshot.search_available is True
    assert snapshot.ticketing_available is expected


def test_sandbox_search_uses_grouped_credential_without_exposing_it_in_snapshot() -> None:
    api = FakeAccessApi(
        access=AccessInfo(activation_status=1, top_up_completed=False, access_info_exists=False),
        preproduction=preproduction(),
    )

    access = make_manager(api, FakeAccessSecrets(), mode=CustomerMode.SANDBOX).resolve_search_access("jwt-" + "value")

    assert access.route.credential_slot is CredentialSlot.SANDBOX
    assert access.credential.ak == "box-" + "ak"
    assert access.route.bookable is True


def test_sandbox_transaction_uses_sandbox_credential_without_subscription_gate() -> None:
    api = FakeAccessApi(
        access=AccessInfo(activation_status=1, top_up_completed=False, access_info_exists=False),
        preproduction=preproduction(),
    )

    access = make_manager(
        api,
        FakeAccessSecrets(),
        mode=CustomerMode.SANDBOX,
    ).resolve_transaction_access("jwt-" + "value", BusinessOperation.ORDER)

    assert access.route.base_url == "https://sandbox.example.invalid"
    assert access.route.credential_slot is CredentialSlot.SANDBOX
    assert access.credential.ak == "box-" + "ak"
    assert api.events == ["access_checked", "preproduction_fetched"]


def test_live_sandbox_uses_sandbox_credential_from_production_response() -> None:
    api = FakeAccessApi(
        access=AccessInfo(activation_status=3, top_up_completed=True, access_info_exists=True),
        preproduction=preproduction(),
        production=ProductionAccessInfos(
            prd=[record("prod")],
            sandbox=[record("live-box", client_code="CLIENT")],
        ),
    )

    access = make_manager(
        api,
        FakeAccessSecrets(),
        mode=CustomerMode.SANDBOX,
    ).resolve_transaction_access("jwt-" + "value", BusinessOperation.ORDER)

    assert access.route.credential_slot is CredentialSlot.SANDBOX
    assert access.credential.ak == "live-box-" + "ak"
    assert api.events == ["access_checked", "production_fetched"]


def test_sandbox_snapshot_exposes_transaction_capability_without_environment_details() -> None:
    api = FakeAccessApi(
        access=AccessInfo(activation_status=1, top_up_completed=False, access_info_exists=False),
        preproduction=preproduction(),
    )

    snapshot = make_manager(
        api,
        FakeAccessSecrets(),
        mode=CustomerMode.SANDBOX,
    ).synchronize("jwt-" + "value")

    assert snapshot.search_available is True
    assert snapshot.ticketing_available is True
