from opik_to_bt.mapping import dataset_event, experiment_event, trace_events


def test_dataset_and_experiment_mapping() -> None:
    dataset = dataset_event(
        {"id": "item-1", "data": {"input": {"x": 1}, "expected_output": {"y": 2}}}
    )
    assert dataset["id"] == "opik:dataset-item:item-1"
    assert dataset["expected"] == {"y": 2}

    experiment = experiment_event(
        {
            "id": "result-1",
            "dataset_item_id": "item-1",
            "dataset_item_data": {"input": {"x": 1}, "expected": {"y": 2}},
            "evaluation_task_output": {"y": 2},
            "feedback_scores": [
                {"name": "quality", "value": 0.9},
                {"name": "latency_ms", "value": 123},
            ],
        }
    )
    assert experiment["scores"] == {"quality": 0.9}
    assert experiment["metadata"]["opik"]["scores_outside_zero_to_one"]["latency_ms"] == 123


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
    assert events[1]["span_parents"] == ["opik:trace:trace-1"]
    assert events[2]["span_parents"] == ["opik:span:span-1"]
    assert events[2]["span_attributes"]["type"] == "tool"
