import pytest

from opik_to_bt.mapping import (
    dataset_event,
    experiment_events,
    span_event,
    trace_event,
    trace_events,
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
