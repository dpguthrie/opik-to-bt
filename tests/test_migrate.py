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


class EvaluatorSource:
    def __init__(self, evaluators) -> None:
        self._evaluators = evaluators

    async def projects(self):
        return [{"id": "project-1", "name": "selected"}]

    async def evaluators(self, project_id):
        del project_id
        return self._evaluators


class ScorerTarget:
    def __init__(self) -> None:
        self.functions = {}
        self.function_writes = []
        self.scores = {}
        self.score_writes = []
        self.next_function = 1
        self.next_score = 1

    async def check(self):
        return None

    async def create_project(self, name, description):
        del name, description
        return "bt-project"

    async def get_function(self, project_id, slug):
        del project_id
        return self.functions.get(slug)

    async def write_function(self, project_id, definition, *, update):
        del project_id
        written = {
            "id": f"fn-{self.next_function}",
            **definition,
        }
        self.next_function += 1
        self.functions[definition["slug"]] = written
        self.function_writes.append((update, definition))
        return written

    async def get_project_score(self, project_id, name):
        del project_id
        return self.scores.get(name)

    async def write_project_score(self, project_id, definition, *, update):
        del project_id
        written = {"id": f"score-{self.next_score}", **definition}
        self.next_score += 1
        self.scores[definition["name"]] = written
        self.score_writes.append((update, definition))
        return written

    async def get_view(self, project_id, name, *, view_type):
        del project_id, view_type
        return self.views.get(name) if hasattr(self, "views") else None

    async def write_view(self, project_id, definition, *, update):
        del project_id
        if not hasattr(self, "views"):
            self.views = {}
            self.view_writes = []
            self.next_view = 1
        written = {"id": f"view-{self.next_view}", **definition}
        self.next_view += 1
        self.views[definition["name"]] = written
        self.view_writes.append((update, definition))
        return written

    async def flag_logs_for_review(self, project_id, events):
        del project_id
        if not hasattr(self, "flagged"):
            self.flagged = []
        self.flagged.extend(events)


def _judge(name="Hallucination", **overrides):
    rule = {
        "id": "rule-1",
        "name": name,
        "type": "llm_as_judge",
        "enabled": True,
        "sampling_rate": 1,
        "code": {
            "model": {"name": "gpt-4o", "temperature": 0},
            "variables": {"output": "output"},
            "messages": [{"role": "USER", "content": "Score {{output}}"}],
            "schema": [{"name": "score", "type": "BOOLEAN", "description": "ok"}],
        },
    }
    rule.update(overrides)
    return rule


def scorer_selection(*resources: Resource) -> Selection:
    return Selection(
        resources=set(resources),
        projects=None,
        datasets=None,
        experiments=None,
        start=None,
        end=None,
    )


async def test_scorers_write_functions_and_resume(tmp_path) -> None:
    source = EvaluatorSource([_judge()])
    target = ScorerTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    migrator = Migrator(source, target, checkpoint)

    await migrator.run(scorer_selection(Resource.SCORERS))
    await migrator.run(scorer_selection(Resource.SCORERS))

    assert len(target.function_writes) == 1
    assert target.score_writes == []
    update, definition = target.function_writes[0]
    assert update is False
    assert definition["prompt_data"]["prompt"]["messages"][0]["content"] == "Score {{output}}"
    assert checkpoint.completed("scorer:rule-1")


async def test_online_evals_write_scorers_then_bind_the_rule(tmp_path) -> None:
    source = EvaluatorSource([_judge()])
    target = ScorerTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")

    await Migrator(source, target, checkpoint).run(scorer_selection(Resource.ONLINE_EVALS))

    assert len(target.function_writes) == 1
    assert len(target.score_writes) == 1
    _, payload = target.score_writes[0]
    assert payload["score_type"] == "online"
    assert payload["config"]["online"]["scorers"] == [{"type": "function", "id": "fn-1"}]
    assert payload["config"]["online"]["scope"]["type"] == "trace"
    assert checkpoint.completed("scorer:rule-1")
    assert checkpoint.completed("online-eval:rule-1")


async def test_dry_run_inventories_opt_in_scorers_and_online_evals(tmp_path) -> None:
    class Capture:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def message(self, message: str) -> None:
            self.lines.append(message)

    source = EvaluatorSource(
        [
            _judge(),
            {
                "id": "py-1",
                "name": "Length",
                "type": "user_defined_metric_python",
                "enabled": True,
            },
        ]
    )
    progress = Capture()
    await Migrator(
        source,
        object(),
        Checkpoint(tmp_path / "checkpoint.json"),
        progress=progress,
    ).run(
        Selection(
            resources={Resource.SCORERS, Resource.ONLINE_EVALS},
            projects=None,
            datasets=None,
            experiments=None,
            start=None,
            end=None,
            dry_run=True,
        )
    )
    joined = "\n".join(progress.lines)
    assert "2 scorer(s)" in joined
    assert "2 online eval(s)" in joined
    assert "Warning: online scoring will score new production logs" in joined
    assert "Hallucination (llm_as_judge/trace): scorer translate, online translate" in joined
    assert "Length (user_defined_metric_python/trace): scorer skipped" in joined


async def test_python_metrics_skip_and_disabled_rules_keep_scorers(tmp_path) -> None:
    source = EvaluatorSource(
        [
            {
                "id": "py-1",
                "name": "Length",
                "type": "user_defined_metric_python",
                "enabled": True,
            },
            _judge(id="rule-2", enabled=False),
        ]
    )
    target = ScorerTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")

    await Migrator(source, target, checkpoint).run(
        scorer_selection(Resource.SCORERS, Resource.ONLINE_EVALS)
    )

    assert len(target.function_writes) == 1
    assert target.function_writes[0][1]["name"] == "Hallucination"
    assert target.score_writes == []
    assert checkpoint.completed("scorer:py-1")
    assert checkpoint.completed("scorer:rule-2")
    assert checkpoint.completed("online-eval:py-1")
    assert checkpoint.completed("online-eval:rule-2")


class ReviewSource:
    def __init__(self, definitions, queues, traces=None, threads=None) -> None:
        self._definitions = definitions
        self._queues = queues
        self._traces = traces or []
        self._threads = threads or []

    async def projects(self):
        return [{"id": "project-1", "name": "selected"}]

    async def feedback_definitions(self):
        return self._definitions

    async def annotation_queues(self, project_id):
        del project_id
        return self._queues

    async def annotation_queue_traces(self, project_id, queue_id):
        del project_id, queue_id
        return self._traces

    async def annotation_queue_threads(self, project_id, queue_id):
        del project_id, queue_id
        return self._threads

    async def traces_for_thread(self, project_id, thread_id):
        del project_id
        return [
            trace for trace in self._traces if as_dict_trace(trace).get("thread_id") == thread_id
        ]


def as_dict_trace(trace):
    return trace if isinstance(trace, dict) else {}


def _definition(**overrides):
    item = {
        "id": "def-1",
        "name": "Grounded",
        "type": "boolean",
        "details": {"true_label": "yes", "false_label": "no"},
    }
    item.update(overrides)
    return item


def _queue(**overrides):
    item = {
        "id": "queue-1",
        "name": "Hallucination backlog",
        "project_id": "project-1",
        "scope": "trace",
        "instructions": "Mark grounded answers.",
        "feedback_definition_names": ["Grounded"],
        "items_count": 1,
    }
    item.update(overrides)
    return item


async def test_review_scores_write_human_review_widgets(tmp_path) -> None:
    source = ReviewSource([_definition()], [])
    target = ScorerTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")

    await Migrator(source, target, checkpoint).run(scorer_selection(Resource.REVIEW_SCORES))
    await Migrator(source, target, checkpoint).run(scorer_selection(Resource.REVIEW_SCORES))

    assert len(target.score_writes) == 1
    _, payload = target.score_writes[0]
    assert payload["score_type"] == "categorical"
    assert checkpoint.completed("review-score:project-1:def-1")


async def test_annotation_queues_write_review_scores_then_flag_items(tmp_path) -> None:
    source = ReviewSource(
        [_definition()],
        [_queue()],
        traces=[{"id": "trace-9", "thread_id": None}],
    )
    target = ScorerTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")

    await Migrator(source, target, checkpoint).run(scorer_selection(Resource.ANNOTATION_QUEUES))

    assert target.score_writes[0][1]["name"] == "Grounded"
    assert target.view_writes[0][1]["view_type"] == "for_review_project_log"
    assert [event["id"] for event in target.flagged] == ["opik:trace:trace-9"]
    assert checkpoint.completed("annotation-queue:queue-1")


async def test_thread_queues_flag_traces_in_the_thread(tmp_path) -> None:
    source = ReviewSource(
        [_definition()],
        [_queue(scope="thread")],
        traces=[{"id": "trace-a", "thread_id": "thread-1"}],
        threads=[{"id": "thread-1"}],
    )
    target = ScorerTarget()

    await Migrator(source, target, Checkpoint(tmp_path / "checkpoint.json")).run(
        scorer_selection(Resource.ANNOTATION_QUEUES)
    )

    assert [event["id"] for event in target.flagged] == ["opik:trace:trace-a"]
    assert target.view_writes[0][1]["options"]["grouping"] == "metadata.thread_id"


class DashboardSource:
    def __init__(self, dashboards) -> None:
        self._dashboards = dashboards

    async def projects(self):
        return [{"id": "project-1", "name": "selected"}]

    async def dashboards(self, project_id=None):
        if project_id is None:
            return [item for item in self._dashboards if not as_dict_trace(item).get("project_id")]
        return [
            item
            for item in self._dashboards
            if as_dict_trace(item).get("project_id") in (None, project_id)
        ]


def _dashboard(**overrides):
    item = {
        "id": "dash-1",
        "name": "Prod overview",
        "type": "multi_project",
        "scope": "workspace",
        "config": {
            "sections": [
                {
                    "widgets": [
                        {
                            "id": "w-traces",
                            "type": "project_metrics",
                            "title": "Trace volume",
                            "config": {"metricType": "TRACE_COUNT"},
                        },
                        {
                            "id": "w-notes",
                            "type": "text_markdown",
                            "title": "Notes",
                            "config": {"content": "n/a"},
                        },
                    ]
                }
            ]
        },
    }
    item.update(overrides)
    return item


async def test_dashboards_write_monitor_views_and_resume(tmp_path) -> None:
    source = DashboardSource([_dashboard()])
    target = ScorerTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")

    await Migrator(source, target, checkpoint).run(scorer_selection(Resource.DASHBOARDS))
    await Migrator(source, target, checkpoint).run(scorer_selection(Resource.DASHBOARDS))

    assert len(target.view_writes) == 1
    _, payload = target.view_writes[0]
    assert payload["view_type"] == "monitor"
    charts = payload["view_data"]["custom_charts"]["charts"]
    traces = charts["w-traces"]["definition"]["measures"]
    assert traces == [{"btql": "id", "aggregator": {"type": "count"}}]
    assert checkpoint.completed("dashboard:project-1:dash-1")


async def test_experiment_dashboards_are_inventoried_not_written(tmp_path) -> None:
    source = DashboardSource([_dashboard(type="experiments")])
    target = ScorerTarget()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")

    await Migrator(source, target, checkpoint).run(scorer_selection(Resource.DASHBOARDS))

    assert getattr(target, "view_writes", []) == []
    assert checkpoint.completed("dashboard:project-1:dash-1")


async def test_dry_run_inventories_dashboard_widgets(tmp_path) -> None:
    class Capture:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def message(self, message: str) -> None:
            self.lines.append(message)

    source = DashboardSource(
        [
            _dashboard(),
            _dashboard(id="dash-2", name="Eval board", type="experiments"),
        ]
    )
    progress = Capture()
    await Migrator(
        source,
        object(),
        Checkpoint(tmp_path / "checkpoint.json"),
        progress=progress,
    ).run(
        Selection(
            resources={Resource.DASHBOARDS},
            projects=None,
            datasets=None,
            experiments=None,
            start=None,
            end=None,
            dry_run=True,
        )
    )
    joined = "\n".join(progress.lines)
    assert "2 dashboard(s)" in joined
    assert "Prod overview (multi_project/workspace): translate" in joined
    assert "Eval board (experiments/workspace): skipped" in joined
    assert "widget Trace volume: timeseries (1 measure(s))" in joined
    assert "markdown is not a Monitor chart" in joined
    assert "Custom charts require a Braintrust Pro or Enterprise plan" in joined
