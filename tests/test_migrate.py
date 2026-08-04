import json
from datetime import UTC, datetime

from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.config import Resource
from opik_to_bt.migrate import Migrator, Selection
from opik_to_bt.pipeline import Page
from opik_to_bt.tuning import RuntimeTuning


def decode_events(path) -> list[dict]:
    return [json.loads(line) for line in path.read_bytes().splitlines()]


class FakeSource:
    async def projects(self):
        return [{"id": "p1", "name": "selected"}, {"id": "p2", "name": "ignored"}]

    async def datasets(self, project_id):
        return [{"id": "d1", "name": "dataset", "tags": ["curated"]}]

    async def dataset_items(self, dataset_id):
        return [{"id": "row", "tags": ["golden"], "data": {"input": {"hello": "world"}}}]

    async def experiments(self, project_id):
        return [
            {
                "id": "e1",
                "name": "recent",
                "created_at": "2026-02-01T00:00:00Z",
                "dataset_id": "d1",
                "tags": ["baseline"],
            },
            {"id": "e2", "name": "old", "created_at": "2025-01-01T00:00:00Z"},
        ]

    async def experiment_items(self, experiment_id):
        return [
            {
                "id": "result",
                "dataset_item_id": "row",
                "assertion_results": [
                    {
                        "value": "Useful",
                        "passed": True,
                        "reason": "The answer is useful.",
                    }
                ],
                "status": "passed",
            }
        ]

    async def traces(self, project_name, *, start, end):
        return []


class FakeTarget:
    def __init__(self):
        self.datasets = []
        self.experiments = []
        self.tagged = []

    async def check(self):
        return None

    async def apply_tags(self, handle, tags):
        self.tagged.append((handle, tags))

    async def create_project(self, name, description):
        return "bt-project"

    async def create_dataset(self, project_id, name, description):
        return "bt-dataset"

    async def create_experiment(self, project_id, name, description):
        return "bt-experiment"

    async def insert_dataset(self, dataset_id, events, *, partition_key=None):
        del partition_key
        self.datasets.extend(decode_events(events))

    async def insert_experiment(self, experiment_id, events, *, partition_key=None):
        del partition_key
        self.experiments.extend(decode_events(events))


async def test_migrator_filters_and_checkpoints(tmp_path) -> None:
    source, target = FakeSource(), FakeTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    selection = Selection(
        resources={Resource.DATASETS, Resource.EXPERIMENTS},
        projects={"selected"},
        datasets=None,
        experiments=None,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=None,
    )
    migrator = Migrator(source, target, checkpoint)
    await migrator.run(selection)
    await migrator.run(selection)

    assert len(target.datasets) == 1
    assert target.datasets[0]["tags"] == ["golden"]
    assert len(target.experiments) == 3
    assert target.experiments[0]["id"] == "opik:experiment-item:result"
    # Object-level tags apply on the fresh run and again on the checkpointed rerun.
    assert target.tagged == [
        ("bt-dataset", ["curated"]),
        ("bt-experiment", ["baseline"]),
        ("bt-dataset", ["curated"]),
        ("bt-experiment", ["baseline"]),
    ]
    assert target.experiments[1]["_object_delete"] is True
    assert target.experiments[2]["scores"] == {"Test suite passed": 1.0}
    assert target.experiments[2]["metadata"]["assertions"] == [
        {
            "text": "Useful",
            "passed": True,
            "reason": "The answer is useful.",
        }
    ]


async def test_logs_use_independent_bulk_trace_and_span_pagination(
    tmp_path,
) -> None:
    class LogSource:
        def __init__(self):
            self.calls = []

        async def trace_pages(self, project_name, *, start, end, start_page):
            self.calls.append(("traces", project_name, start, end, start_page))
            yield Page(
                1,
                [
                    {
                        "id": "inside",
                        "start_time": "2026-01-31T23:59:59Z",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                        "total_estimated_cost": 0.02,
                        "tags": ["production"],
                    },
                    {"id": "at-end", "start_time": "2026-02-01T00:00:00Z"},
                ],
                2,
            )

        async def span_pages_for_traces(
            self,
            project_name,
            *,
            trace_ids,
            start,
            end,
        ):
            self.calls.append(("spans", project_name, trace_ids, start, end))
            yield Page(
                1,
                [
                    {
                        "id": "span-inside",
                        "trace_id": "inside",
                        "start_time": "2026-01-31T23:59:59Z",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                        "total_estimated_cost": 0.02,
                        "tags": ["retrieval"],
                    },
                    {
                        "id": "span-at-end",
                        "trace_id": "at-end",
                        "start_time": "2026-02-01T00:00:00Z",
                    },
                ],
                2,
            )

    class LogTarget:
        def __init__(self):
            self.events = []

        async def insert_logs(self, project_id, events, *, partition_key=None):
            del project_id, partition_key
            self.events.extend(decode_events(events))

    source, target = LogSource(), LogTarget()
    migrator = Migrator(
        source,
        target,
        Checkpoint(tmp_path / "checkpoint.json"),
        RuntimeTuning.conservative(),
    )
    selection = Selection(
        resources={Resource.LOGS},
        projects=None,
        datasets=None,
        experiments=None,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 2, 1, tzinfo=UTC),
    )

    await migrator._logs("project", "source-project", "target-project", selection)

    assert {event["id"] for event in target.events} == {
        "opik:trace:inside",
        "opik:span:span-inside",
    }
    root = next(event for event in target.events if event["is_root"])
    span = next(event for event in target.events if not event["is_root"])
    assert root["tags"] == ["production"]
    assert span["tags"] == ["retrieval"]
    assert "prompt_tokens" not in root["metrics"]
    assert "estimated_cost" not in root["metrics"]
    assert root["metadata"]["opik"]["aggregate_usage"]["prompt_tokens"] == 10
    assert span["metrics"]["prompt_tokens"] == 10
    assert span["metrics"]["completion_tokens"] == 2
    assert span["metrics"]["tokens"] == 12
    assert span["metrics"]["estimated_cost"] == 0.02
    assert source.calls == [
        ("traces", "project", selection.start, selection.end, 1),
        ("spans", "project", {"inside"}, selection.start, selection.end),
    ]
    assert migrator.checkpoint.completed(f"logs:source-project:{selection.start}:{selection.end}")


async def test_implicit_end_reuses_checkpoint_snapshot(tmp_path) -> None:
    class EmptySource:
        async def projects(self):
            return []

    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    checkpoint.set_value("implicit_end", "2026-03-01T12:00:00Z")
    selection = Selection(
        resources={Resource.LOGS},
        projects=None,
        datasets=None,
        experiments=None,
        start=None,
        end=None,
        dry_run=True,
    )

    await Migrator(
        EmptySource(),
        object(),
        checkpoint,
        RuntimeTuning.conservative(),
    ).run(selection)

    assert selection.end == datetime(2026, 3, 1, 12, tzinfo=UTC)
