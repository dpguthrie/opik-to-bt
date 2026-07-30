from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
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
    path: Path
    event_count: int
    bytes: int
    next_page: int
    next_offset: int


TransformPage = Callable[[list[Any]], Awaitable[Iterable[dict[str, Any]]]]
UploadPartition = Callable[[Partition], Awaitable[None]]


def encode_event(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


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
        output = None
        path: Path | None = None
        size = 0
        event_count = 0
        first_page: int | None = None
        first_offset: int | None = None
        last_page: int | None = None
        resume_page = checkpoint.cursor(stream_key)
        resume_offset = checkpoint.offset(stream_key)

        async def flush(next_page: int, next_offset: int) -> None:
            nonlocal output, path, size, event_count
            nonlocal first_page, first_offset, last_page
            if output is None or path is None:
                return
            output.close()
            key = f"{stream_key}:events:{first_page}.{first_offset}-{next_page}.{next_offset}"
            await queue.put(
                Partition(
                    key=key,
                    path=path,
                    event_count=event_count,
                    bytes=size,
                    next_page=next_page,
                    next_offset=next_offset,
                )
            )
            output = None
            path = None
            size = 0
            event_count = 0
            first_page = None
            first_offset = None
            last_page = None

        try:
            async for page in pages:
                transformed = await transform(page.items)
                offset = resume_offset if page.number == resume_page else 0
                saw_event = False
                for index, event in enumerate(transformed):
                    if index < offset:
                        continue
                    saw_event = True
                    encoded = encode_event(event)
                    if output is not None and size + len(encoded) > tuning.partition_bytes:
                        await flush(page.number, index)
                    if output is None:
                        temporary = tempfile.NamedTemporaryFile(  # noqa: SIM115
                            mode="wb",
                            buffering=1024 * 1024,
                            dir=buffer_dir,
                            prefix="partition-",
                            suffix=".ndjson",
                            delete=False,
                        )
                        output = temporary
                        path = Path(temporary.name)
                        first_page = page.number
                        first_offset = index
                    last_page = page.number
                    output.write(encoded)
                    size += len(encoded)
                    event_count += 1
                if saw_event and size >= tuning.partition_bytes:
                    await flush(page.number + 1, 0)
            if last_page is not None:
                await flush(last_page + 1, 0)
            await queue.put(None)
        finally:
            if output is not None and not output.closed:
                output.close()

    async def consume() -> None:
        while partition := await queue.get():
            if not checkpoint.completed(partition.key):
                await upload(partition)
                checkpoint.mark_completed(partition.key)
            partition.path.unlink(missing_ok=True)
            checkpoint.set_position(
                stream_key,
                partition.next_page,
                partition.next_offset,
            )
            totals["events"] += partition.event_count
            totals["partitions"] += 1
            queue.task_done()
        queue.task_done()

    buffer_root = checkpoint.path.parent / "buffers"
    buffer_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stream-", dir=buffer_root) as directory:
        buffer_dir = Path(directory)
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
