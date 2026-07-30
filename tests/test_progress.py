from io import StringIO

from rich.console import Console

from opik_to_bt.progress import RichMigrationProgress


def test_rich_progress_reports_pages_uploads_retries_and_completion() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=140)

    with RichMigrationProgress(console) as progress:
        task = progress.start("logs · traces and spans")
        progress.page(task, page=1, items=500, total=750)
        progress.detail(task, "span page 2 · 1,200 matched")
        progress.uploading(task, events=1700, partition=1)
        progress.retry(delay=53, reason="HTTP 429", attempt=2)
        progress.page(task, page=2, items=250, total=750)
        progress.complete(task, items=2000, partitions=2)

    rendered = output.getvalue()
    assert "Opik request paused for 53s" in rendered
    assert "logs · traces and spans" in rendered
    assert "done: 2,000 events, 2 partition(s)" in rendered
