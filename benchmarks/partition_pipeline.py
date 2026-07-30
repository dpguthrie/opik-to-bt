from __future__ import annotations

import argparse
import asyncio
import gc
import json
import resource
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from opik_to_bt.bt_sync_target import BtSyncTarget
from opik_to_bt.checkpoint import Checkpoint
from opik_to_bt.pipeline import Page, Partition, run_partitioned
from opik_to_bt.tuning import RuntimeTuning

MIB = 1024 * 1024


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def payload(record_id: int, size: int) -> str:
    prefix = f"{record_id}:"
    return prefix + "x" * max(0, size - len(prefix))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    total_events = args.pages * args.records_per_page
    total_written = 0
    maximum_partition = 0

    async def pages():
        for page_number in range(1, args.pages + 1):
            offset = (page_number - 1) * args.records_per_page
            yield Page(
                page_number,
                [
                    {
                        "id": offset + index,
                        "payload": payload(offset + index, args.payload_bytes),
                    }
                    for index in range(args.records_per_page)
                ],
                total_events,
            )

    async def transform(items: list[dict[str, Any]]):
        transformed = (
            {
                "id": f"synthetic:{item['id']}",
                "input": {"payload": item["payload"]},
                "metadata": {"source": "synthetic-benchmark"},
            }
            for item in items
        )
        # Main required a materialized transformed page; the experiment consumes
        # the same mapping incrementally, matching the real migration adapters.
        if "path" in Partition.__dataclass_fields__:
            return transformed
        return list(transformed)

    with tempfile.TemporaryDirectory(prefix="opik-to-bt-benchmark-") as directory:
        root = Path(directory)
        checkpoint = Checkpoint(root / "checkpoint.json")
        upload_number = 0

        async def upload(partition: Partition) -> None:
            nonlocal maximum_partition, total_written, upload_number
            upload_number += 1
            if hasattr(partition, "path"):
                path = partition.path
            else:
                # Keep the benchmark runnable against main for before/after comparisons.
                path = root / f"partition-{upload_number}.ndjson"
                await asyncio.to_thread(BtSyncTarget._write_partition, path, partition.events)
            written = path.stat().st_size
            maximum_partition = max(maximum_partition, written)
            total_written += written
            if args.upload_delay_ms:
                await asyncio.sleep(args.upload_delay_ms / 1000)
            if not hasattr(partition, "path"):
                path.unlink()

        tuning = RuntimeTuning(
            page_size=args.records_per_page,
            partition_bytes=args.partition_mib * MIB,
            resource_workers=1,
            buffered_partitions=args.buffered_partitions,
            upload_processes=1,
            bt_workers=1,
        )
        gc.collect()
        started = time.perf_counter()
        events, partitions = await run_partitioned(
            stream_key="benchmark",
            pages=pages(),
            transform=transform,
            upload=upload,
            checkpoint=checkpoint,
            tuning=tuning,
        )
        elapsed = time.perf_counter() - started

    return {
        "pages": args.pages,
        "records_per_page": args.records_per_page,
        "payload_bytes": args.payload_bytes,
        "partition_target_mib": args.partition_mib,
        "buffered_partitions": args.buffered_partitions,
        "upload_delay_ms": args.upload_delay_ms,
        "events": events,
        "partitions": partitions,
        "written_mib": round(total_written / MIB, 2),
        "maximum_partition_mib": round(maximum_partition / MIB, 2),
        "elapsed_seconds": round(elapsed, 3),
        "events_per_second": round(events / elapsed, 1),
        "mib_per_second": round(total_written / MIB / elapsed, 1),
        "peak_rss_mib": round(peak_rss_bytes() / MIB, 2),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--records-per-page", type=int, default=2000)
    parser.add_argument("--payload-bytes", type=int, default=32768)
    parser.add_argument("--partition-mib", type=int, default=32)
    parser.add_argument("--buffered-partitions", type=int, default=2)
    parser.add_argument("--upload-delay-ms", type=int, default=0)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), indent=2, sort_keys=True))
