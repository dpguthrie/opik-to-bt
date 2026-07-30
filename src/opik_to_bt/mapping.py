from __future__ import annotations

from typing import Any

from opik_to_bt.util import as_dict, compact, isoformat, jsonable, unix_seconds


def source_id(prefix: str, value: Any) -> str:
    return f"opik:{prefix}:{value}"


def dataset_event(item: Any) -> dict[str, Any]:
    raw = jsonable(as_dict(item))
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    item_id = raw.get("id") or data.get("id")
    expected = data.get("expected_output", data.get("expected"))
    input_value = data.get("input")
    if input_value is None:
        input_value = {
            key: value
            for key, value in data.items()
            if key not in {"id", "expected", "expected_output", "metadata"}
        }
    return compact(
        {
            "id": source_id("dataset-item", item_id),
            "input": input_value,
            "expected": expected,
            "metadata": {
                **(data.get("metadata") or {}),
                "opik": {"item_id": item_id},
            },
        }
    )


def experiment_event(item: Any) -> dict[str, Any]:
    raw = jsonable(as_dict(item))
    data = raw.get("dataset_item_data") or raw.get("data") or {}
    scores: dict[str, float] = {}
    invalid_scores: dict[str, Any] = {}
    for score in raw.get("feedback_scores") or raw.get("scores") or []:
        score = as_dict(score)
        name, value = score.get("name"), score.get("value")
        if name is None or value is None:
            continue
        if 0 <= float(value) <= 1:
            scores[str(name)] = float(value)
        else:
            invalid_scores[str(name)] = value
    item_id = raw.get("id") or raw.get("trace_id") or raw.get("dataset_item_id")
    event = {
        "id": source_id("experiment-item", item_id),
        "input": data.get("input"),
        "expected": data.get("expected_output", data.get("expected")),
        "output": raw.get("evaluation_task_output", raw.get("output")),
        "scores": scores or None,
        "metadata": {
            **(raw.get("metadata") or {}),
            "opik": {
                "item_id": item_id,
                "trace_id": raw.get("trace_id"),
                "dataset_item_id": raw.get("dataset_item_id"),
                "scores_outside_zero_to_one": invalid_scores or None,
            },
        },
    }
    return compact(event)


def trace_event(trace: Any) -> dict[str, Any]:
    raw_trace = jsonable(as_dict(trace))
    trace_id = source_id("trace", raw_trace["id"])
    return compact(
        {
            "id": trace_id,
            "span_id": trace_id,
            "root_span_id": trace_id,
            "span_parents": [],
            "is_root": True,
            "name": raw_trace.get("name") or "Opik trace",
            "input": raw_trace.get("input"),
            "output": raw_trace.get("output"),
            "error": raw_trace.get("error_info"),
            "metadata": {
                **(raw_trace.get("metadata") or {}),
                "opik": {
                    "trace_id": raw_trace["id"],
                    "project_name": raw_trace.get("project_name"),
                    "tags": raw_trace.get("tags"),
                },
            },
            "created": isoformat(raw_trace.get("start_time")),
            "metrics": {
                "start": unix_seconds(raw_trace.get("start_time")),
                "end": unix_seconds(raw_trace.get("end_time")),
                **(raw_trace.get("usage") or {}),
            },
        }
    )


def span_event(trace_id: Any, span: Any) -> dict[str, Any]:
    raw = jsonable(as_dict(span))
    root_id = source_id("trace", trace_id)
    parent_id = raw.get("parent_span_id")
    parent = source_id("span", parent_id) if parent_id else root_id
    span_type = {
        "llm": "llm",
        "tool": "tool",
        "guardrail": "classifier",
    }.get(str(raw.get("type", "")).lower(), "task")
    return compact(
        {
            "id": source_id("span", raw["id"]),
            "span_id": source_id("span", raw["id"]),
            "root_span_id": root_id,
            "span_parents": [parent],
            "is_root": False,
            "name": raw.get("name") or "Opik span",
            "span_attributes": {"type": span_type},
            "input": raw.get("input"),
            "output": raw.get("output"),
            "error": raw.get("error_info"),
            "metadata": {
                **(raw.get("metadata") or {}),
                "opik": {
                    "span_id": raw["id"],
                    "trace_id": trace_id,
                    "model": raw.get("model"),
                    "provider": raw.get("provider"),
                    "tags": raw.get("tags"),
                },
            },
            "created": isoformat(raw.get("start_time")),
            "metrics": {
                "start": unix_seconds(raw.get("start_time")),
                "end": unix_seconds(raw.get("end_time")),
                **(raw.get("usage") or {}),
            },
        }
    )


def trace_events(trace: Any, spans: list[Any]) -> list[dict[str, Any]]:
    raw_trace = jsonable(as_dict(trace))
    return [
        trace_event(trace),
        *[span_event(raw_trace["id"], span) for span in spans],
    ]
