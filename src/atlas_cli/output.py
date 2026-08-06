"""Single terminal-output boundary for command results."""

from __future__ import annotations

import sys
from typing import TextIO

from atlas_cli.models import CommandResult, exit_code_for


def render_json(result: CommandResult) -> str:
    return result.model_dump_json() + "\n"


class OutputWriter:
    def __init__(self, stdout: TextIO | None = None) -> None:
        self._stdout = stdout

    def write(self, result: CommandResult, *, json_output: bool = True) -> int:
        stdout = self._stdout or sys.stdout
        stdout.write(render_json(result) if json_output else f"{result.message}\n")
        stdout.flush()
        return int(exit_code_for(result))

