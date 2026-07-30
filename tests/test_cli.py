from typer.testing import CliRunner

from opik_to_bt.cli import app


def test_migration_is_the_top_level_bt_only_command() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "[OPTIONS]" in result.output
    assert "--projects" in result.output
    assert "Commands" not in result.output
    assert "--writer" not in result.output


def test_migrate_subcommand_is_not_accepted() -> None:
    result = CliRunner().invoke(app, ["migrate"])

    assert result.exit_code != 0
    assert "unexpected extra argument" in result.output.lower()
