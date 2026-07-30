from typer.testing import CliRunner

from opik_migrate.cli import app


def test_migrate_is_an_explicit_bt_only_command() -> None:
    result = CliRunner().invoke(app, ["migrate", "--help"])

    assert result.exit_code == 0
    assert "Usage: root migrate" in result.output
    assert "--writer" not in result.output
