from __future__ import annotations

import math
from typing import Any

from opik_to_bt.util import as_dict, compact, isoformat, jsonable, unix_seconds


def source_id(prefix: str, value: Any) -> str:
    return f"opik:{prefix}:{value}"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def feedback_fields(raw: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    """Split Opik feedback into Braintrust scores and arbitrary numeric metrics."""
    scores: dict[str, float] = {}
    metrics: dict[str, float] = {}
    for feedback in raw.get("feedback_scores") or raw.get("scores") or []:
        feedback = as_dict(feedback)
        name = feedback.get("name")
        value = _number(feedback.get("value"))
        if name is None or value is None:
            continue
        destination = scores if 0 <= value <= 1 else metrics
        destination[str(name)] = value
    return scores, metrics


def feedback_score_results(
    raw: dict[str, Any],
) -> list[tuple[str, float, dict[str, Any]]]:
    results = []
    for feedback in raw.get("feedback_scores") or raw.get("scores") or []:
        feedback = as_dict(feedback)
        name = feedback.get("name")
        value = _number(feedback.get("value"))
        if name is None or value is None or not 0 <= value <= 1:
            continue
        metadata = {
            str(key): jsonable(field_value)
            for key, field_value in feedback.items()
            if key not in {"name", "value"} and field_value is not None
        }
        results.append((str(name), value, metadata))
    return results


def usage_metrics(usage: Any) -> dict[str, float]:
    """Normalize Opik token names while retaining other numeric usage counters."""
    raw = as_dict(usage) if usage else {}
    metrics = {
        str(name): value
        for name, raw_value in raw.items()
        if (value := _number(raw_value)) is not None
    }
    aliases = (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("tokens", "total_tokens"),
    )
    for canonical, alternate in aliases:
        value = _number(raw.get(canonical))
        if value is None:
            value = _number(raw.get(alternate))
        if value is not None:
            metrics[canonical] = value
    if "tokens" not in metrics and {"prompt_tokens", "completion_tokens"} <= metrics.keys():
        metrics["tokens"] = metrics["prompt_tokens"] + metrics["completion_tokens"]
    if "total_tokens" not in metrics and "tokens" in metrics:
        metrics["total_tokens"] = metrics["tokens"]
    return metrics


def standard_metrics(
    raw: dict[str, Any],
    *,
    include_duration: bool,
) -> dict[str, float]:
    metrics = usage_metrics(raw.get("usage"))
    if include_duration and (duration := _number(raw.get("duration"))) is not None:
        metrics["duration"] = duration / 1000
    if (ttft := _number(raw.get("ttft"))) is not None:
        metrics["time_to_first_token"] = ttft / 1000
    if (cost := _number(raw.get("total_estimated_cost"))) is not None:
        metrics["estimated_cost"] = cost
    return metrics


def timing_metrics(raw: dict[str, Any]) -> dict[str, float]:
    """Build Braintrust timing fields, preferring Opik's measured duration."""
    start = unix_seconds(raw.get("start_time"))
    duration = _number(raw.get("duration"))
    end = start + duration / 1000 if start is not None and duration is not None else None
    if end is None:
        end = unix_seconds(raw.get("end_time"))
    return compact({"start": start, "end": end})


def data_input(data: dict[str, Any]) -> Any:
    input_value = data.get("input")
    if input_value is not None:
        return input_value
    return {
        key: value
        for key, value in data.items()
        if key not in {"id", "expected", "expected_output", "metadata"}
    }


def test_suite_result(
    raw: dict[str, Any],
) -> tuple[str, float, dict[str, Any]] | None:
    assertions = []
    for assertion in raw.get("assertion_results") or []:
        assertion = as_dict(assertion)
        assertions.append(
            compact(
                {
                    "text": assertion.get("value"),
                    "passed": assertion.get("passed"),
                    "reason": assertion.get("reason"),
                }
            )
        )
    status = str(raw.get("status") or "").lower()
    if status in {"passed", "failed"}:
        score = 1.0 if status == "passed" else 0.0
    elif assertions and all(item.get("passed") is True for item in assertions):
        score = 1.0
    elif assertions:
        score = 0.0
    else:
        return None
    passed_count = sum(item.get("passed") is True for item in assertions)
    return (
        "Test suite passed",
        score,
        compact(
            {
                "assertions": assertions,
                "assertions_passed": passed_count,
                "assertions_total": len(assertions),
                "opik": {
                    "status": status or None,
                    "execution_policy": raw.get("execution_policy"),
                },
            },
        ),
    )


def legacy_assertion_count(raw: dict[str, Any]) -> int:
    """Count assertion spans emitted by versions before aggregate suite scoring."""
    return sum(
        assertion.get("value") is not None and isinstance(assertion.get("passed"), bool)
        for item in raw.get("assertion_results") or []
        if (assertion := as_dict(item))
    )


def dataset_event(item: Any) -> dict[str, Any]:
    raw = jsonable(as_dict(item))
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    item_id = raw.get("id") or data.get("id")
    expected = data.get("expected_output", data.get("expected"))
    return compact(
        {
            "id": source_id("dataset-item", item_id),
            "input": data_input(data),
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
    _, feedback_metrics = feedback_fields(raw)
    item_id = raw.get("id") or raw.get("trace_id") or raw.get("dataset_item_id")
    event_id = source_id("experiment-item", item_id)
    input_value = raw.get("input")
    if input_value is None:
        input_value = data_input(data)
    output = raw.get("evaluation_task_output")
    if output is None:
        output = raw.get("output")
    event = {
        "id": event_id,
        "span_id": event_id,
        "root_span_id": event_id,
        "span_parents": [],
        "is_root": True,
        "span_attributes": {"name": "eval", "type": "eval"},
        "input": input_value,
        "expected": data.get("expected_output", data.get("expected")),
        "output": output,
        "metrics": {
            **feedback_metrics,
            **standard_metrics(raw, include_duration=True),
        },
        "metadata": {
            **(raw.get("metadata") or {}),
            "opik": {
                "item_id": item_id,
                "trace_id": raw.get("trace_id"),
                "dataset_item_id": raw.get("dataset_item_id"),
                "feedback_scores": raw.get("feedback_scores") or raw.get("scores"),
            },
        },
    }
    return compact(event)


def experiment_events(item: Any) -> list[dict[str, Any]]:
    """Map an experiment result and its Opik assertions to native scorer spans."""
    raw = jsonable(as_dict(item))
    root = experiment_event(raw)
    root_id = root["id"]
    scorer_input = compact(
        {
            "input": root.get("input"),
            "output": root.get("output"),
            "expected": root.get("expected"),
            "metadata": root.get("metadata"),
        }
    )
    created = isoformat(raw.get("created_at"))
    score_spans = []
    feedback_results = feedback_score_results(raw)
    for index, (name, score, metadata) in enumerate(feedback_results):
        score_id = source_id("experiment-score", f"{root_id}:{index}")
        score_spans.append(
            compact(
                {
                    "id": score_id,
                    "span_id": score_id,
                    "root_span_id": root_id,
                    "span_parents": [root_id],
                    "is_root": False,
                    "span_attributes": {
                        "name": name,
                        "type": "score",
                        "purpose": "scorer",
                    },
                    "input": scorer_input,
                    "output": {"score": score},
                    "scores": {name: score},
                    "metadata": metadata,
                    "created": created,
                }
            )
        )

    assertion_count = legacy_assertion_count(raw)
    for index in range(len(feedback_results), len(feedback_results) + assertion_count):
        score_spans.append(
            {
                "id": source_id("experiment-score", f"{root_id}:{index}"),
                "_object_delete": True,
            }
        )

    suite_result = test_suite_result(raw)
    if suite_result is not None:
        name, score, metadata = suite_result
        index = len(feedback_results) + assertion_count
        score_id = source_id("experiment-score", f"{root_id}:{index}")
        score_spans.append(
            compact(
                {
                    "id": score_id,
                    "span_id": score_id,
                    "root_span_id": root_id,
                    "span_parents": [root_id],
                    "is_root": False,
                    "span_attributes": {
                        "name": name,
                        "type": "score",
                        "purpose": "scorer",
                    },
                    "input": scorer_input,
                    "output": {"score": score},
                    "scores": {name: score},
                    "metadata": metadata,
                    "created": created,
                }
            )
        )
    return [root, *score_spans]


def trace_event(
    trace: Any,
    *,
    include_aggregate_metrics: bool = True,
    spans: list[Any] | None = None,
) -> dict[str, Any]:
    raw_trace = jsonable(as_dict(trace))
    trace_id = source_id("trace", raw_trace["id"])
    scores, feedback_metrics = feedback_fields(raw_trace)
    name = raw_trace.get("name") or "Opik trace"
    timing = timing_metrics(raw_trace)
    if spans:
        child_timings = [timing_metrics(jsonable(as_dict(span))) for span in spans]
        child_starts = [item["start"] for item in child_timings if "start" in item]
        child_ends = [item["end"] for item in child_timings if "end" in item]
        if child_starts and child_ends:
            timing = {"start": min(child_starts), "end": max(child_ends)}
    return compact(
        {
            "id": trace_id,
            "span_id": trace_id,
            "root_span_id": trace_id,
            "span_parents": [],
            "is_root": True,
            "span_attributes": {"name": name, "type": "task"},
            "input": raw_trace.get("input"),
            "output": raw_trace.get("output"),
            "error": raw_trace.get("error_info"),
            "scores": scores or None,
            "metadata": {
                **(raw_trace.get("metadata") or {}),
                "opik": {
                    "trace_id": raw_trace["id"],
                    "project_name": raw_trace.get("project_name"),
                    "tags": raw_trace.get("tags"),
                    "feedback_scores": raw_trace.get("feedback_scores"),
                    "aggregate_usage": raw_trace.get("usage"),
                    "aggregate_estimated_cost": raw_trace.get("total_estimated_cost"),
                    "duration_ms": raw_trace.get("duration"),
                    "ttft_ms": raw_trace.get("ttft"),
                },
            },
            "created": isoformat(raw_trace.get("start_time")),
            "metrics": {
                **timing,
                **feedback_metrics,
                **(
                    standard_metrics(raw_trace, include_duration=False)
                    if include_aggregate_metrics
                    else {}
                ),
            },
        }
    )


def span_event(trace_id: Any, span: Any) -> dict[str, Any]:
    raw = jsonable(as_dict(span))
    scores, feedback_metrics = feedback_fields(raw)
    root_id = source_id("trace", trace_id)
    parent_id = raw.get("parent_span_id")
    parent = source_id("span", parent_id) if parent_id else root_id
    span_type = {
        "llm": "llm",
        "tool": "tool",
        "guardrail": "classifier",
    }.get(str(raw.get("type", "")).lower(), "task")
    name = raw.get("name") or "Opik span"
    return compact(
        {
            "id": source_id("span", raw["id"]),
            "span_id": source_id("span", raw["id"]),
            "root_span_id": root_id,
            "span_parents": [parent],
            "is_root": False,
            "span_attributes": {"name": name, "type": span_type},
            "input": raw.get("input"),
            "output": raw.get("output"),
            "error": raw.get("error_info"),
            "scores": scores or None,
            "metadata": {
                **(raw.get("metadata") or {}),
                "opik": {
                    "span_id": raw["id"],
                    "trace_id": trace_id,
                    "model": raw.get("model"),
                    "provider": raw.get("provider"),
                    "tags": raw.get("tags"),
                    "feedback_scores": raw.get("feedback_scores"),
                    "duration_ms": raw.get("duration"),
                    "ttft_ms": raw.get("ttft"),
                },
            },
            "created": isoformat(raw.get("start_time")),
            "metrics": {
                **timing_metrics(raw),
                **feedback_metrics,
                **standard_metrics(raw, include_duration=False),
            },
        }
    )


def trace_events(trace: Any, spans: list[Any]) -> list[dict[str, Any]]:
    raw_trace = jsonable(as_dict(trace))
    return [
        trace_event(trace, include_aggregate_metrics=not spans, spans=spans),
        *[span_event(raw_trace["id"], span) for span in spans],
    ]
