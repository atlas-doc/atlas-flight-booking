"""Internal settings and atomic non-secret configuration storage."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import portalocker
from platformdirs import user_config_path


@dataclass(frozen=True)
class InternalSettings:
    control_api_base_url: str = "https://api-sg.atriptech.com"
    prod_api_base_url: str = "https://api-sg.atriptech.com"
    sandbox_api_base_url: str = "https://sandbox.atriptech.com"
    authorization_page_url: str = "https://www.atriptech.com/#/login"
    subscription_page_url: str = "https://www.atriptech.com/#/skill-entry"
    order_detail_url_template: str = "https://www.atriptech.com/#/order/detail/{order_no}/en"
    server_timezone: str = "Asia/Shanghai"
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    poll_interval_seconds: float = 2.0
    ticketing_poll_max_seconds: float = 120.0


class ConfigStore:
    CUSTOMER_MODE_KEY = "customer_mode"
    CUSTOMER_MODES = frozenset({"prod", "sandbox"})

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or Path(user_config_path("atlas-flight-booking"))
        self.config_file = self.directory / "config.json"
        self._lock_file = self.directory / "config.lock"
        self._legacy_config_file = (
            None if directory is not None else Path(user_config_path("atlas-cli")) / "config.json"
        )

    def update(self, values: Mapping[str, object]) -> dict[str, object]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self._lock_file), mode="a", timeout=10):
            current = self._read()
            current.update(values)
            self._atomic_write(current)
            return current

    def load_customer_mode(self) -> str:
        value = self._read().get(self.CUSTOMER_MODE_KEY, "prod")
        return value if isinstance(value, str) and value in self.CUSTOMER_MODES else "prod"

    def save_customer_mode(self, value: str) -> None:
        if value not in self.CUSTOMER_MODES:
            raise ValueError("Unsupported Atlas configuration")
        self.update({self.CUSTOMER_MODE_KEY: value})

    def probe(self) -> bool:
        probe_path: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".probe-", dir=self.directory)
            probe_path = Path(name)
            with os.fdopen(descriptor, "wb") as probe:
                probe.write(b"ok")
                probe.flush()
                os.fsync(probe.fileno())
            probe_path.unlink()
            return True
        except OSError:
            if probe_path is not None:
                probe_path.unlink(missing_ok=True)
            return False

    def _read(self) -> dict[str, object]:
        source = self.config_file
        if not source.exists() and self._legacy_config_file is not None:
            source = self._legacy_config_file
        if not source.exists():
            return {}
        loaded = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Atlas configuration must contain a JSON object")
        return loaded

    def _atomic_write(self, value: dict[str, object]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".config-",
                suffix=".tmp",
                dir=self.directory,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(value, temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.config_file)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
