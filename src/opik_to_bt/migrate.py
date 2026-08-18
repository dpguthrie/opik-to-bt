from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.config import PromptHistory, Resource, parse_datetime
from opik_to_bt.mapping import (
    dataset_event,
    evaluator_scope,
    evaluator_type,
    experiment_events,
    online_score_payload,
    prompt_definition,
    prompt_slug,
    scorer_definitions,
    span_event,
    trace_event,
)
from opik_to_bt.pipeline import Page, Partition, bounded_gather, run_partitioned
from opik_to_bt.progress import MigrationProgress
from opik_to_bt.tuning import RuntimeTuning
from opik_to_bt.util import as_dict, isoformat


@dataclass
class Selection:
    resources: set[Resource]
    projects: set[str] | None
    datasets: set[str] | None
    experiments: set[str] | None
    start: datetime | None
    end: datetime | None
    prompts: set[str] | None = None
    prompt_history: PromptHistory = PromptHistory.LATEST
    scorers: set[str] | None = None
    online_evals: set[str] | None = None
    dry_run: bool = False


def selected(name: str, names: set[str] | None) -> bool:
    return names is None or name in names


def in_range(value: Any, start: datetime | None, end: datetime | None) -> bool:
    if value is None:
        return True
    parsed = parse_datetime(value)
    return (start is None or parsed >= start) and (end is None or parsed < end)


class Migrator:
    def __init__(
        self,
        source: Any,
        target: Any,
        checkpoint: Checkpoint,
        tuning: RuntimeTuning | None = None,
        progress: MigrationProgress | None = None,
    ) -> None:
        self.source = source
        self.target = target
        self.checkpoint = checkpoint
        self.tuning = tuning or RuntimeTuning.conservative()
        self.progress = progress or MigrationProgress()
        self.resource_slots = asyncio.Semaphore(self.tuning.resource_workers)

    async def _resource(self, function: Any, /, *args: Any) -> Any:
        async with self.resource_slots:
            return await function(*args)

    async def _legacy_page(self, awaitable: Any) -> Any:
        yield Page(1, list(await awaitable))

    async def _observed_pages(self, task: Any, pages: Any) -> Any:
        async for page in pages:
            self.progress.page(
                task,
                page=page.number,
                items=len(page.items),
                total=page.total,
            )
            yield page

    async def _apply_tags(self, kind: str, source_id: str, tags: Any) -> None:
        """Tag the destination object, whether this run uploaded it or resumed past it."""
        handle = self.checkpoint.target(kind, source_id)
        if handle and tags:
            await self.target.apply_tags(handle, [str(tag) for tag in tags])

    async def _upload(
        self,
        method: Any,
        target_id: str,
        partition: Partition,
    ) -> None:
        await method(
            target_id,
            partition.path,
            partition_key=partition.key,
        )

    async def run(self, selection: Selection) -> None:
        if selection.end is None and {Resource.EXPERIMENTS, Resource.LOGS} & selection.resources:
            saved_end = self.checkpoint.value("implicit_end")
            selection.end = parse_datetime(saved_end) if saved_end else datetime.now(UTC)
            if not selection.dry_run and not saved_end:
                self.checkpoint.set_value("implicit_end", isoformat(selection.end))
            self.progress.message(f"Snapshot end: {isoformat(selection.end)}")
        projects = [
            project
            for project in await self.source.projects()
            if selected(as_dict(project)["name"], selection.projects)
        ]
        if selection.projects:
            found = {as_dict(project)["name"] for project in projects}
            missing = selection.projects - found
            if missing:
                raise RuntimeError(f"Opik projects not found: {', '.join(sorted(missing))}")
        self.progress.message(f"Selected {len(projects)} project(s)")
        if Resource.ONLINE_EVALS in selection.resources:
            self.progress.message(
                "Warning: online scoring will score new production logs in Braintrust. "
                "Already migrated traces are not re-scored. Omit online-evals to copy "
                "scorer definitions without attaching live scoring."
            )
        if selection.dry_run:
            await self._inventory(projects, selection)
            return
        await self.target.check()
        await bounded_gather(
            projects,
            lambda project: self._project(project, selection),
            min(2, self.tuning.resource_workers),
        )

    async def _project(self, project: Any, selection: Selection) -> None:
        raw = as_dict(project)
        source_project_id = str(raw["id"])
        target_project_id = self.checkpoint.target("project", source_project_id)
        if not target_project_id:
            target_project_id = await self.target.create_project(
                raw["name"], raw.get("description")
            )
            self.checkpoint.set_target("project", source_project_id, target_project_id)
        self.progress.message(f"\n[bold]Project: {raw['name']}[/bold]")

        independent = []
        if Resource.DATASETS in selection.resources:
            independent.append(self._datasets(source_project_id, target_project_id, selection))
        if Resource.LOGS in selection.resources:
            independent.append(
                self._resource(
                    self._logs,
                    raw["name"],
                    source_project_id,
                    target_project_id,
                    selection,
                )
            )
        if Resource.PROMPTS in selection.resources:
            independent.append(self._prompts(source_project_id, target_project_id, selection))
        if Resource.SCORERS in selection.resources:
            independent.append(self._scorers(source_project_id, target_project_id, selection))
        if independent:
            await asyncio.gather(*independent)
        # Keep related datasets available before their experiment results.
        if Resource.EXPERIMENTS in selection.resources:
            await self._experiments(source_project_id, target_project_id, selection)
        if Resource.ONLINE_EVALS in selection.resources:
            await self._online_evals(source_project_id, target_project_id, selection)

    @staticmethod
    def _prompt_version_id(version: Any) -> str:
        raw = as_dict(version)
        version_id = raw.get("id") or raw.get("version_number") or raw.get("commit")
        if not version_id:
            raise RuntimeError("Opik returned a prompt version without an identifier")
        return str(version_id)

    @staticmethod
    def _prompt_matches(current: dict[str, Any], definition: dict[str, Any]) -> bool:
        fields = ("name", "slug", "description", "prompt_data")
        return all(current.get(field) == definition.get(field) for field in fields) and (
            current.get("tags") or []
        ) == (definition.get("tags") or [])

    @staticmethod
    def _function_matches(current: dict[str, Any], definition: dict[str, Any]) -> bool:
        fields = (
            "name",
            "slug",
            "description",
            "function_type",
            "function_data",
            "prompt_data",
        )
        return all(current.get(field) == definition.get(field) for field in fields) and (
            current.get("tags") or []
        ) == (definition.get("tags") or [])

    @staticmethod
    def _online_score_matches(current: dict[str, Any], definition: dict[str, Any]) -> bool:
        return (
            current.get("name") == definition.get("name")
            and current.get("score_type") == definition.get("score_type")
            and current.get("config") == definition.get("config")
        )

    async def _prompts(
        self, source_project_id: str, target_project_id: str, selection: Selection
    ) -> None:
        prompts = [
            item
            for item in await self.source.prompts(source_project_id)
            if selected(as_dict(item)["name"], selection.prompts)
        ]
        await bounded_gather(
            prompts,
            lambda prompt: self._resource(
                self._prompt,
                prompt,
                target_project_id,
                selection.prompt_history,
            ),
            self.tuning.resource_workers,
        )

    async def _prompt(
        self,
        prompt: Any,
        target_project_id: str,
        history: PromptHistory,
    ) -> None:
        raw = as_dict(prompt)
        source_prompt_id = str(raw["id"])
        mode_key = f"prompt:{source_prompt_id}:mode"
        stored_mode = self.checkpoint.value(mode_key)
        if stored_mode == PromptHistory.LATEST and history == PromptHistory.ALL:
            raise RuntimeError(
                f"Prompt {raw['name']!r} was already migrated in latest-only mode. "
                "Its older versions cannot be inserted before the existing Braintrust version; "
                "use a new state directory and remove or rename the destination prompt first."
            )

        all_complete = self.checkpoint.completed(f"prompt:{source_prompt_id}:all")
        completion_key = f"prompt:{source_prompt_id}:{history.value}"
        if self.checkpoint.completed(completion_key) or (
            history == PromptHistory.LATEST and all_complete
        ):
            self.progress.checkpointed(f"prompt {raw['name']}")
            return

        if stored_mode is None or history == PromptHistory.ALL:
            self.checkpoint.set_value(mode_key, history.value)

        task = self.progress.start(f"prompt · {raw['name']}")
        if history == PromptHistory.ALL:
            versions = await self.source.prompt_versions(source_prompt_id)
        else:
            detail = as_dict(await self.source.prompt_detail(source_prompt_id))
            latest = detail.get("latest_version")
            versions = [latest] if latest is not None else []

        if not versions:
            self.checkpoint.mark_completed(completion_key)
            self.progress.complete(task, items=0, partitions=0)
            return

        slug_key = f"prompt:{source_prompt_id}:slug"
        slug = self.checkpoint.value(slug_key) or prompt_slug(raw["name"], source_prompt_id)
        self.checkpoint.set_value(slug_key, slug)
        current = await self.target.get_prompt(target_project_id, slug)
        target_id = self.checkpoint.target("prompt", source_prompt_id)
        if current is None and target_id:
            raise RuntimeError(
                f"Checkpointed Braintrust prompt {target_id!r} for {raw['name']!r} no longer exists"
            )
        if current and target_id and str(current["id"]) != target_id:
            raise RuntimeError(
                f"Braintrust prompt slug {slug!r} no longer resolves to the checkpointed prompt"
            )

        versions_key = f"prompt:{source_prompt_id}:versions"
        migrated_versions = dict(self.checkpoint.value(versions_key) or {})
        source_version_ids = [self._prompt_version_id(version) for version in versions]
        mapped_ids = [
            version_id for version_id in source_version_ids if version_id in migrated_versions
        ]
        if history == PromptHistory.ALL:
            unexpected_ids = set(migrated_versions) - set(source_version_ids)
            if unexpected_ids:
                raise RuntimeError(
                    f"Prompt history for {raw['name']!r} no longer contains checkpointed "
                    f"version(s): {', '.join(sorted(unexpected_ids))}"
                )
            if mapped_ids != source_version_ids[: len(mapped_ids)]:
                raise RuntimeError(
                    f"Prompt history checkpoint for {raw['name']!r} is not a chronological prefix"
                )

        recovery_candidate = current is not None
        if mapped_ids:
            if current is None:
                raise RuntimeError(
                    f"Braintrust prompt {slug!r} is missing after checkpointed history writes"
                )
            expected_current_version = migrated_versions[mapped_ids[-1]]
            if str(current.get("_xact_id")) == str(expected_current_version):
                # The last checkpointed write is still current. Even if the next
                # source version has identical content, it needs its own PUT.
                recovery_candidate = False
            elif len(mapped_ids) == len(source_version_ids):
                raise RuntimeError(
                    f"Braintrust prompt {slug!r} changed after its last checkpoint; "
                    "refusing to mark unexpected content as migrated."
                )

        for index, version in enumerate(versions, start=1):
            source_version_id = self._prompt_version_id(version)
            if source_version_id in migrated_versions:
                continue
            definition = {
                **prompt_definition(prompt, version),
                "slug": slug,
            }
            self.progress.detail(task, f"version {index}/{len(versions)}")

            matches_current = current is not None and self._prompt_matches(current, definition)
            if matches_current and (history == PromptHistory.LATEST or recovery_candidate):
                written = current
            else:
                if (
                    history == PromptHistory.ALL
                    and recovery_candidate
                    and current is not None
                    and migrated_versions
                ):
                    raise RuntimeError(
                        f"Braintrust prompt {slug!r} changed after its last checkpoint; "
                        "refusing to append history to an unexpected version."
                    )
                if history == PromptHistory.ALL and current is not None and not migrated_versions:
                    raise RuntimeError(
                        f"Braintrust prompt {slug!r} already exists with content that does not "
                        "match the oldest Opik version; refusing to create incorrectly ordered "
                        "history."
                    )
                written = await self.target.write_prompt(
                    target_project_id,
                    definition,
                    update=current is not None,
                )

            target_id = str(written["id"])
            target_version = str(written["_xact_id"])
            self.checkpoint.set_target("prompt", source_prompt_id, target_id)
            migrated_versions[source_version_id] = target_version
            self.checkpoint.set_value(versions_key, migrated_versions)
            current = written
            recovery_candidate = False

        self.checkpoint.mark_completed(completion_key)
        self.progress.complete(task, items=len(versions), partitions=0)

    async def _evaluators(self, source_project_id: str, names: set[str] | None) -> list[Any]:
        return [
            item
            for item in await self.source.evaluators(source_project_id)
            if selected(as_dict(item)["name"], names)
        ]

    async def _scorers(
        self, source_project_id: str, target_project_id: str, selection: Selection
    ) -> None:
        evaluators = await self._evaluators(source_project_id, selection.scorers)
        await bounded_gather(
            evaluators,
            lambda evaluator: self._resource(self._write_scorers, evaluator, target_project_id),
            self.tuning.resource_workers,
        )

    async def _write_scorers(self, evaluator: Any, target_project_id: str) -> list[str]:
        raw = as_dict(evaluator)
        source_id = str(raw["id"])
        completion_key = f"scorer:{source_id}"
        functions_key = f"scorer:{source_id}:functions"
        if self.checkpoint.completed(completion_key):
            self.progress.checkpointed(f"scorer {raw['name']}")
            stored = self.checkpoint.value(functions_key) or {}
            return list(stored.values())

        definitions, skip = scorer_definitions(evaluator)
        if skip:
            self.progress.message(f"  scorer {raw['name']}: skipped — {skip}")
            self.checkpoint.set_value(functions_key, {})
            self.checkpoint.mark_completed(completion_key)
            return []

        task = self.progress.start(f"scorer · {raw['name']}")
        migrated = dict(self.checkpoint.value(functions_key) or {})
        function_ids = []
        for definition in definitions:
            slug = str(definition["slug"])
            if slug in migrated:
                function_ids.append(migrated[slug])
                continue
            current = await self.target.get_function(target_project_id, slug)
            if current is not None and self._function_matches(current, definition):
                written = current
            else:
                written = await self.target.write_function(
                    target_project_id,
                    definition,
                    update=current is not None,
                )
            function_id = str(written["id"])
            migrated[slug] = function_id
            function_ids.append(function_id)
            self.checkpoint.set_target("scorer", f"{source_id}:{slug}", function_id)
            self.checkpoint.set_value(functions_key, migrated)

        self.checkpoint.mark_completed(completion_key)
        self.progress.complete(task, items=len(definitions), partitions=0)
        return function_ids

    async def _online_evals(
        self, source_project_id: str, target_project_id: str, selection: Selection
    ) -> None:
        evaluators = await self._evaluators(source_project_id, selection.online_evals)
        await bounded_gather(
            evaluators,
            lambda evaluator: self._resource(self._online_eval, evaluator, target_project_id),
            self.tuning.resource_workers,
        )

    async def _online_eval(self, evaluator: Any, target_project_id: str) -> None:
        raw = as_dict(evaluator)
        source_id = str(raw["id"])
        completion_key = f"online-eval:{source_id}"
        if self.checkpoint.completed(completion_key):
            self.progress.checkpointed(f"online eval {raw['name']}")
            return

        function_ids = await self._write_scorers(evaluator, target_project_id)
        payload, skip = online_score_payload(evaluator, function_ids)
        if skip:
            self.progress.message(f"  online eval {raw['name']}: skipped — {skip}")
            self.checkpoint.mark_completed(completion_key)
            return

        task = self.progress.start(f"online eval · {raw['name']}")
        current = await self.target.get_project_score(target_project_id, payload["name"])
        if current is not None and self._online_score_matches(current, payload):
            written = current
        else:
            written = await self.target.write_project_score(
                target_project_id,
                payload,
                update=current is not None,
            )
        self.checkpoint.set_target("online-eval", source_id, str(written["id"]))
        self.checkpoint.mark_completed(completion_key)
        self.progress.complete(task, items=1, partitions=0)

    async def _datasets(
        self, source_project_id: str, target_project_id: str, selection: Selection
    ) -> None:
        datasets = [
            item
            for item in await self.source.datasets(source_project_id)
            if selected(as_dict(item)["name"], selection.datasets)
        ]
        await bounded_gather(
            datasets,
            lambda dataset: self._resource(self._dataset, dataset, target_project_id),
            self.tuning.resource_workers,
        )

    async def _dataset(self, dataset: Any, target_project_id: str) -> None:
        raw = as_dict(dataset)
        stream_key = f"dataset:{raw['id']}"
        if self.checkpoint.completed(stream_key):
            self.progress.checkpointed(f"dataset {raw['name']}")
            await self._apply_tags("dataset", str(raw["id"]), raw.get("tags"))
            return
        self.checkpoint.bind_page_size(stream_key, self.tuning.page_size)
        target_id = self.checkpoint.target("dataset", str(raw["id"]))
        if not target_id:
            target_id = await self.target.create_dataset(
                target_project_id, raw["name"], raw.get("description")
            )
            self.checkpoint.set_target("dataset", str(raw["id"]), target_id)

        task = self.progress.start(f"dataset · {raw['name']}")
        partition_number = 0

        async def transform(items: list[Any]) -> Iterable[dict[str, Any]]:
            return (dataset_event(item) for item in items)

        async def upload(partition: Partition) -> None:
            nonlocal partition_number
            partition_number += 1
            self.progress.uploading(
                task,
                events=partition.event_count,
                partition=partition_number,
            )
            await self._upload(self.target.insert_dataset, target_id, partition)

        pages = (
            self.source.dataset_item_pages(raw["id"], start_page=self.checkpoint.cursor(stream_key))
            if hasattr(self.source, "dataset_item_pages")
            else self._legacy_page(self.source.dataset_items(raw["id"]))
        )

        count, partitions = await run_partitioned(
            stream_key=stream_key,
            pages=self._observed_pages(task, pages),
            transform=transform,
            upload=upload,
            checkpoint=self.checkpoint,
            tuning=self.tuning,
        )
        await self._apply_tags("dataset", str(raw["id"]), raw.get("tags"))
        self.progress.complete(task, items=count, partitions=partitions)

    async def _experiments(
        self, source_project_id: str, target_project_id: str, selection: Selection
    ) -> None:
        experiments = [
            item
            for item in await self.source.experiments(source_project_id)
            if selected(as_dict(item)["name"], selection.experiments)
            and in_range(as_dict(item).get("created_at"), selection.start, selection.end)
        ]
        await bounded_gather(
            experiments,
            lambda experiment: self._resource(self._experiment, experiment, target_project_id),
            self.tuning.resource_workers,
        )

    async def _experiment(self, experiment: Any, target_project_id: str) -> None:
        raw = as_dict(experiment)
        stream_key = f"experiment:{raw['id']}"
        if self.checkpoint.completed(stream_key):
            self.progress.checkpointed(f"experiment {raw['name']}")
            await self._apply_tags("experiment", str(raw["id"]), raw.get("tags"))
            return
        self.checkpoint.bind_page_size(stream_key, self.tuning.page_size)
        source_dataset_id = raw.get("dataset_id")
        if not source_dataset_id:
            raise RuntimeError(
                f"Experiment {raw['name']!r} has no dataset_id; "
                "paged migration cannot safely enumerate its results."
            )
        target_id = self.checkpoint.target("experiment", str(raw["id"]))
        if not target_id:
            target_id = await self.target.create_experiment(
                target_project_id, raw["name"], raw.get("description")
            )
            self.checkpoint.set_target("experiment", str(raw["id"]), target_id)

        task = self.progress.start(f"experiment · {raw['name']}")
        partition_number = 0

        async def transform(items: list[Any]) -> Iterable[dict[str, Any]]:
            return (event for item in items for event in experiment_events(item))

        async def upload(partition: Partition) -> None:
            nonlocal partition_number
            partition_number += 1
            self.progress.uploading(
                task,
                events=partition.event_count,
                partition=partition_number,
            )
            await self._upload(self.target.insert_experiment, target_id, partition)

        pages = (
            self.source.experiment_item_pages(
                str(raw["id"]),
                str(source_dataset_id),
                start_page=self.checkpoint.cursor(stream_key),
            )
            if hasattr(self.source, "experiment_item_pages")
            else self._legacy_page(self.source.experiment_items(raw["id"]))
        )

        count, partitions = await run_partitioned(
            stream_key=stream_key,
            pages=self._observed_pages(task, pages),
            transform=transform,
            upload=upload,
            checkpoint=self.checkpoint,
            tuning=self.tuning,
        )
        await self._apply_tags("experiment", str(raw["id"]), raw.get("tags"))
        self.progress.complete(task, items=count, partitions=partitions)

    async def _logs(
        self,
        project_name: str,
        source_project_id: str,
        target_project_id: str,
        selection: Selection,
    ) -> None:
        stream_key = f"logs:{source_project_id}:{selection.start}:{selection.end}"
        if self.checkpoint.completed(stream_key):
            self.progress.checkpointed("logs")
            return
        self.checkpoint.bind_page_size(stream_key, self.tuning.page_size)
        task = self.progress.start("logs · traces and spans")
        partition_number = 0

        async def transform(traces: list[Any]) -> Iterable[dict[str, Any]]:
            traces = [
                trace
                for trace in traces
                if in_range(
                    as_dict(trace).get("start_time"),
                    selection.start,
                    selection.end,
                )
            ]
            trace_ids = {str(as_dict(trace)["id"]) for trace in traces}
            spans = []
            span_page = 0
            matched_spans = 0
            async for page in self.source.span_pages_for_traces(
                project_name,
                trace_ids=trace_ids,
                start=selection.start,
                end=selection.end,
            ):
                span_page += 1
                matched = [
                    span for span in page.items if as_dict(span).get("trace_id") in trace_ids
                ]
                matched_spans += len(matched)
                spans.extend(matched)
                self.progress.detail(
                    task,
                    f"span page {span_page} · {matched_spans:,} matched",
                )
            spans_by_trace: dict[str, list[Any]] = {}
            for span in spans:
                spans_by_trace.setdefault(str(as_dict(span)["trace_id"]), []).append(span)
            traces_with_spans = set(spans_by_trace)

            def events() -> Any:
                for trace in traces:
                    trace_id = str(as_dict(trace)["id"])
                    yield trace_event(
                        trace,
                        include_aggregate_metrics=trace_id not in traces_with_spans,
                        spans=spans_by_trace.get(trace_id),
                    )
                for span in spans:
                    yield span_event(as_dict(span)["trace_id"], span)

            return events()

        async def upload(partition: Partition) -> None:
            nonlocal partition_number
            partition_number += 1
            self.progress.uploading(
                task,
                events=partition.event_count,
                partition=partition_number,
            )
            await self._upload(self.target.insert_logs, target_project_id, partition)

        pages = self.source.trace_pages(
            project_name,
            start=selection.start,
            end=selection.end,
            start_page=self.checkpoint.cursor(stream_key),
        )
        count, partitions = await run_partitioned(
            stream_key=stream_key,
            pages=self._observed_pages(task, pages),
            transform=transform,
            upload=upload,
            checkpoint=self.checkpoint,
            tuning=self.tuning,
        )
        self.progress.complete(task, items=count, partitions=partitions)

    async def _inventory(self, projects: list[Any], selection: Selection) -> None:
        for project in projects:
            raw = as_dict(project)
            parts = []
            if Resource.DATASETS in selection.resources:
                datasets = [
                    item
                    for item in await self.source.datasets(raw["id"])
                    if selected(as_dict(item)["name"], selection.datasets)
                ]
                parts.append(f"{len(datasets)} dataset(s)")
            if Resource.EXPERIMENTS in selection.resources:
                experiments = [
                    item
                    for item in await self.source.experiments(raw["id"])
                    if selected(as_dict(item)["name"], selection.experiments)
                    and in_range(as_dict(item).get("created_at"), selection.start, selection.end)
                ]
                parts.append(f"{len(experiments)} experiment(s)")
            if Resource.LOGS in selection.resources:
                parts.append("logs in selected date range")
            if Resource.PROMPTS in selection.resources:
                prompts = [
                    item
                    for item in await self.source.prompts(raw["id"])
                    if selected(as_dict(item)["name"], selection.prompts)
                ]
                parts.append(
                    f"{len(prompts)} prompt(s), {selection.prompt_history.value} version mode"
                )
            extra: list[str] = []
            details: list[str] = []
            if (
                Resource.SCORERS in selection.resources
                or Resource.ONLINE_EVALS in selection.resources
            ):
                extra, details = await self._inventory_evaluators(raw["id"], selection)
            parts.extend(extra)
            self.progress.message(f"  {raw['name']}: {', '.join(parts)}")
            for line in details:
                self.progress.message(line)

    async def _inventory_evaluators(
        self, source_project_id: str, selection: Selection
    ) -> tuple[list[str], list[str]]:
        evaluators = await self.source.evaluators(source_project_id)
        scorer_count = 0
        online_count = 0
        details: list[str] = []
        extra: list[str] = []
        for evaluator in evaluators:
            raw = as_dict(evaluator)
            include_scorer = Resource.SCORERS in selection.resources and selected(
                raw["name"], selection.scorers
            )
            include_online = Resource.ONLINE_EVALS in selection.resources and selected(
                raw["name"], selection.online_evals
            )
            if not include_scorer and not include_online:
                continue
            definitions, scorer_skip = scorer_definitions(evaluator)
            if include_scorer:
                scorer_count += 1
            function_ids = [f"dry-run-{index}" for index in range(len(definitions))]
            _, online_skip = online_score_payload(evaluator, function_ids)
            if include_online:
                online_count += 1
            kind = evaluator_type(evaluator) or "unknown"
            scope = evaluator_scope(evaluator) or "n/a"
            scorer_status = f"skipped — {scorer_skip}" if scorer_skip else "translate"
            online_status = f"skipped — {online_skip}" if online_skip else "translate"
            bits = []
            if include_scorer:
                bits.append(f"scorer {scorer_status}")
            if include_online:
                bits.append(f"online {online_status}")
            details.append(f"    {raw['name']} ({kind}/{scope}): {', '.join(bits)}")
        if Resource.SCORERS in selection.resources:
            extra.append(f"{scorer_count} scorer(s)")
        if Resource.ONLINE_EVALS in selection.resources:
            extra.append(f"{online_count} online eval(s)")
        return extra, details
