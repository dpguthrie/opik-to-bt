import pytest

from opik_to_bt.mapping import (
    dataset_event,
    experiment_events,
    filters_to_sql,
    online_score_payload,
    prompt_definition,
    prompt_slug,
    queue_reviewer_notes,
    review_flag_event,
    review_score_payload,
    review_view_payload,
    rewrite_judge_template,
    scorer_definitions,
    span_event,
    trace_event,
    trace_events,
)


def test_text_prompt_maps_to_braintrust_completion() -> None:
    definition = prompt_definition(
        {
            "id": "prompt-1",
            "name": "Support Answer",
            "description": "Answer support questions",
            "template_structure": "text",
            "tags": ["support"],
        },
        {
            "id": "version-2",
            "template": "Answer {{question}}",
            "type": "mustache",
        },
    )

    assert definition == {
        "name": "Support Answer",
        "description": "Answer support questions",
        "prompt_data": {
            "prompt": {"type": "completion", "content": "Answer {{question}}"},
            "template_format": "mustache",
        },
        "tags": ["support"],
    }
    assert prompt_slug("Support Answer", "prompt-1").startswith("support-answer-")


def test_chat_prompt_maps_json_messages_and_jinja_to_nunjucks() -> None:
    definition = prompt_definition(
        {"name": "Chat", "template_structure": "chat", "tags": ["container"]},
        {
            "template": '[{"role":"system","content":"Hello {{ name }}"}]',
            "type": "jinja2",
            "tags": ["version"],
        },
    )

    assert definition["prompt_data"] == {
        "prompt": {
            "type": "chat",
            "messages": [{"role": "system", "content": "Hello {{ name }}"}],
        },
        "template_format": "nunjucks",
    }
    assert definition["tags"] == ["version"]


def test_invalid_chat_prompt_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid message JSON"):
        prompt_definition(
            {"name": "Broken", "template_structure": "chat"},
            {"template": "not-json", "type": "mustache"},
        )


def test_dataset_and_experiment_mapping() -> None:
    dataset = dataset_event(
        {"id": "item-1", "data": {"input": {"x": 1}, "expected_output": {"y": 2}}}
    )
    assert dataset["id"] == "opik:dataset-item:item-1"
    assert dataset["expected"] == {"y": 2}

    experiment, quality_score = experiment_events(
        {
            "id": "result-1",
            "dataset_item_id": "item-1",
            "dataset_item_data": {"input": {"x": 1}, "expected": {"y": 2}},
            "evaluation_task_output": {"y": 2},
            "feedback_scores": [
                {"name": "quality", "value": 0.9},
                {"name": "rating", "value": 4},
            ],
            "duration": 123,
            "total_estimated_cost": 0.004,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
            },
        }
    )
    assert "scores" not in experiment
    assert experiment["span_attributes"] == {"name": "eval", "type": "eval"}
    assert quality_score["scores"] == {"quality": 0.9}
    assert quality_score["output"] == {"score": 0.9}
    assert experiment["metrics"] == {
        "rating": 4.0,
        "input_tokens": 10.0,
        "output_tokens": 4.0,
        "total_tokens": 14.0,
        "prompt_tokens": 10.0,
        "completion_tokens": 4.0,
        "tokens": 14.0,
        "duration": 0.123,
        "estimated_cost": 0.004,
    }
    assert experiment["metadata"]["opik"]["feedback_scores"][1]["name"] == "rating"


def test_trace_mapping_preserves_parentage() -> None:
    trace = {
        "id": "trace-1",
        "name": "answer",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:00:02Z",
    }
    spans = [
        {
            "id": "span-1",
            "trace_id": "trace-1",
            "type": "llm",
            "start_time": "2026-01-01T00:00:00Z",
        },
        {
            "id": "span-2",
            "trace_id": "trace-1",
            "parent_span_id": "span-1",
            "type": "tool",
            "start_time": "2026-01-01T00:00:01Z",
        },
    ]
    events = trace_events(trace, spans)
    assert events[0]["is_root"] is True
    assert events[0]["span_attributes"] == {"name": "answer", "type": "task"}
    assert events[1]["span_parents"] == ["opik:trace:trace-1"]
    assert events[1]["span_attributes"]["name"] == "Opik span"
    assert events[2]["span_parents"] == ["opik:span:span-1"]
    assert events[2]["span_attributes"]["type"] == "tool"


def test_trace_timing_uses_child_envelope_when_root_timestamp_is_stale() -> None:
    events = trace_events(
        {
            "id": "trace-1",
            "start_time": "2026-07-11T00:00:00Z",
            "duration": 1900,
        },
        [
            {
                "id": "span-1",
                "trace_id": "trace-1",
                "start_time": "2026-07-29T00:00:00Z",
                "duration": 650,
            },
            {
                "id": "span-2",
                "trace_id": "trace-1",
                "start_time": "2026-07-29T00:00:00.650Z",
                "duration": 1250,
            },
        ],
    )

    root = events[0]
    assert root["metrics"]["start"] == events[1]["metrics"]["start"]
    assert root["metrics"]["end"] == pytest.approx(events[2]["metrics"]["end"])
    assert root["metrics"]["end"] - root["metrics"]["start"] == pytest.approx(1.9)


def test_test_suite_input_output_and_assertions() -> None:
    events = experiment_events(
        {
            "id": "result-1",
            "dataset_item_id": "item-1",
            "dataset_item_data": {"question": "What is a span?"},
            "output": {"answer": "A unit of work in a trace."},
            "assertion_results": [
                {
                    "value": "Defines a span",
                    "passed": True,
                    "reason": "The response gives a definition.",
                },
                {
                    "value": "Mentions nesting",
                    "passed": False,
                    "reason": "Nesting is not mentioned.",
                },
            ],
            "status": "failed",
            "execution_policy": {"runs_per_item": 1, "pass_threshold": 1},
        }
    )
    event = events[0]
    deleted_assertions = [item for item in events if item.get("_object_delete")]
    overall = next(item for item in events if item.get("scores"))

    assert event["input"] == {"question": "What is a span?"}
    assert event["output"] == {"answer": "A unit of work in a trace."}
    assert "scores" not in event
    assert len(deleted_assertions) == 2
    assert overall["scores"] == {"Test suite passed": 0.0}
    assert overall["output"] == {"score": 0.0}
    assert overall["metadata"] == {
        "assertions": [
            {
                "text": "Defines a span",
                "passed": True,
                "reason": "The response gives a definition.",
            },
            {
                "text": "Mentions nesting",
                "passed": False,
                "reason": "Nesting is not mentioned.",
            },
        ],
        "assertions_passed": 1,
        "assertions_total": 2,
        "opik": {
            "status": "failed",
            "execution_policy": {"runs_per_item": 1, "pass_threshold": 1},
        },
    }


def test_every_opik_span_type_maps_to_a_braintrust_span_type() -> None:
    # Opik's SpanType enum is exhaustively general/tool/llm/guardrail.
    mapped = {
        opik_type: span_event("trace-1", {"id": "span-1", "type": opik_type})["span_attributes"][
            "type"
        ]
        for opik_type in ("general", "tool", "llm", "guardrail", "", "something-new")
    }
    assert mapped == {
        "general": "task",
        "tool": "tool",
        "llm": "llm",
        "guardrail": "function",
        "": "task",
        "something-new": "task",
    }


def test_tags_map_to_native_braintrust_tags() -> None:
    events = trace_events(
        {
            "id": "trace-1",
            "start_time": "2026-01-01T00:00:00Z",
            "tags": ["production", "  regression  ", ""],
        },
        [
            {
                "id": "span-1",
                "trace_id": "trace-1",
                "start_time": "2026-01-01T00:00:00Z",
                "tags": ["regression", "retrieval"],
            }
        ],
    )
    root, span = events
    assert root["tags"] == ["production", "regression"]
    assert "tags" not in root["metadata"]["opik"]
    assert span["tags"] == ["regression", "retrieval"]
    assert "tags" not in span["metadata"]["opik"]

    untagged = trace_event({"id": "trace-2", "start_time": "2026-01-01T00:00:00Z"})
    assert "tags" not in untagged
    assert "tags" not in span_event("trace-2", {"id": "span-2", "tags": []})

    dataset = dataset_event({"id": "item-1", "tags": ["golden"], "data": {"input": {"x": 1}}})
    assert dataset["tags"] == ["golden"]
    assert "tags" not in dataset_event({"id": "item-2", "data": {"input": {"x": 1}}})


def test_trace_and_span_feedback_and_standard_metrics() -> None:
    trace = trace_event(
        {
            "id": "trace-1",
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:00:02Z",
            "duration": 750,
            "feedback_scores": [
                {"name": "quality", "value": 0.8},
                {"name": "rating", "value": 5},
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            "total_estimated_cost": 0.01,
        },
        include_aggregate_metrics=False,
    )
    assert trace["scores"] == {"quality": 0.8}
    assert trace["metrics"]["rating"] == 5.0
    assert trace["metrics"]["end"] - trace["metrics"]["start"] == 0.75
    assert "prompt_tokens" not in trace["metrics"]
    assert "estimated_cost" not in trace["metrics"]
    assert trace["metadata"]["opik"]["aggregate_estimated_cost"] == 0.01

    span = span_event(
        "trace-1",
        {
            "id": "span-1",
            "start_time": "2026-01-01T00:00:00Z",
            "feedback_scores": [{"name": "distance", "value": 2}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            "total_estimated_cost": 0.01,
            "ttft": 250,
            "duration": 425,
        },
    )
    assert span["metrics"]["distance"] == 2.0
    assert span["metrics"]["tokens"] == 15.0
    assert span["metrics"]["estimated_cost"] == 0.01
    assert span["metrics"]["time_to_first_token"] == 0.25
    assert span["metrics"]["end"] - span["metrics"]["start"] == pytest.approx(0.425)


def test_trace_copies_thread_id_for_grouped_online_scoring() -> None:
    trace = trace_event(
        {
            "id": "trace-1",
            "thread_id": "thread-9",
            "start_time": "2026-01-01T00:00:00Z",
        }
    )
    assert trace["metadata"]["thread_id"] == "thread-9"
    assert trace["metadata"]["opik"]["thread_id"] == "thread-9"


def _judge_evaluator(**overrides):
    evaluator = {
        "id": "rule-1",
        "name": "Hallucination",
        "type": "llm_as_judge",
        "enabled": True,
        "sampling_rate": 0.5,
        "code": {
            "model": {"name": "gpt-4o", "temperature": 0},
            "variables": {"answer": "output", "question": "input"},
            "messages": [
                {"role": "SYSTEM", "content": "Judge the answer."},
                {"role": "USER", "content": "Q: {{question}}\nA: {{answer}}"},
            ],
            "schema": [
                {
                    "name": "hallucination",
                    "type": "BOOLEAN",
                    "description": "Whether the answer is grounded",
                }
            ],
        },
    }
    evaluator.update(overrides)
    return evaluator


def test_llm_judge_scorer_rewrites_variables_and_boolean_choices() -> None:
    definitions, skip = scorer_definitions(_judge_evaluator())
    assert skip is None
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["function_type"] == "scorer"
    assert definition["function_data"] == {"type": "prompt"}
    messages = definition["prompt_data"]["prompt"]["messages"]
    assert messages[0] == {"role": "system", "content": "Judge the answer."}
    assert messages[1]["content"] == "Q: {{input}}\nA: {{output}}"
    assert "true or false" in messages[2]["content"]
    assert definition["prompt_data"]["parser"]["choice_scores"] == {"true": 1.0, "false": 0.0}
    assert definition["prompt_data"]["options"]["model"] == "gpt-4o"


def test_multi_score_schema_becomes_one_scorer_per_field() -> None:
    evaluator = _judge_evaluator()
    evaluator["code"]["schema"] = [
        {"name": "relevance", "type": "BOOLEAN", "description": ""},
        {"name": "quality", "type": "DOUBLE", "description": "0 to 1"},
    ]
    definitions, skip = scorer_definitions(evaluator)
    assert skip is None
    assert [item["name"] for item in definitions] == [
        "Hallucination · relevance",
        "Hallucination · quality",
    ]
    assert definitions[1]["prompt_data"]["parser"]["choice_scores"]["0.5"] == 0.5


def test_sdk_schema_alias_is_read_from_pydantic_field_name() -> None:
    evaluator = _judge_evaluator()
    code = evaluator["code"]
    code["schema_"] = code.pop("schema")
    definitions, skip = scorer_definitions(evaluator)
    assert skip is None
    assert definitions[0]["prompt_data"]["parser"]["choice_scores"] == {
        "true": 1.0,
        "false": 0.0,
    }


def test_python_metric_is_skipped() -> None:
    definitions, skip = scorer_definitions(
        {"id": "py-1", "name": "Length", "type": "user_defined_metric_python"}
    )
    assert definitions == []
    assert "Python" in skip


def test_rewrite_judge_template_maps_thread_context() -> None:
    assert rewrite_judge_template("See {{context}}", {"context": "context"}) == "See {{thread}}"


def test_filters_to_sql_and_untranslated_fields() -> None:
    sql, error = filters_to_sql(
        [
            {"field": "name", "operator": "=", "value": "chat"},
            {"field": "tags", "operator": "contains", "value": "prod"},
            {"field": "metadata", "operator": "!=", "key": "env", "value": "staging"},
        ]
    )
    assert error is None
    assert sql == (
        "span_attributes.name = 'chat' AND tags IN ('prod') AND metadata.env IS NOT 'staging'"
    )
    _, error = filters_to_sql([{"field": "unknown", "operator": "=", "value": "x"}])
    assert error == "filter field 'unknown' is not translated"


def test_online_score_payload_trace_span_and_thread_scopes() -> None:
    payload, skip = online_score_payload(_judge_evaluator(), ["fn-1"])
    assert skip is None
    online = payload["config"]["online"]
    assert online["sampling_rate"] == 0.5
    assert online["scorers"] == [{"type": "function", "id": "fn-1"}]
    assert online["scope"] == {"type": "trace", "idle_seconds": 30}

    span_rule = _judge_evaluator(type="span_llm_as_judge")
    payload, skip = online_score_payload(span_rule, ["fn-1"])
    assert payload["config"]["online"]["scope"] == {"type": "span"}

    thread_rule = _judge_evaluator(type="trace_thread_llm_as_judge")
    payload, skip = online_score_payload(thread_rule, ["fn-1"])
    assert payload["config"]["online"]["scope"]["type"] == "group"
    assert payload["config"]["online"]["scope"]["group_by"] == "metadata.thread_id"


def test_online_score_skips_disabled_python_and_experiment_rules() -> None:
    _, skip = online_score_payload(_judge_evaluator(enabled=False), ["fn-1"])
    assert skip == "rule is disabled"
    _, skip = online_score_payload(_judge_evaluator(trigger_scope="experiment"), ["fn-1"])
    assert "experiment-only" in skip
    _, skip = online_score_payload(_judge_evaluator(), [])
    assert skip == "no translated scorers to attach"


def test_numerical_and_boolean_feedback_definitions_become_review_scores() -> None:
    slider, skip = review_score_payload(
        {
            "id": "def-1",
            "name": "Quality",
            "type": "numerical",
            "description": "SME quality",
            "details": {"min": 0, "max": 1},
        }
    )
    assert skip is None
    assert slider["score_type"] == "slider"
    assert slider["description"] == "SME quality"

    boolean, skip = review_score_payload(
        {
            "id": "def-2",
            "name": "Grounded",
            "type": "boolean",
            "details": {"trueLabel": "yes", "falseLabel": "no"},
        }
    )
    assert skip is None
    assert boolean["score_type"] == "categorical"
    assert boolean["categories"] == [
        {"name": "yes", "value": 1.0},
        {"name": "no", "value": 0.0},
    ]


def test_categorical_values_outside_unit_interval_are_rescaled() -> None:
    payload, skip = review_score_payload(
        {
            "id": "def-3",
            "name": "Severity",
            "type": "categorical",
            "details": {"categories": {"low": 1, "medium": 2, "high": 3}},
        }
    )
    assert skip is None
    values = {item["name"]: item["value"] for item in payload["categories"]}
    assert values == {"low": 0.0, "medium": 0.5, "high": 1.0}
    assert "rescaled" in payload["description"]


def test_review_view_and_flag_events_use_queue_identity() -> None:
    queue = {
        "id": "queue-1",
        "name": "Hallucination backlog",
        "scope": "thread",
        "instructions": "Mark grounded answers.",
        "annotators_per_item": 2,
        "feedback_definition_names": ["Grounded"],
    }
    view = review_view_payload(queue)
    assert view["view_type"] == "for_review_project_log"
    assert view["options"]["grouping"] == "metadata.thread_id"
    assert "queue-1" in view["view_data"]["search"]["filter"][0]
    assert "annotators_per_item=2" in queue_reviewer_notes(queue)

    event = review_flag_event("trace-9", queue)
    assert event["id"] == "opik:trace:trace-9"
    assert event["_is_merge"] is True
    assert event["metadata"]["~__bt_review_lists"]["__bt_default_review_list"]["status"] == (
        "PENDING"
    )
