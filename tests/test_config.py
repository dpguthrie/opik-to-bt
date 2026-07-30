from datetime import UTC

import pytest

from opik_to_bt.config import Resource, parse_csv, parse_datetime, parse_resources


def test_parse_csv_and_resources() -> None:
    assert parse_csv(" alpha, beta,alpha ") == {"alpha", "beta"}
    assert parse_resources("datasets,logs") == {Resource.DATASETS, Resource.LOGS}
    assert parse_resources("all") == set(Resource)


def test_parse_datetime_normalizes_utc() -> None:
    parsed = parse_datetime("2026-01-02T03:04:05")
    assert parsed.tzinfo == UTC
    assert parsed.isoformat() == "2026-01-02T03:04:05+00:00"


def test_invalid_resource_is_clear() -> None:
    with pytest.raises(ValueError, match="Resources must be"):
        parse_resources("prompts")
