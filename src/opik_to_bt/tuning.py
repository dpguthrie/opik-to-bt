from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from opik_to_bt.config import Settings

MIB = 1024 * 1024


def _total_memory() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return 4 * 1024 * MIB


@dataclass(frozen=True)
class RuntimeTuning:
    """Automatically selected bounds; environment overrides are for support use."""

    page_size: int
    partition_bytes: int
    resource_workers: int
    buffered_partitions: int
    upload_processes: int
    bt_workers: int

    @classmethod
    def conservative(cls) -> RuntimeTuning:
        return cls(
            page_size=100,
            partition_bytes=64 * MIB,
            resource_workers=2,
            buffered_partitions=2,
            upload_processes=1,
            bt_workers=4,
        )

    @classmethod
    def detect(cls, state_dir: Path, settings: Settings) -> RuntimeTuning:
        cpu = max(1, os.cpu_count() or 1)
        memory = _total_memory()
        disk_path = state_dir if state_dir.exists() else state_dir.parent
        while not disk_path.exists() and disk_path != disk_path.parent:
            disk_path = disk_path.parent
        free_disk = shutil.disk_usage(disk_path).free

        resource_workers = settings.resource_workers or min(8, max(2, cpu // 2))
        buffered = settings.buffered_partitions or 2
        # Leave ample headroom for Opik response objects, bt sync, and the OS.
        memory_bound = memory // max(32, resource_workers * buffered * 8)
        disk_bound = free_disk // max(16, resource_workers * buffered * 4)
        partition_bytes = settings.partition_bytes or min(256 * MIB, memory_bound, disk_bound)
        partition_bytes = max(16 * MIB, partition_bytes)

        upload_processes = settings.upload_processes or min(2, max(1, cpu // 4))
        bt_workers = settings.bt_workers or min(16, max(2, cpu // upload_processes))
        return cls(
            page_size=settings.page_size or 500,
            partition_bytes=partition_bytes,
            resource_workers=resource_workers,
            buffered_partitions=buffered,
            upload_processes=upload_processes,
            bt_workers=bt_workers,
        )
