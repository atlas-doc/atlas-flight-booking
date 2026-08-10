from atlas_cli import booking_runtime, cli


class CapturingApiClient:
    stores: list[object | None] = []

    def __init__(self, settings: object, *, credential_store: object | None = None) -> None:
        del settings
        self.stores.append(credential_store)


def test_cli_control_api_builders_enable_session_refresh(monkeypatch) -> None:
    store = object()
    CapturingApiClient.stores = []
    monkeypatch.setattr(cli, "KeyringSecretStore", lambda: store)
    monkeypatch.setattr(cli, "AtlasApiClient", CapturingApiClient)

    cli.build_auth_service()
    cli.build_doctor_service()
    cli.build_search_service()

    assert CapturingApiClient.stores == [store, store, store]


def test_booking_runtime_control_api_enables_session_refresh(monkeypatch) -> None:
    store = object()
    CapturingApiClient.stores = []
    monkeypatch.setattr(booking_runtime, "KeyringSecretStore", lambda: store)
    monkeypatch.setattr(booking_runtime, "AtlasApiClient", CapturingApiClient)

    booking_runtime.build_booking_runtime()

    assert CapturingApiClient.stores == [store]
