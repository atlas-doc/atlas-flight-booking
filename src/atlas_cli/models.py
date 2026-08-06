"""Stable public result models and process exit-code mapping."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class CommandStatus(StrEnum):
    SUCCESS = "success"
    ACTION_REQUIRED = "action_required"
    RETRYABLE_ERROR = "retryable_error"
    TERMINAL_ERROR = "terminal_error"


class ExitCode(IntEnum):
    OK = 0
    INVALID_ARGUMENT = 2
    RETRYABLE = 20
    TERMINAL = 30


class CommandResult(BaseModel):
    schema_version: Literal["1"] = "1"
    status: CommandStatus
    code: str
    message: str
    retryable: bool = False
    request_id: str | None = None
    data: dict[str, object] = Field(default_factory=dict)
    details: dict[str, object] = Field(default_factory=dict)


def _result(
    status: CommandStatus,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
    data: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
) -> CommandResult:
    return CommandResult(
        status=status,
        code=code,
        message=message,
        retryable=retryable,
        request_id=request_id,
        data=data or {},
        details=details or {},
    )


def success_result(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    data: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
) -> CommandResult:
    return _result(CommandStatus.SUCCESS, code, message, request_id=request_id, data=data, details=details)


def action_required_result(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    data: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
) -> CommandResult:
    return _result(CommandStatus.ACTION_REQUIRED, code, message, request_id=request_id, data=data, details=details)


def retryable_error_result(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    data: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
) -> CommandResult:
    return _result(
        CommandStatus.RETRYABLE_ERROR,
        code,
        message,
        retryable=True,
        request_id=request_id,
        data=data,
        details=details,
    )


def terminal_error_result(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    data: dict[str, object] | None = None,
    details: dict[str, object] | None = None,
) -> CommandResult:
    return _result(CommandStatus.TERMINAL_ERROR, code, message, request_id=request_id, data=data, details=details)


def exit_code_for(result: CommandResult) -> ExitCode:
    if result.code == "INVALID_ARGUMENT":
        return ExitCode.INVALID_ARGUMENT
    if result.status in {CommandStatus.SUCCESS, CommandStatus.ACTION_REQUIRED}:
        return ExitCode.OK
    if result.status is CommandStatus.RETRYABLE_ERROR:
        return ExitCode.RETRYABLE
    return ExitCode.TERMINAL
