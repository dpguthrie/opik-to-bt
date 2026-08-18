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
