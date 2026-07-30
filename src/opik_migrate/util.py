from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    for method in ("model_dump", "dict", "to_dict"):
        candidate = getattr(value, method, None)
        if callable(candidate):
            result = candidate()
            if isinstance(result, dict):
                return result
    return {key: item for key, item in vars(value).items() if not key.startswith("_")}


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return isoformat(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [jsonable(item) for item in value]
    return jsonable(as_dict(value))


def isoformat(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def unix_seconds(value: str | datetime | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value
