from datetime import UTC

import pytest

from opik_to_bt.config import (
    PromptHistory,
    Resource,
    Settings,
    parse_csv,
    parse_datetime,
    parse_resources,
)


def test_parse_csv_and_resources() -> None:
    assert parse_csv(" alpha, beta,alpha ") == {"alpha", "beta"}
    assert parse_resources("datasets,logs,prompts") == {
        Resource.DATASETS,
        Resource.LOGS,
        Resource.PROMPTS,
    }
    assert parse_resources("all") == {
        Resource.DATASETS,
        Resource.EXPERIMENTS,
        Resource.LOGS,
        Resource.PROMPTS,
    }
    assert parse_resources("all,scorers,online-evals") == {
        Resource.DATASETS,
        Resource.EXPERIMENTS,
        Resource.LOGS,
        Resource.PROMPTS,
        Resource.SCORERS,
        Resource.ONLINE_EVALS,
    }


def test_parse_datetime_normalizes_utc() -> None:
    parsed = parse_datetime("2026-01-02T03:04:05")
    assert parsed.tzinfo == UTC
    assert parsed.isoformat() == "2026-01-02T03:04:05+00:00"


def test_invalid_resource_is_clear() -> None:
    with pytest.raises(ValueError, match="Resources must be"):
        parse_resources("unknown")


def test_prompt_history_defaults_to_latest_and_reads_environment(monkeypatch) -> None:
    assert Settings(_env_file=None).prompt_history == PromptHistory.LATEST
    monkeypatch.setenv("OPIK_TO_BT_PROMPT_HISTORY", "all")
    assert Settings(_env_file=None).prompt_history == PromptHistory.ALL
