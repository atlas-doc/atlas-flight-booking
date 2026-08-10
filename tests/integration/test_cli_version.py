from typer.testing import CliRunner

from atlas_cli.cli import app

runner = CliRunner()


def test_version_is_local_plain_text() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == "atlas-flight 0.3.8\n"
    assert result.stderr == ""
