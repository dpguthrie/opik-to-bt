from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from opik_to_bt.bt_sync_target import BtSyncTarget
from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.config import Settings, parse_csv, parse_datetime, parse_resources
from opik_to_bt.migrate import Migrator, Selection
from opik_to_bt.opik_source import OpikSource
from opik_to_bt.progress import RichMigrationProgress
from opik_to_bt.tuning import RuntimeTuning

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.command()
def main(
    projects: Annotated[
        str | None, typer.Option(help="Comma-separated Opik project names.")
    ] = None,
    resources: Annotated[
        str, typer.Option(help="all, or a comma-separated list: datasets,experiments,logs.")
    ] = "all",
    datasets: Annotated[
        str | None, typer.Option(help="Optional comma-separated dataset names.")
    ] = None,
    experiments: Annotated[
        str | None, typer.Option(help="Optional comma-separated experiment names.")
    ] = None,
    start: Annotated[
        str | None, typer.Option(help="Inclusive ISO-8601 UTC start for experiments and logs.")
    ] = None,
    end: Annotated[
        str | None, typer.Option(help="Exclusive ISO-8601 UTC end for experiments and logs.")
    ] = None,
    state_dir: Annotated[
        Path, typer.Option(help="Directory for resumable migration state.")
    ] = Path(".opik-to-bt"),
    resume: Annotated[
        bool, typer.Option("--resume/--no-resume", help="Resume completed resources.")
    ] = True,
    dry_run: Annotated[
        bool, typer.Option(help="Show selected resources without writing to Braintrust.")
    ] = False,
) -> None:
    """Migrate selected Opik resources into Braintrust."""
    try:
        settings = Settings()
        start_at, end_at = parse_datetime(start), parse_datetime(end)
        if start_at and end_at and start_at >= end_at:
            raise ValueError("--start must be before --end")
        selection = Selection(
            resources=parse_resources(resources),
            projects=parse_csv(projects),
            datasets=parse_csv(datasets),
            experiments=parse_csv(experiments),
            start=start_at,
            end=end_at,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    asyncio.run(_run(settings, selection, state_dir, resume))


async def _run(settings: Settings, selection: Selection, state_dir: Path, resume: bool) -> None:
    tuning = RuntimeTuning.detect(state_dir, settings)
    target = BtSyncTarget(state_dir, settings, tuning, fresh=not resume)
    checkpoint = Checkpoint(state_dir / "checkpoint.json", resume=resume)
    typer.echo(
        "Runtime: automatic "
        f"({tuning.resource_workers} resource workers, "
        f"{tuning.upload_processes} upload slot(s), "
        f"{tuning.partition_bytes // (1024 * 1024)} MiB partitions)"
    )
    with RichMigrationProgress() as progress:
        source = OpikSource(
            settings,
            page_size=tuning.page_size,
            request_workers=tuning.resource_workers,
            on_retry=progress.retry,
        )
        try:
            await Migrator(source, target, checkpoint, tuning, progress).run(selection)
        finally:
            await target.close()


if __name__ == "__main__":
    app()
