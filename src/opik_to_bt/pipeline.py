from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.tuning import RuntimeTuning


@dataclass(frozen=True)
class Page:
    number: int
    items: list[Any]
    total: int | None = None


@dataclass(frozen=True)
class Partition:
    key: str
    events: list[dict[str, Any]]
    bytes: int
    next_page: int


TransformPage = Callable[[list[Any]], Awaitable[list[dict[str, Any]]]]
UploadPartition = Callable[[Partition], Awaitable[None]]


def event_bytes(event: dict[str, Any]) -> int:
    return len(json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()) + 1


async def run_partitioned(
    *,
    stream_key: str,
    pages: AsyncIterator[Page],
    transform: TransformPage,
    upload: UploadPartition,
    checkpoint: Checkpoint,
    tuning: RuntimeTuning,
) -> tuple[int, int]:
    """Overlap extraction/transformation with ordered, resumable partition uploads."""
    queue: asyncio.Queue[Partition | None] = asyncio.Queue(maxsize=tuning.buffered_partitions)
    totals = {"events": 0, "partitions": 0}

    async def produce() -> None:
        events: list[dict[str, Any]] = []
        size = 0
        first_page: int | None = None
        last_page: int | None = None

        async def flush(next_page: int) -> None:
            nonlocal events, size, first_page, last_page
            if not events:
                return
            key = f"{stream_key}:pages:{first_page}-{last_page}"
            await queue.put(Partition(key, events, size, next_page))
            events, size, first_page, last_page = [], 0, None, None

        async for page in pages:
            transformed = await transform(page.items)
            transformed_size = sum(event_bytes(event) for event in transformed)
            if events and size + transformed_size > tuning.partition_bytes:
                await flush(page.number)
            if transformed:
                first_page = page.number if first_page is None else first_page
                last_page = page.number
                events.extend(transformed)
                size += transformed_size
            # A single page may exceed the target, but memory remains bounded by page size.
            if size >= tuning.partition_bytes:
                await flush(page.number + 1)
        if last_page is not None:
            await flush(last_page + 1)
        await queue.put(None)

    async def consume() -> None:
        while partition := await queue.get():
            if not checkpoint.completed(partition.key):
                await upload(partition)
                checkpoint.mark_completed(partition.key)
            checkpoint.set_cursor(stream_key, partition.next_page)
            totals["events"] += len(partition.events)
            totals["partitions"] += 1
            queue.task_done()
        queue.task_done()

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(produce())
            tasks.create_task(consume())
    except* Exception as errors:
        # Keep CLI failures direct and actionable instead of exposing TaskGroup internals.
        raise errors.exceptions[0] from None
    checkpoint.mark_completed(stream_key)
    return totals["events"], totals["partitions"]


async def bounded_gather(
    items: list[Any],
    worker: Callable[[Any], Awaitable[Any]],
    limit: int,
) -> list[Any]:
    semaphore = asyncio.Semaphore(limit)

    async def run(item: Any) -> Any:
        async with semaphore:
            return await worker(item)

    return await asyncio.gather(*(run(item) for item in items))
