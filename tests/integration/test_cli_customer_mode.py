from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_cli import cli as cli_module
from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.cli import app
from atlas_cli.config import ConfigStore
from atlas_cli.endpoints import CustomerMode

runner = CliRunner()


def test_hidden_customer_mode_command_persists_sandbox_without_exposing_it_in_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    monkeypatch.setattr("atlas_cli.cli.ConfigStore", lambda: store)

    result = runner.invoke(app, ["environment", "use", "sandbox", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["code"] == "CONFIGURATION_UPDATED"
    assert payload["data"] == {}
    assert "sandbox" not in result.stdout.lower()
    assert store.load_customer_mode() == "sandbox"


def test_hidden_customer_mode_command_restores_production_with_neutral_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    store.save_customer_mode("sandbox")
    monkeypatch.setattr("atlas_cli.cli.ConfigStore", lambda: store)

    result = runner.invoke(app, ["environment", "use", "production", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["code"] == "CONFIGURATION_UPDATED"
    assert payload["data"] == {}
    assert "production" not in result.stdout.lower()
    assert store.load_customer_mode() == "prod"


def test_customer_mode_command_rejects_unknown_target_without_changing_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    monkeypatch.setattr("atlas_cli.cli.ConfigStore", lambda: store)

    result = runner.invoke(app, ["environment", "use", "testing", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_ARGUMENT"
    assert payload["details"] == {"field": "target"}
    assert store.load_customer_mode() == "prod"


def test_customer_mode_command_is_absent_from_public_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "environment" not in result.stdout.lower()


def test_booking_runtime_composition_receives_persisted_customer_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    store.save_customer_mode("sandbox")
    sentinel = object()
    captured: list[CustomerMode] = []

    monkeypatch.setattr(cli_module, "ConfigStore", lambda: store)

    def fake_compose(*, mode: CustomerMode) -> BookingRuntime:
        captured.append(mode)
        return sentinel  # type: ignore[return-value]

    monkeypatch.setattr(cli_module, "compose_booking_runtime", fake_compose)

    assert cli_module.build_booking_runtime() is sentinel
    assert captured == [CustomerMode.SANDBOX]


def test_search_composition_uses_persisted_customer_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    store.save_customer_mode("sandbox")
    secure_store = object()

    monkeypatch.setattr(cli_module, "ConfigStore", lambda: store)
    monkeypatch.setattr(cli_module, "KeyringSecretStore", lambda: secure_store)

    service = cli_module.build_search_service()

    assert service._access._mode is CustomerMode.SANDBOX
