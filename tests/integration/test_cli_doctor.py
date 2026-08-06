import json

from typer.testing import CliRunner

from atlas_cli.cli import app
from atlas_cli.models import action_required_result

runner = CliRunner()


class FakeDoctorService:
    def run(self):
        return action_required_result(
            "DOCTOR_ISSUES",
            "Authorization required",
            data={
                "checks": {
                    "cli_version": True,
                    "config_directory": True,
                    "secure_store": True,
                    "api_reachable": True,
                    "authenticated": False,
                }
            },
        )


def test_doctor_json_writes_exactly_one_sanitized_stdout_object(monkeypatch) -> None:
    monkeypatch.setattr("atlas_cli.cli.build_doctor_service", FakeDoctorService)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "DOCTOR_ISSUES"
    assert payload["data"]["checks"]["authenticated"] is False

