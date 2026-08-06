import json

from atlas_cli.models import (
    ExitCode,
    action_required_result,
    exit_code_for,
    retryable_error_result,
    success_result,
)
from atlas_cli.output import render_json


def test_success_envelope_has_stable_shape() -> None:
    result = success_result("AUTHORIZED", "Authorization active", data={"authenticated": True})
    payload = json.loads(render_json(result))
    assert payload == {
        "schema_version": "1",
        "status": "success",
        "code": "AUTHORIZED",
        "message": "Authorization active",
        "retryable": False,
        "request_id": None,
        "data": {"authenticated": True},
        "details": {},
    }
    assert exit_code_for(result) is ExitCode.OK


def test_action_required_is_exit_zero() -> None:
    result = action_required_result("AUTHORIZATION_REQUIRED", "Authorization required")
    assert exit_code_for(result) is ExitCode.OK


def test_retryable_error_is_exit_twenty() -> None:
    result = retryable_error_result("AUTH_SERVICE_UNAVAILABLE", "Service temporarily unavailable")
    assert exit_code_for(result) is ExitCode.RETRYABLE

