import json
from pathlib import Path
from types import TracebackType

from atlas_cli.config import ConfigStore, InternalSettings


class RecordingLock:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> "RecordingLock":
        self._events.append("entered")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._events.append("exited")


def test_update_uses_lock_and_atomically_preserves_existing_fields(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text('{"retained":"yes"}\n', encoding="utf-8")
    events: list[str] = []

    def lock_factory(*args: object, **kwargs: object) -> RecordingLock:
        return RecordingLock(events)

    monkeypatch.setattr("atlas_cli.config.portalocker.Lock", lock_factory)
    store = ConfigStore(tmp_path)

    merged = store.update({"enabled": True})

    assert events == ["entered", "exited"]
    assert merged == {"retained": "yes", "enabled": True}
    assert json.loads(config_file.read_text(encoding="utf-8")) == merged
    assert list(tmp_path.glob("*.tmp")) == []


def test_probe_removes_its_temporary_file(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)

    assert store.probe() is True
    assert list(tmp_path.iterdir()) == []


def test_customer_mode_defaults_to_prod_and_round_trips_supported_values(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)

    assert store.load_customer_mode() == "prod"

    store.save_customer_mode("sandbox")
    assert store.load_customer_mode() == "sandbox"

    store.save_customer_mode("prod")
    assert store.load_customer_mode() == "prod"


def test_customer_mode_rejects_unknown_values_without_overwriting_config(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.update({"retained": "yes"})

    try:
        store.save_customer_mode("testing")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported customer mode must fail")

    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"retained": "yes"}


def test_default_store_reads_legacy_mode_and_migrates_on_next_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    legacy = tmp_path / "atlas-cli"
    current = tmp_path / "atlas-flight-booking"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"customer_mode":"sandbox"}\n', encoding="utf-8")
    monkeypatch.setattr("atlas_cli.config.user_config_path", lambda name: tmp_path / name)
    store = ConfigStore()

    assert store.load_customer_mode() == "sandbox"

    store.save_customer_mode("prod")
    assert json.loads((current / "config.json").read_text(encoding="utf-8")) == {
        "customer_mode": "prod"
    }


def test_internal_settings_centralize_business_api_hosts() -> None:
    settings = InternalSettings()

    assert settings.prod_api_base_url == settings.control_api_base_url
    assert settings.prod_api_base_url == "https://api-sg.atriptech.com"
    assert settings.sandbox_api_base_url == "https://sandbox.atriptech.com"
    assert settings.authorization_page_url == "https://www.atriptech.com/#/login"
    assert settings.subscription_page_url == "https://www.atriptech.com/#/skill-entry"
    assert settings.order_detail_url_template == "https://www.atriptech.com/#/order/detail/{order_no}/en"
    assert settings.ticketing_poll_max_seconds == 120.0
