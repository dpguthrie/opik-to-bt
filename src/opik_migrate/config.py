from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Resource(StrEnum):
    DATASETS = "datasets"
    EXPERIMENTS = "experiments"
    LOGS = "logs"


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_csv(value: str | None) -> set[str] | None:
    if not value:
        return None
    values = {item.strip() for item in value.split(",") if item.strip()}
    return values or None


def parse_resources(value: str) -> set[Resource]:
    names = parse_csv(value) or {"all"}
    if "all" in names:
        return set(Resource)
    try:
        return {Resource(name) for name in names}
    except ValueError as exc:
        allowed = ", ".join(resource.value for resource in Resource)
        raise ValueError(
            f"Resources must be 'all' or a comma-separated subset of: {allowed}"
        ) from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    opik_url: str = "https://www.comet.com/opik/api"
    opik_api_key: str | None = None
    opik_workspace: str | None = None
    braintrust_url: str = "https://api.braintrust.dev"
    braintrust_api_key: str | None = None

    timeout_seconds: float = Field(60.0, alias="OPIK_MIGRATE_TIMEOUT_SECONDS", gt=0)
    retry_attempts: int = Field(8, alias="OPIK_MIGRATE_RETRY_ATTEMPTS", ge=1)
    page_size: int | None = Field(None, alias="OPIK_MIGRATE_PAGE_SIZE", ge=1, le=2000)
    partition_bytes: int | None = Field(None, alias="OPIK_MIGRATE_PARTITION_BYTES", ge=1024 * 1024)
    resource_workers: int | None = Field(None, alias="OPIK_MIGRATE_RESOURCE_WORKERS", ge=1, le=64)
    buffered_partitions: int | None = Field(
        None, alias="OPIK_MIGRATE_BUFFERED_PARTITIONS", ge=1, le=8
    )
    upload_processes: int | None = Field(None, alias="OPIK_MIGRATE_UPLOAD_PROCESSES", ge=1, le=16)
    bt_workers: int | None = Field(None, alias="OPIK_MIGRATE_BT_WORKERS", ge=1, le=64)

    @field_validator("opik_url", "braintrust_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")
