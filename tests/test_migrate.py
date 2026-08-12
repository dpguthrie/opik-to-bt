import json
from datetime import UTC, datetime

import pytest

from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.config import PromptHistory, Resource
from opik_to_bt.mapping import prompt_definition
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


class PromptSource:
    def __init__(self) -> None:
        self.container = {
            "id": "prompt-1",
            "name": "Greeting",
            "description": "A greeting",
            "template_structure": "text",
            "tags": ["shared"],
        }
        self.history = [
            {"id": "version-1", "template": "Hello", "type": "mustache"},
            {"id": "version-2", "template": "Hello {{name}}", "type": "mustache"},
        ]

    async def projects(self):
        return [{"id": "project-1", "name": "selected"}]

    async def prompts(self, project_id):
        del project_id
        return [self.container]

    async def prompt_detail(self, prompt_id):
        del prompt_id
        return {"latest_version": self.history[-1]}

    async def prompt_versions(self, prompt_id):
        del prompt_id
        return self.history


class PromptTarget:
    def __init__(self) -> None:
        self.current = None
        self.writes = []
        self.next_version = 1

    async def check(self):
        return None

    async def create_project(self, name, description):
        del name, description
        return "bt-project"

    async def get_prompt(self, project_id, slug):
        del project_id, slug
        return self.current

    async def write_prompt(self, project_id, definition, *, update):
        del project_id
        version = self.next_version
        self.next_version += 1
        self.current = {
            "id": "bt-prompt",
            "_xact_id": f"xact-{version}",
            **definition,
        }
        self.writes.append((update, definition))
        return self.current


def prompt_selection(history: PromptHistory) -> Selection:
    return Selection(
        resources={Resource.PROMPTS},
        projects=None,
        datasets=None,
        experiments=None,
        start=None,
        end=None,
        prompt_history=history,
    )


async def test_latest_prompt_mode_writes_only_latest_and_resumes(tmp_path) -> None:
    source, target = PromptSource(), PromptTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    migrator = Migrator(source, target, checkpoint)

    await migrator.run(prompt_selection(PromptHistory.LATEST))
    await migrator.run(prompt_selection(PromptHistory.LATEST))

    assert len(target.writes) == 1
    update, definition = target.writes[0]
    assert update is False
    assert definition["prompt_data"]["prompt"]["content"] == "Hello {{name}}"
    assert checkpoint.value("prompt:prompt-1:versions") == {"version-2": "xact-1"}
    assert checkpoint.completed("prompt:prompt-1:latest")


async def test_all_prompt_history_replays_oldest_to_newest(tmp_path) -> None:
    source, target = PromptSource(), PromptTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")

    await Migrator(source, target, checkpoint).run(prompt_selection(PromptHistory.ALL))

    assert [update for update, _ in target.writes] == [False, True]
    assert [definition["prompt_data"]["prompt"]["content"] for _, definition in target.writes] == [
        "Hello",
        "Hello {{name}}",
    ]
    assert checkpoint.value("prompt:prompt-1:versions") == {
        "version-1": "xact-1",
        "version-2": "xact-2",
    }
    assert checkpoint.completed("prompt:prompt-1:all")


async def test_all_prompt_history_keeps_identical_versions_distinct(tmp_path) -> None:
    source, target = PromptSource(), PromptTarget()
    source.history[1]["template"] = source.history[0]["template"]

    await Migrator(source, target, Checkpoint(tmp_path / "checkpoint.json")).run(
        prompt_selection(PromptHistory.ALL)
    )

    assert [update for update, _ in target.writes] == [False, True]


async def test_latest_to_all_mode_change_is_rejected(tmp_path) -> None:
    source, target = PromptSource(), PromptTarget()
    migrator = Migrator(source, target, Checkpoint(tmp_path / "checkpoint.json"))
    await migrator.run(prompt_selection(PromptHistory.LATEST))

    with pytest.raises(RuntimeError, match="already migrated in latest-only mode"):
        await migrator.run(prompt_selection(PromptHistory.ALL))


async def test_prompt_resume_rejects_destination_change_after_final_write(tmp_path) -> None:
    source, target = PromptSource(), PromptTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    await Migrator(source, target, checkpoint).run(prompt_selection(PromptHistory.ALL))

    checkpoint.data["completed"].remove("prompt:prompt-1:all")
    checkpoint.save()
    target.current["_xact_id"] = "manual-edit"

    with pytest.raises(RuntimeError, match="changed after its last checkpoint"):
        await Migrator(source, target, checkpoint).run(prompt_selection(PromptHistory.ALL))


async def test_prompt_resume_recovers_write_before_checkpoint(tmp_path) -> None:
    source, target = PromptSource(), PromptTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    slug = "greeting-2c7e2d3a"
    first_definition = {
        **prompt_definition(source.container, source.history[0]),
        "slug": slug,
    }
    target.current = {"id": "bt-prompt", "_xact_id": "xact-1", **first_definition}
    target.next_version = 2
    checkpoint.set_value("prompt:prompt-1:mode", "all")
    checkpoint.set_value("prompt:prompt-1:slug", slug)

    await Migrator(source, target, checkpoint).run(prompt_selection(PromptHistory.ALL))

    assert [update for update, _ in target.writes] == [True]
    assert checkpoint.value("prompt:prompt-1:versions") == {
        "version-1": "xact-1",
        "version-2": "xact-2",
    }
