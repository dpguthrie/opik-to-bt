from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.config import Resource, parse_datetime
from opik_to_bt.mapping import dataset_event, experiment_events, span_event, trace_event
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
        if independent:
            await asyncio.gather(*independent)
        # Keep related datasets available before their experiment results.
        if Resource.EXPERIMENTS in selection.resources:
            await self._experiments(source_project_id, target_project_id, selection)

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
            self.progress.message(f"  {raw['name']}: {', '.join(parts)}")
