import json
import socket
from pathlib import Path

from test_booking_flow import _offer, _passengers, _runtime, _script
from typer.testing import CliRunner

from atlas_cli.cli import app
from atlas_cli.models import action_required_result, success_result

runner = CliRunner()


class OfflineAuthService:
    def login(self):
        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Complete authorization in the browser",
            data={"authorization_url": "https://web.example.invalid/authorize", "expires_at": "2099-01-01 00:00:00"},
        )

    def poll(self, timeout_seconds: int):
        return action_required_result(
            "AUTH_PENDING",
            "Authorization is still pending",
            data={"timeout_used": timeout_seconds},
        )

    def status(self):
        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Authorization required",
            data={"authenticated": False},
        )


class OfflineDoctorService:
    def run(self):
        return success_result(
            "DOCTOR_OK",
            "Readiness checks passed",
            data={"checks": {"offline": True}},
        )


class OfflineSearchService:
    def search(self, request):
        return action_required_result(
            "AUTHORIZATION_REQUIRED",
            "Authorization required",
        )

    def list_offers(self, search_id: str):
        return success_result(
            "OFFERS_LISTED",
            "Offers listed",
            data={"search_id": search_id, "offer_count": 0, "offers": []},
        )


def test_local_and_mocked_cli_surfaces_never_open_a_socket(monkeypatch) -> None:
    def deny_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("test attempted a live network connection")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr("atlas_cli.cli.build_auth_service", OfflineAuthService)
    monkeypatch.setattr("atlas_cli.cli.build_doctor_service", OfflineDoctorService)
    monkeypatch.setattr("atlas_cli.cli.build_search_service", OfflineSearchService)

    commands = [
        ["--version"],
        ["auth", "login", "--json"],
        ["auth", "poll", "--timeout", "1", "--json"],
        ["auth", "status", "--json"],
        ["doctor", "--json"],
        ["search", "--json"],
        ["offer", "list", "--search-id", "srch_example", "--json"],
    ]
    results = [runner.invoke(app, command) for command in commands]

    assert [result.exit_code for result in results] == [0, 0, 0, 0, 0, 0, 0]
    assert all(result.stderr == "" for result in results)


def test_complete_booking_flow_uses_only_mock_transport(monkeypatch, tmp_path: Path) -> None:
    """Replacing MockTransport with a live client would hit the socket-deny harness."""
    script = _script(include_ancillaries=False)
    runtime = _runtime(tmp_path, script, offer=_offer(ancillary_supported=()))
    monkeypatch.setattr("atlas_cli.cli.build_booking_runtime", lambda: runtime)

    verified = runner.invoke(app, ["offer", "verify", "--offer-id", "off_3", "--json"])
    assert verified.exit_code == 0
    verified_json = json.loads(verified.stdout)
    booking_id = str(verified_json["data"]["booking_id"])
    traveler_id = str(verified_json["data"]["travelers"][0]["traveler_id"])
    created = runner.invoke(
        app,
        ["order", "create", "--booking-id", booking_id, "--passengers-stdin", "--json"],
        input=_passengers(traveler_id),
    )
    assert created.exit_code == 0
    confirmation_id = str(json.loads(created.stdout)["data"]["payment_confirmation_id"])
    paid = runner.invoke(app, ["order", "pay", "--confirmation-id", confirmation_id, "--json"])

    assert paid.exit_code == 0
    assert script.paths == ["/verify.do", "/order.do", "/pay.do", "/queryOrderDetails.do"]
