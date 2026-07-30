from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)


class MigrationProgress:
    """Small progress interface used by the migrator and source client."""

    def message(self, message: str) -> None:
        print(message)

    def start(self, label: str) -> Any:
        del label
        return None

    def page(self, task: Any, *, page: int, items: int, total: int | None) -> None:
        del task, page, items, total

    def uploading(self, task: Any, *, events: int, partition: int) -> None:
        del task, events, partition

    def detail(self, task: Any, status: str) -> None:
        del task, status

    def complete(self, task: Any, *, items: int, partitions: int) -> None:
        del task, items, partitions

    def checkpointed(self, label: str) -> None:
        self.message(f"  {label}: checkpointed")

    def retry(self, *, delay: float, reason: str, attempt: int) -> None:
        self.message(
            f"  Opik throttled or unavailable; retrying in {delay:.0f}s "
            f"(attempt {attempt}: {reason})"
        )


@dataclass
class _TaskState:
    task_id: int
    extracted: int = 0


class RichMigrationProgress(MigrationProgress):
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TextColumn("{task.fields[status]}"),
            console=self.console,
            transient=False,
        )

    def __enter__(self) -> RichMigrationProgress:
        self._progress.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._progress.stop()

    def message(self, message: str) -> None:
        self._progress.console.print(message)

    def start(self, label: str) -> _TaskState:
        task_id = self._progress.add_task(label, total=None, status="starting")
        return _TaskState(task_id)

    def page(
        self,
        task: _TaskState,
        *,
        page: int,
        items: int,
        total: int | None,
    ) -> None:
        task.extracted += items
        self._progress.update(
            task.task_id,
            completed=task.extracted,
            total=total,
            status=f"page {page} fetched",
        )

    def uploading(self, task: _TaskState, *, events: int, partition: int) -> None:
        self._progress.update(
            task.task_id,
            status=f"uploading partition {partition} ({events:,} events)",
        )

    def detail(self, task: _TaskState, status: str) -> None:
        self._progress.update(task.task_id, status=status)

    def complete(self, task: _TaskState, *, items: int, partitions: int) -> None:
        current = self._progress.tasks[task.task_id]
        total = current.total if current.total is not None else task.extracted
        self._progress.update(
            task.task_id,
            completed=total,
            total=total,
            status=f"done: {items:,} events, {partitions} partition(s)",
        )
        self._progress.stop_task(task.task_id)

    def checkpointed(self, label: str) -> None:
        self._progress.console.print(f"  [green]✓[/green] {label}: checkpointed")

    def retry(self, *, delay: float, reason: str, attempt: int) -> None:
        self._progress.console.print(
            f"  [yellow]Opik request paused for {delay:.0f}s[/yellow] (attempt {attempt}: {reason})"
        )
