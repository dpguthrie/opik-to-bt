import asyncio
import json

import pytest

from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.pipeline import Page, bounded_gather, run_partitioned
from opik_to_bt.tuning import RuntimeTuning


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


def partition_events(partition) -> list[dict]:
    return [json.loads(line) for line in partition.path.read_bytes().splitlines()]


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


async def test_large_source_page_is_split_at_partition_target(tmp_path) -> None:
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    uploaded_sizes = []
    uploaded_ids = []
    items = [{"id": index, "payload": "x" * 40} for index in range(8)]

    async def pages():
        yield Page(1, items)

    async def upload(partition):
        uploaded_sizes.append(partition.bytes)
        uploaded_ids.extend(event["id"] for event in partition_events(partition))

    events, partitions = await run_partitioned(
        stream_key="dataset:oversized-page",
        pages=pages(),
        transform=transform,
        upload=upload,
        checkpoint=checkpoint,
        tuning=tuning(partition_bytes=120),
    )

    assert events == len(items)
    assert partitions > 1
    assert all(size <= 120 for size in uploaded_sizes)
    assert uploaded_ids == list(range(8))


async def test_mid_page_checkpoint_resumes_without_duplicates(tmp_path) -> None:
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    items = [{"id": index, "payload": "x" * 40} for index in range(8)]
    uploaded_before_failure = []
    attempts = 0

    async def pages():
        yield Page(1, items)

    async def fail_second(partition):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("temporary upload failure")
        uploaded_before_failure.extend(partition_events(partition))

    with pytest.raises(RuntimeError, match="temporary upload failure"):
        await run_partitioned(
            stream_key="dataset:mid-page",
            pages=pages(),
            transform=transform,
            upload=fail_second,
            checkpoint=checkpoint,
            tuning=tuning(partition_bytes=120),
        )

    assert checkpoint.cursor("dataset:mid-page") == 1
    assert checkpoint.offset("dataset:mid-page") > 0

    resumed = []

    async def upload(partition):
        resumed.extend(partition_events(partition))

    await run_partitioned(
        stream_key="dataset:mid-page",
        pages=pages(),
        transform=transform,
        upload=upload,
        checkpoint=checkpoint,
        tuning=tuning(partition_bytes=120),
    )

    migrated_ids = [event["id"] for event in uploaded_before_failure + resumed]
    assert migrated_ids == list(range(8))


async def test_single_event_may_exceed_partition_target(tmp_path) -> None:
    uploaded_sizes = []
    uploaded_counts = []

    async def pages():
        yield Page(1, [{"id": 1, "payload": "x" * 500}])

    async def upload(partition):
        uploaded_sizes.append(partition.bytes)
        uploaded_counts.append(partition.event_count)

    await run_partitioned(
        stream_key="dataset:single-large-event",
        pages=pages(),
        transform=transform,
        upload=upload,
        checkpoint=Checkpoint(tmp_path / "checkpoint.json"),
        tuning=tuning(partition_bytes=100),
    )

    assert len(uploaded_sizes) == 1
    assert uploaded_sizes[0] > 100
    assert uploaded_counts == [1]


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
