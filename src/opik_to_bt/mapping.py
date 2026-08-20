from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any

from opik_to_bt.util import as_dict, compact, isoformat, jsonable, unix_seconds


def source_id(prefix: str, value: Any) -> str:
    return f"opik:{prefix}:{value}"


def tag_list(value: Any) -> list[str] | None:
    """Map an Opik tag list onto Braintrust's tag list, dropping blanks and repeats."""
    if isinstance(value, str):
        value = [value]
    tags = {text: None for tag in value or [] if (text := str(tag).strip())}
    return list(tags) or None


def object_slug(name: str, source_id: str, *, fallback: str = "opik-item") -> str:
    """Build a readable, deterministic Braintrust slug without name collisions."""
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or fallback
    suffix = hashlib.sha256(source_id.encode()).hexdigest()[:8]
    return f"{stem[:80].rstrip('-')}-{suffix}"


def prompt_slug(name: str, source_prompt_id: str) -> str:
    return object_slug(name, source_prompt_id, fallback="opik-prompt")


def scorer_slug(name: str, source_id: str) -> str:
    return object_slug(name, source_id, fallback="opik-scorer")


def prompt_definition(prompt: Any, version: Any) -> dict[str, Any]:
    """Map one Opik prompt snapshot to Braintrust's public prompt schema."""
    container = jsonable(as_dict(prompt))
    snapshot = jsonable(as_dict(version))
    structure = str(
        snapshot.get("template_structure") or container.get("template_structure") or "text"
    ).lower()
    template = snapshot.get("template")
    if not isinstance(template, str):
        raise ValueError(f"Opik prompt {container.get('name')!r} has no string template")

    if structure == "chat":
        try:
            messages = json.loads(template)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Opik chat prompt {container.get('name')!r} has invalid message JSON"
            ) from exc
        if not isinstance(messages, list):
            raise ValueError(
                f"Opik chat prompt {container.get('name')!r} does not contain a message list"
            )
        prompt_block: dict[str, Any] = {"type": "chat", "messages": messages}
    elif structure in {"text", "string"}:
        prompt_block = {"type": "completion", "content": template}
    else:
        raise ValueError(
            f"Opik prompt {container.get('name')!r} has unsupported template "
            f"structure {structure!r}"
        )

    template_type = str(snapshot.get("type") or "mustache").lower()
    template_format = {
        "mustache": "mustache",
        "jinja2": "nunjucks",
    }.get(template_type, "none")
    tags = snapshot.get("tags")
    if tags is None:
        tags = container.get("tags")

    return {
        "name": str(container["name"]),
        # Keep explicit null/empty values because PUT is a full snapshot. Omitting
        # them could retain fields from the preceding Braintrust version.
        "description": container.get("description"),
        "prompt_data": {
            "prompt": prompt_block,
            "template_format": template_format,
        },
        "tags": tag_list(tags) or [],
    }


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
            "tags": tag_list(raw.get("tags")),
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
            "tags": tag_list(raw_trace.get("tags")),
            "metadata": {
                **(raw_trace.get("metadata") or {}),
                **(
                    {"thread_id": raw_trace["thread_id"]}
                    if raw_trace.get("thread_id")
                    and "thread_id" not in (raw_trace.get("metadata") or {})
                    else {}
                ),
                "opik": {
                    "trace_id": raw_trace["id"],
                    "thread_id": raw_trace.get("thread_id"),
                    "project_name": raw_trace.get("project_name"),
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
        "guardrail": "function",
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
            "tags": tag_list(raw.get("tags")),
            "metadata": {
                **(raw.get("metadata") or {}),
                "opik": {
                    "span_id": raw["id"],
                    "trace_id": trace_id,
                    "model": raw.get("model"),
                    "provider": raw.get("provider"),
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


LLM_JUDGE_SCOPES = {
    "llm_as_judge": "trace",
    "span_llm_as_judge": "span",
    "trace_thread_llm_as_judge": "group",
}
PYTHON_METRIC_TYPES = {
    "user_defined_metric_python",
    "span_user_defined_metric_python",
    "trace_thread_user_defined_metric_python",
}
_VARIABLE_ROOTS = {
    "input": "input",
    "output": "output",
    "expected": "expected",
    "expected_output": "expected",
    "context": "thread",
    "metadata": "metadata",
}
_MESSAGE_ROLES = {
    "system": "system",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "custom": "user",
}
_FILTER_FIELDS = {
    "name": "span_attributes.name",
    "type": "span_attributes.type",
    "input": "input",
    "output": "output",
    "error": "error",
    "error_info": "error",
    "tags": "tags",
    "thread_id": "metadata.thread_id",
    "model": "metadata.opik.model",
    "provider": "metadata.opik.provider",
    "duration": "metrics.duration",
    "total_estimated_cost": "metrics.estimated_cost",
}
_MUSTACHE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
THREAD_IDLE_SECONDS = 900.0
NUMERIC_CHOICES = tuple(round(index / 10, 1) for index in range(11))


def evaluator_type(evaluator: Any) -> str:
    raw = as_dict(evaluator)
    return str(raw.get("type") or "").lower()


def evaluator_scope(evaluator: Any) -> str | None:
    kind = evaluator_type(evaluator)
    if kind in LLM_JUDGE_SCOPES:
        return LLM_JUDGE_SCOPES[kind]
    if kind == "span_user_defined_metric_python":
        return "span"
    if kind == "trace_thread_user_defined_metric_python":
        return "group"
    if kind == "user_defined_metric_python":
        return "trace"
    return None


def _sql_literal(value: Any) -> str:
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _sql_field(filter_item: dict[str, Any]) -> str | None:
    field = str(filter_item.get("field") or "").strip()
    key = str(filter_item.get("key") or "").strip()
    if field in {"metadata", "metadata_field"}:
        return f"metadata.{key}" if key else "metadata"
    if field in {"feedback_scores", "feedback_score", "scores"}:
        return f"scores.{key}" if key else None
    mapped = _FILTER_FIELDS.get(field)
    if mapped:
        return mapped
    if field.startswith(("metadata.", "span_attributes.", "scores.", "metrics.")):
        return field
    return None


def filters_to_sql(filters: Any) -> tuple[str | None, str | None]:
    """Compile Opik structured filters to a Braintrust online-scoring SQL clause."""
    clauses = []
    for item in filters or []:
        raw = as_dict(item)
        field = _sql_field(raw)
        operator = str(raw.get("operator") or "=").strip()
        value = raw.get("value")
        if field is None:
            return None, f"filter field {raw.get('field')!r} is not translated"
        if operator in {"is_empty", "is_not_empty"}:
            clauses.append(f"{field} IS NULL" if operator == "is_empty" else f"{field} IS NOT NULL")
            continue
        if value is None:
            return None, f"filter {field} {operator} has no value"
        if operator == "=":
            clauses.append(f"{field} = {_sql_literal(value)}")
        elif operator == "!=":
            clauses.append(f"{field} IS NOT {_sql_literal(value)}")
        elif operator in {">", ">=", "<", "<="}:
            clauses.append(f"{field} {operator} {_sql_literal(value)}")
        elif operator == "contains" and field == "tags":
            clauses.append(f"tags IN ({_sql_literal(value)})")
        elif operator == "contains":
            clauses.append(f"{field} MATCH {_sql_literal(value)}")
        elif operator == "starts_with":
            clauses.append(f"{field} LIKE {_sql_literal(f'{value}%')}")
        elif operator == "ends_with":
            clauses.append(f"{field} LIKE {_sql_literal(f'%{value}')}")
        elif operator == "in":
            values = [part.strip() for part in str(value).split(",") if part.strip()]
            if not values:
                return None, f"filter {field} in has no values"
            joined = ", ".join(_sql_literal(part) for part in values)
            clauses.append(f"{field} IN ({joined})")
        else:
            return None, f"filter operator {operator!r} is not translated"
    return " AND ".join(clauses) or None, None


def _rewrite_variable_path(path: str) -> str | None:
    parts = [part.strip() for part in path.strip().split(".") if part.strip()]
    if not parts:
        return None
    root = _VARIABLE_ROOTS.get(parts[0])
    if root is None:
        return None
    return ".".join([root, *parts[1:]])


def rewrite_judge_template(text: str, variables: dict[str, str] | None) -> str:
    aliases = {str(name): str(path) for name, path in (variables or {}).items()}

    def replace(match: re.Match[str]) -> str:
        original = match.group(1).strip()
        path = aliases.get(original, original)
        rewritten = _rewrite_variable_path(path)
        if rewritten is None:
            return match.group(0)
        return "{{" + rewritten + "}}"

    return _MUSTACHE.sub(replace, text)


def _message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    for part in message.get("content_array") or []:
        part = as_dict(part)
        if part.get("image_url") or part.get("video_url") or part.get("audio_url"):
            return None
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _judge_messages(code: dict[str, Any]) -> tuple[list[dict[str, str]] | None, str | None]:
    messages = []
    variables = {
        str(name): str(path) for name, path in as_dict(code.get("variables") or {}).items() if path
    }
    for item in code.get("messages") or []:
        raw = as_dict(item)
        if raw.get("image_url") or raw.get("video_url") or raw.get("audio_url"):
            return None, "multimodal judge messages are not translated"
        for part in raw.get("content_array") or []:
            if as_dict(part).get("image_url") or as_dict(part).get("video_url"):
                return None, "multimodal judge messages are not translated"
        text = _message_text(raw)
        if text is None:
            continue
        role = _MESSAGE_ROLES.get(str(raw.get("role") or "user").lower())
        if role is None:
            return None, f"judge message role {raw.get('role')!r} is not translated"
        messages.append({"role": role, "content": rewrite_judge_template(text, variables)})
    if not messages:
        return None, "LLM-as-judge rule has no text messages"
    return messages, None


def _choice_scores(schema: dict[str, Any]) -> tuple[dict[str, float], str] | tuple[None, str]:
    kind = str(schema.get("type") or "").upper()
    name = str(schema.get("name") or "score")
    description = str(schema.get("description") or "").strip()
    if kind == "BOOLEAN":
        instruction = f"For {name}, answer with true or false."
        if description:
            instruction = f"{description.rstrip('.')}. {instruction}"
        return {"true": 1.0, "false": 0.0}, instruction
    if kind in {"DOUBLE", "INTEGER"}:
        instruction = (
            f"For {name}, return only one of: "
            + ", ".join(str(choice) for choice in NUMERIC_CHOICES)
            + "."
        )
        if description:
            instruction = f"{description.rstrip('.')}. {instruction}"
        return {str(choice): float(choice) for choice in NUMERIC_CHOICES}, instruction
    return None, f"score schema type {kind or 'missing'!r} is not translated"


def _model_options(model: Any) -> dict[str, Any]:
    raw = as_dict(model) if model and not isinstance(model, str) else {"name": model}
    options: dict[str, Any] = {}
    if name := raw.get("name") or raw.get("model"):
        options["model"] = str(name)
    params = compact(
        {
            "temperature": _number(raw.get("temperature")),
            "seed": raw.get("seed"),
        }
    )
    if params:
        options["params"] = params
    return options


def sampling_rate(evaluator: Any) -> float:
    raw = as_dict(evaluator)
    rate = _number(raw.get("sampling_rate"))
    if rate is None:
        return 1.0
    if rate > 1:
        rate = rate / 100
    return min(1.0, max(0.0, rate))


def scorer_definitions(evaluator: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Map an Opik evaluator into Braintrust LLM-as-judge scorer functions."""
    raw = jsonable(as_dict(evaluator))
    kind = evaluator_type(raw)
    if kind in PYTHON_METRIC_TYPES:
        return [], "custom Python metrics are not auto-translated"
    if kind not in LLM_JUDGE_SCOPES:
        return [], f"evaluator type {kind or 'missing'!r} is not translated"
    code = as_dict(raw.get("code"))
    messages, message_error = _judge_messages(code)
    if message_error:
        return [], message_error
    schema_fields = [as_dict(item) for item in code.get("schema") or code.get("schema_") or []]
    if not schema_fields:
        schema_fields = [{"name": "score", "type": "BOOLEAN", "description": ""}]
    definitions = []
    rule_name = str(raw.get("name") or "Opik scorer")
    source_id_value = str(raw.get("id") or rule_name)
    for field in schema_fields:
        scores, instruction = _choice_scores(field)
        if scores is None:
            return [], instruction
        field_name = str(field.get("name") or "score")
        name = rule_name if len(schema_fields) == 1 else f"{rule_name} · {field_name}"
        field_id = source_id_value if len(schema_fields) == 1 else f"{source_id_value}:{field_name}"
        prompt_messages = list(messages or [])
        prompt_messages.append({"role": "user", "content": instruction})
        prompt_data = {
            "prompt": {"type": "chat", "messages": prompt_messages},
            "template_format": "mustache",
            "parser": {
                "type": "llm_classifier",
                "use_cot": True,
                "choice_scores": scores,
            },
        }
        if options := _model_options(code.get("model")):
            prompt_data["options"] = options
        definitions.append(
            {
                "name": name,
                "slug": scorer_slug(name, field_id),
                "description": raw.get("description"),
                "function_type": "scorer",
                "function_data": {"type": "prompt"},
                "prompt_data": prompt_data,
                "tags": ["opik"],
            }
        )
    return definitions, None


def online_score_payload(
    evaluator: Any,
    function_ids: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Build a Braintrust online project_score that binds already-migrated scorers."""
    raw = jsonable(as_dict(evaluator))
    if not function_ids:
        return None, "no translated scorers to attach"
    if raw.get("enabled") is False:
        return None, "rule is disabled"
    trigger = str(raw.get("trigger_scope") or "production").lower()
    if trigger == "experiment":
        return None, "online scoring is not attached for experiment-only rules"
    scope_type = evaluator_scope(raw)
    if scope_type is None:
        return None, f"evaluator type {evaluator_type(raw)!r} is not translated"
    sql, sql_error = filters_to_sql(raw.get("filters"))
    if sql_error:
        return None, sql_error
    if scope_type == "span":
        scope: dict[str, Any] = {"type": "span"}
    elif scope_type == "group":
        scope = {
            "type": "group",
            "group_by": "metadata.thread_id",
            "placement": "each",
            "idle_seconds": THREAD_IDLE_SECONDS,
        }
    else:
        scope = {"type": "trace", "idle_seconds": 30}
    return compact(
        {
            "name": str(raw.get("name") or "Opik online eval"),
            "description": raw.get("description"),
            "score_type": "online",
            "config": {
                "online": compact(
                    {
                        "sampling_rate": sampling_rate(raw),
                        "scorers": [
                            {"type": "function", "id": function_id} for function_id in function_ids
                        ],
                        "btql_filter": sql,
                        "scope": scope,
                    }
                )
            },
        }
    ), None


REVIEW_FLAG_LIST = "__bt_default_review_list"


def _feedback_details(raw: dict[str, Any]) -> dict[str, Any]:
    return as_dict(raw.get("details") or raw.get("details_") or {})


def _rescale_category_values(categories: dict[str, float]) -> dict[str, float]:
    values = list(categories.values())
    if not values:
        return categories
    low, high = min(values), max(values)
    if low >= 0 and high <= 1:
        return categories
    if high == low:
        return {name: 1.0 for name in categories}
    return {name: (value - low) / (high - low) for name, value in categories.items()}


def review_score_payload(definition: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Map an Opik feedback definition onto a Braintrust human-review project score."""
    raw = jsonable(as_dict(definition))
    name = str(raw.get("name") or "").strip()
    if not name:
        return None, "feedback definition has no name"
    kind = str(raw.get("type") or "").lower()
    details = jsonable(_feedback_details(raw))
    description_parts = [text for text in [raw.get("description")] if text]
    if kind == "numerical":
        low = _number(details.get("min"))
        high = _number(details.get("max"))
        if low is None or high is None:
            return None, "numerical definition is missing min/max"
        if high < low:
            return None, "numerical definition has max below min"
        if low != 0 or high != 1:
            description_parts.append(
                f"Opik range was {low:g}-{high:g}. Braintrust review sliders are 0-1; "
                "historical values outside [0, 1] were stored as metrics."
            )
        return {
            "name": name,
            "description": " ".join(description_parts) or None,
            "score_type": "slider",
        }, None
    if kind == "categorical":
        raw_categories = details.get("categories") or {}
        if not isinstance(raw_categories, dict) or not raw_categories:
            return None, "categorical definition has no categories"
        numeric = {
            str(label): value
            for label, raw_value in raw_categories.items()
            if (value := _number(raw_value)) is not None
        }
        if not numeric:
            return None, "categorical definition has no numeric values"
        scaled = _rescale_category_values(numeric)
        if scaled != numeric:
            description_parts.append(
                "Opik category values were rescaled into Braintrust's 0-1 range."
            )
        return {
            "name": name,
            "description": " ".join(description_parts) or None,
            "score_type": "categorical",
            "categories": [{"name": label, "value": value} for label, value in scaled.items()],
        }, None
    if kind == "boolean":
        true_label = (
            str(details.get("true_label") or details.get("trueLabel") or "true").strip() or "true"
        )
        false_label = (
            str(details.get("false_label") or details.get("falseLabel") or "false").strip()
            or "false"
        )
        return {
            "name": name,
            "description": " ".join(description_parts) or None,
            "score_type": "categorical",
            "categories": [
                {"name": true_label, "value": 1.0},
                {"name": false_label, "value": 0.0},
            ],
        }, None
    return None, f"feedback definition type {kind or 'missing'!r} is not translated"


def queue_reviewer_notes(queue: Any) -> str:
    raw = jsonable(as_dict(queue))
    notes = []
    if instructions := str(raw.get("instructions") or "").strip():
        notes.append(instructions)
    extras = []
    if raw.get("scope"):
        extras.append(f"Opik scope: {raw['scope']}")
    if raw.get("comments_enabled") is not None:
        extras.append(f"comments_enabled={raw['comments_enabled']}")
    if raw.get("annotators_per_item") is not None:
        extras.append(f"annotators_per_item={raw['annotators_per_item']} (not enforced)")
    if raw.get("lock_timeout_seconds") is not None:
        extras.append(f"lock_timeout_seconds={raw['lock_timeout_seconds']} (not enforced)")
    if extras:
        notes.append("Unmapped Opik queue fields: " + "; ".join(extras) + ".")
    return "\n\n".join(notes)


def review_view_payload(queue: Any) -> dict[str, Any]:
    """Build a Braintrust Review view for an Opik annotation queue."""
    raw = jsonable(as_dict(queue))
    queue_id = str(raw.get("id") or raw.get("name") or "queue")
    options: dict[str, Any] = {"layout": "kanban"}
    if str(raw.get("scope") or "").lower() == "thread":
        options["grouping"] = "metadata.thread_id"
    return {
        "name": str(raw.get("name") or "Opik annotation queue"),
        "object_type": "project",
        "view_type": "for_review_project_log",
        "view_data": {
            "search": {
                "filter": [f"metadata.opik_annotation_queue_id = '{queue_id}'"],
            }
        },
        "options": options,
    }


def review_flag_event(trace_id: str, queue: Any) -> dict[str, Any]:
    """Merge a pending-review flag onto an already migrated Braintrust root span."""
    raw = jsonable(as_dict(queue))
    return {
        "id": source_id("trace", trace_id),
        "metadata": {
            "~__bt_review_lists": {REVIEW_FLAG_LIST: {"status": "PENDING"}},
            "opik_annotation_queue": raw.get("name"),
            "opik_annotation_queue_id": raw.get("id"),
        },
        "_is_merge": True,
        "_merge_paths": [
            ["metadata", "~__bt_review_lists"],
            ["metadata", "opik_annotation_queue"],
            ["metadata", "opik_annotation_queue_id"],
        ],
    }


ROOT_SPAN_FILTER = "is_root"
THREAD_SPAN_FILTER = "metadata.thread_id IS NOT NULL"
LLM_SPAN_FILTER = "span_attributes.type = 'llm'"
ERROR_SPAN_FILTER = "error IS NOT NULL"
_DURATION_PERCENTILES = {"p50": 0.5, "p90": 0.9, "p99": 0.99}
_BREAKDOWN_FIELDS = {
    "tags": "tags",
    "name": "span_attributes.name",
    "error_info": "error",
    "error_type": "error",
    "model": "metadata.opik.model",
    "provider": "metadata.opik.provider",
    "type": "span_attributes.type",
    "guardrail_name": "span_attributes.name",
}
_USAGE_MEASURES = {
    "total_tokens": "metrics.tokens",
    "prompt_tokens": "metrics.prompt_tokens",
    "completion_tokens": "metrics.completion_tokens",
    "usage.total_tokens": "metrics.tokens",
    "usage.prompt_tokens": "metrics.prompt_tokens",
    "usage.completion_tokens": "metrics.completion_tokens",
}


def _cfg(config: dict[str, Any], snake: str, default: Any = None) -> Any:
    parts = snake.split("_")
    camel = parts[0] + "".join(part.title() for part in parts[1:])
    if snake in config and config[snake] is not None:
        return config[snake]
    if camel in config and config[camel] is not None:
        return config[camel]
    return default


def _and_sql(*parts: str | None) -> str | None:
    clauses = [part for part in parts if part]
    return " AND ".join(clauses) or None


def _score_measure(name: str) -> str:
    trimmed = name.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", trimmed):
        return f"avg(scores.{trimmed})"
    escaped = trimmed.replace("`", "``")
    return f"avg(scores.`{escaped}`)"


def _percentile_measures(values: Any) -> list[str]:
    measures = []
    for item in values or []:
        token = str(item).strip().lower().removeprefix("duration.")
        if token in {"avg", "average", "mean"}:
            measures.append("avg(metrics.duration)")
            continue
        percentile = _DURATION_PERCENTILES.get(token)
        if percentile is None:
            continue
        measures.append(f"percentile(metrics.duration, {percentile})")
    return measures or [
        "percentile(metrics.duration, 0.5)",
        "percentile(metrics.duration, 0.9)",
        "percentile(metrics.duration, 0.99)",
    ]


def _usage_measures(values: Any, *, aggregator: str = "sum") -> list[str]:
    measures = []
    for item in values or []:
        field = _USAGE_MEASURES.get(str(item).strip().lower())
        if field:
            measures.append(f"{aggregator}({field})")
    return measures or [f"{aggregator}(metrics.tokens)"]


def _dashboard_config(dashboard: Any) -> dict[str, Any]:
    raw = jsonable(as_dict(dashboard))
    config = raw.get("config")
    if isinstance(config, dict):
        return config
    return {}


def dashboard_widgets(dashboard: Any) -> list[dict[str, Any]]:
    config = _dashboard_config(dashboard)
    widgets = []
    for section in config.get("sections") or []:
        widgets.extend(as_dict(section).get("widgets") or [])
    if not widgets:
        widgets.extend(config.get("widgets") or [])
    return [jsonable(as_dict(widget)) for widget in widgets]


def _widget_title(widget: dict[str, Any]) -> str:
    title = (
        widget.get("title") or widget.get("generatedTitle") or widget.get("generated_title") or ""
    )
    return str(title).strip() or "Untitled widget"


def _widget_chart(
    widget: dict[str, Any],
    *,
    chart_type: str,
    measures: list[str],
    visualization: str | None = None,
    unit: str | None = None,
    span_filter: str | None = None,
    trace_filter: str | None = None,
    group_by: str | None = None,
) -> dict[str, Any]:
    chart_id = str(widget.get("id") or _widget_title(widget))
    return compact(
        {
            "id": chart_id,
            "title": _widget_title(widget),
            "chartType": chart_type,
            "visualization": visualization,
            "unit": unit,
            "measures": measures,
            "spanFilter": span_filter,
            "traceFilter": trace_filter,
            "groupBy": group_by,
        }
    )


def _compile_widget_filters(
    config: dict[str, Any], extra_span: str | None = None
) -> tuple[str | None, str | None, str | None]:
    span_sql, span_error = filters_to_sql(_cfg(config, "span_filters"))
    if span_error:
        return None, None, span_error
    thread_sql, thread_error = filters_to_sql(_cfg(config, "thread_filters"))
    if thread_error:
        return None, None, thread_error
    trace_sql, trace_error = filters_to_sql(_cfg(config, "trace_filters"))
    if trace_error:
        return None, None, trace_error
    return _and_sql(span_sql, thread_sql, extra_span), trace_sql, None


def _breakdown_group(config: dict[str, Any]) -> tuple[str | None, str | None]:
    breakdown = _cfg(config, "breakdown")
    if not isinstance(breakdown, dict):
        return None, None
    field = str(breakdown.get("field") or "").strip().lower()
    if not field or field == "none":
        return None, None
    if field == "metadata":
        key = str(breakdown.get("metadataKey") or breakdown.get("metadata_key") or "").strip()
        if not key:
            return None, "metadata breakdown has no key"
        return f"metadata.{key}", None
    mapped = _BREAKDOWN_FIELDS.get(field)
    if mapped is None:
        return None, f"breakdown field {field!r} is not translated"
    return mapped, None


def _translate_project_metrics(
    widget: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    config = as_dict(widget.get("config") or {})
    metric = str(_cfg(config, "metric_type") or "TRACE_COUNT").strip().upper()
    chart_kind = str(_cfg(config, "chart_type") or "line").strip().lower()
    if chart_kind == "radar":
        return None, "skipped — radar charts have no Monitor equivalent"
    visualization = "bar" if chart_kind == "bar" else "line"
    extra_span = None
    unit = "count"
    measures: list[str] = []
    if metric == "TRACE_COUNT":
        extra_span = ROOT_SPAN_FILTER
        measures = ["count(id)"]
    elif metric == "SPAN_COUNT":
        measures = ["count(id)"]
    elif metric == "THREAD_COUNT":
        extra_span = THREAD_SPAN_FILTER
        measures = ["count_distinct(metadata.thread_id)"]
    elif metric in {"DURATION", "TRACE_DURATION", "SPAN_DURATION", "THREAD_DURATION"}:
        extra_span = {
            "DURATION": ROOT_SPAN_FILTER,
            "TRACE_DURATION": ROOT_SPAN_FILTER,
            "THREAD_DURATION": THREAD_SPAN_FILTER,
        }.get(metric)
        measures = _percentile_measures(_cfg(config, "duration_metrics"))
        unit = "duration"
    elif metric in {
        "TRACE_AVERAGE_DURATION",
        "SPAN_AVERAGE_DURATION",
        "THREAD_AVERAGE_DURATION",
    }:
        extra_span = {
            "TRACE_AVERAGE_DURATION": ROOT_SPAN_FILTER,
            "THREAD_AVERAGE_DURATION": THREAD_SPAN_FILTER,
        }.get(metric)
        measures = ["avg(metrics.duration)"]
        unit = "duration"
    elif metric in {"TOKEN_USAGE", "SPAN_TOKEN_USAGE"}:
        extra_span = ROOT_SPAN_FILTER if metric == "TOKEN_USAGE" else None
        measures = _usage_measures(_cfg(config, "usage_metrics"))
    elif metric == "COST":
        extra_span = ROOT_SPAN_FILTER
        measures = ["sum(metrics.estimated_cost)"]
        unit = "cost"
    elif metric in {"FEEDBACK_SCORES", "THREAD_FEEDBACK_SCORES", "SPAN_FEEDBACK_SCORES"}:
        extra_span = {
            "FEEDBACK_SCORES": ROOT_SPAN_FILTER,
            "THREAD_FEEDBACK_SCORES": THREAD_SPAN_FILTER,
        }.get(metric)
        names = [
            str(name).strip()
            for name in (_cfg(config, "feedback_scores") or [])
            if str(name).strip()
        ]
        if not names:
            return None, "skipped — feedback-score widget has no score names"
        measures = [_score_measure(name) for name in names]
    elif metric in {"TRACE_ERROR_RATE", "SPAN_ERROR_RATE"}:
        extra_span = ROOT_SPAN_FILTER if metric == "TRACE_ERROR_RATE" else None
        measures = ["sum(metrics.errors) / count(id)"]
        unit = "percent"
    elif metric == "GUARDRAILS_FAILED_COUNT":
        return None, "skipped — guardrail-failed count has no dedicated Monitor metric"
    else:
        return None, f"skipped — metric {metric!r} is not translated"
    span_filter, trace_filter, error = _compile_widget_filters(config, extra_span)
    if error:
        return None, f"skipped — {error}"
    group_by, group_error = _breakdown_group(config)
    if group_error:
        return None, f"skipped — {group_error}"
    chart = _widget_chart(
        widget,
        chart_type="timeseries",
        measures=measures,
        visualization=visualization,
        unit=unit,
        span_filter=span_filter,
        trace_filter=trace_filter,
        group_by=group_by,
    )
    return chart, f"timeseries {' '.join(measures)}"


def _translate_stats_card(widget: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    config = as_dict(widget.get("config") or {})
    metric = str(_cfg(config, "metric") or "trace_count").strip()
    source = str(_cfg(config, "source") or "traces").strip().lower()
    extra_span = ROOT_SPAN_FILTER if source != "spans" else None
    unit = "count"
    measures: list[str]
    lowered = metric.lower()
    if lowered in {"trace_count"}:
        extra_span = ROOT_SPAN_FILTER
        measures = ["count(id)"]
    elif lowered == "thread_count":
        extra_span = THREAD_SPAN_FILTER
        measures = ["count_distinct(metadata.thread_id)"]
    elif lowered == "span_count" and source == "spans":
        extra_span = None
        measures = ["count(id)"]
    elif lowered == "llm_span_count":
        extra_span = LLM_SPAN_FILTER
        measures = ["count(id)"]
    elif lowered in {"duration.p50", "duration.p90", "duration.p99"}:
        measures = _percentile_measures([lowered])
        unit = "duration"
    elif lowered == "total_estimated_cost_sum":
        measures = ["sum(metrics.estimated_cost)"]
        unit = "cost"
    elif lowered == "total_estimated_cost":
        measures = ["avg(metrics.estimated_cost)"]
        unit = "cost"
    elif lowered in _USAGE_MEASURES:
        measures = [f"avg({_USAGE_MEASURES[lowered]})"]
    elif lowered == "error_count":
        extra_span = _and_sql(extra_span, ERROR_SPAN_FILTER)
        measures = ["count(id)"]
    elif lowered.startswith("feedback_scores."):
        name = metric.split(".", 1)[1].strip()
        if not name:
            return None, "skipped — feedback-score card has no score name"
        measures = [_score_measure(name)]
    elif lowered == "guardrails_failed_count":
        return None, "skipped — guardrail-failed count has no dedicated Monitor metric"
    elif lowered in {"input", "output", "metadata", "tags", "span_count"}:
        return None, f"skipped — stat card metric {metric!r} is not a Monitor chart"
    else:
        return None, f"skipped — stat card metric {metric!r} is not translated"
    span_filter, trace_filter, error = _compile_widget_filters(config, extra_span)
    if error:
        return None, f"skipped — {error}"
    chart = _widget_chart(
        widget,
        chart_type="bignumber",
        measures=measures,
        unit=unit,
        span_filter=span_filter,
        trace_filter=trace_filter,
    )
    return chart, f"bignumber {' '.join(measures)}"


def _translate_widget(widget: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    kind = str(widget.get("type") or "").strip().lower()
    title = _widget_title(widget)
    if kind == "project_metrics":
        chart, note = _translate_project_metrics(widget)
    elif kind == "project_stats_card":
        chart, note = _translate_stats_card(widget)
    elif kind == "text_markdown":
        chart, note = None, "skipped — markdown is not a Monitor chart"
    elif kind in {"experiments_feedback_scores", "experiment_leaderboard"}:
        chart, note = None, f"skipped — {kind.replace('_', ' ')} has no Monitor equivalent"
    elif not kind:
        chart, note = None, "skipped — widget has no type"
    else:
        chart, note = None, f"skipped — widget type {kind!r} is not translated"
    return chart, f"{title}: {note}"


def dashboard_view_payload(
    dashboard: Any,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """Build a Braintrust Monitor view for an Opik production dashboard."""
    raw = jsonable(as_dict(dashboard))
    notes = []
    charts = []
    for widget in dashboard_widgets(dashboard):
        chart, note = _translate_widget(widget)
        notes.append(note)
        if chart is not None:
            charts.append(chart)
    kind = str(raw.get("type") or "multi_project").strip().lower()
    scope = str(raw.get("scope") or "workspace").strip().lower()
    if scope == "insights":
        return None, "built-in Insights dashboards are not migrated", notes
    if kind == "experiments":
        return (
            None,
            "experiment dashboards are inventoried, not written as Monitor views",
            notes,
        )
    if kind and kind != "multi_project":
        return None, f"dashboard type {kind!r} is not translated", notes
    if not charts:
        return None, "no translatable production widgets", notes
    return (
        {
            "name": str(raw.get("name") or "Opik dashboard"),
            "object_type": "project",
            "view_type": "monitor",
            "view_data": {"custom_charts": charts},
            "options": {
                "viewType": "monitor",
                "options": {"type": "project", "spanType": "range", "rangeValue": "7d"},
            },
        },
        None,
        notes,
    )
