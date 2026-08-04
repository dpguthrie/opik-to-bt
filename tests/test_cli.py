import re

from typer.testing import CliRunner

from opik_to_bt.cli import app

# Typer forces a terminal when GITHUB_ACTIONS is set, and rich then styles flag
# names in pieces (`--projects` arrives as two separate spans), so assertions run
# against the plain text rather than the decorated output.
ANSI_CODES = re.compile(r"\x1b\[[0-9;]*m")


def render(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, ANSI_CODES.sub("", result.output)


def test_migration_is_the_top_level_bt_only_command() -> None:
    exit_code, output = render("--help")

    assert exit_code == 0
    assert "Usage:" in output
    assert "[OPTIONS]" in output
    assert "--projects" in output
    assert "Commands" not in output
    assert "--writer" not in output


def test_migrate_subcommand_is_not_accepted() -> None:
    exit_code, output = render("migrate")

    assert exit_code != 0
    assert "unexpected extra argument" in output.lower()
