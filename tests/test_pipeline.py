import asyncio

import pytest

from opik_migrate.checkpoint import Checkpoint
from opik_migrate.pipeline import Page, bounded_gather, run_partitioned
from opik_migrate.tuning import RuntimeTuning


def tuning(*, partition_bytes: int = 80, buffered: int = 1) -> RuntimeTuning:
    return RuntimeTuning(
        page_size=2,
        partition_bytes=partition_bytes,
        resource_workers=2,
        buffered_partitions=buffered,
        upload_processes=1,
        bt_workers=2,
    )


async def page_stream(start: int, end: int):
    for number in range(start, end):
        yield Page(number, [{"id": number, "payload": "x" * 80}])


async def transform(items):
    return items


async def test_partition_pipeline_is_bounded_and_checkpoints_pages(tmp_path) -> None:
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    uploads = []
    release = asyncio.Event()
    first_upload = asyncio.Event()
    transformed_pages = 0

    async def counted_transform(items):
        nonlocal transformed_pages
        transformed_pages += 1
        return items

    async def upload(partition):
        uploads.append(partition)
        if len(uploads) == 1:
            first_upload.set()
            await release.wait()

    task = asyncio.create_task(
        run_partitioned(
            stream_key="dataset:large",
            pages=page_stream(1, 8),
            transform=counted_transform,
            upload=upload,
            checkpoint=checkpoint,
            tuning=tuning(),
        )
    )
    await first_upload.wait()
    await asyncio.sleep(0)
    # One uploading, one queued, and at most one page being transformed.
    assert transformed_pages <= 3
    release.set()
    events, partitions = await task

    assert events == 7
    assert partitions == 7
    assert len({partition.key for partition in uploads}) == 7
    assert checkpoint.cursor("dataset:large") == 8
    assert checkpoint.completed("dataset:large")


async def test_partition_pipeline_resumes_after_failed_upload(tmp_path) -> None:
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    attempts = 0

    async def fail_second(partition):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("temporary upload failure")

    with pytest.raises(RuntimeError, match="temporary upload failure"):
        await run_partitioned(
            stream_key="logs:project:window",
            pages=page_stream(1, 4),
            transform=transform,
            upload=fail_second,
            checkpoint=checkpoint,
            tuning=tuning(),
        )

    assert checkpoint.cursor("logs:project:window") == 2
    assert not checkpoint.completed("logs:project:window")

    resumed_uploads = []

    async def upload(partition):
        resumed_uploads.append(partition.key)

    await run_partitioned(
        stream_key="logs:project:window",
        pages=page_stream(checkpoint.cursor("logs:project:window"), 4),
        transform=transform,
        upload=upload,
        checkpoint=checkpoint,
        tuning=tuning(),
    )
    assert len(resumed_uploads) == 2
    assert checkpoint.cursor("logs:project:window") == 4
    assert checkpoint.completed("logs:project:window")


async def test_bounded_gather_respects_global_limit() -> None:
    active = 0
    maximum = 0

    async def worker(item):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return item * 2

    assert await bounded_gather(list(range(10)), worker, 3) == [item * 2 for item in range(10)]
    assert maximum == 3
