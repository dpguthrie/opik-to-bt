from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from opik_migrate.config import Settings
from opik_migrate.tuning import RuntimeTuning


class BtSyncTarget:
    """Stage NDJSON and delegate Braintrust writes and upload state to `bt sync`."""

    def __init__(
        self,
        state_dir: Path,
        settings: Settings,
        tuning: RuntimeTuning | None = None,
    ) -> None:
        tuning = tuning or RuntimeTuning.detect(state_dir, settings)
        self.state_dir = state_dir
        self.stage_dir = state_dir / "staged"
        self.sync_root = state_dir / "bt-sync"
        self.api_url = settings.braintrust_url
        self.api_key = settings.braintrust_api_key
        self.upload_slots = asyncio.Semaphore(tuning.upload_processes)
        self.workers = tuning.bt_workers

    async def close(self) -> None:
        return None

    async def check(self) -> None:
        if not shutil.which("bt"):
            raise RuntimeError("The Braintrust CLI (`bt`) is not on PATH. Install bt >= 0.14.0.")

    async def create_project(self, name: str, description: str | None) -> str:
        del description
        return self._handle("project_logs", name, name)

    async def create_dataset(self, project_id: str, name: str, description: str | None) -> str:
        del description
        _, project, _ = self._decode(project_id)
        return self._handle("dataset", project, name)

    async def create_experiment(self, project_id: str, name: str, description: str | None) -> str:
        del description
        _, project, _ = self._decode(project_id)
        return self._handle("experiment", project, name)

    async def insert_dataset(
        self,
        dataset_id: str,
        events: list[dict[str, Any]],
        *,
        partition_key: str | None = None,
    ) -> None:
        await self._push(dataset_id, events, partition_key)

    async def insert_experiment(
        self,
        experiment_id: str,
        events: list[dict[str, Any]],
        *,
        partition_key: str | None = None,
    ) -> None:
        await self._push(experiment_id, events, partition_key)

    async def insert_logs(
        self,
        project_id: str,
        events: list[dict[str, Any]],
        *,
        partition_key: str | None = None,
    ) -> None:
        await self._push(project_id, events, partition_key)

    def _handle(self, kind: str, project: str, name: str) -> str:
        payload = json.dumps([kind, project, name], separators=(",", ":")).encode()
        return f"bt-sync:{base64.urlsafe_b64encode(payload).decode()}"

    def _decode(self, handle: str) -> tuple[str, str, str]:
        encoded = handle.removeprefix("bt-sync:")
        kind, project, name = json.loads(base64.urlsafe_b64decode(encoded))
        return kind, project, name

    async def _push(
        self,
        handle: str,
        events: list[dict[str, Any]],
        partition_key: str | None,
    ) -> None:
        kind, project, name = self._decode(handle)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        identity = f"{handle}\0{partition_key or 'single'}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        path = self.stage_dir / f"{digest}.ndjson"

        async with self.upload_slots:
            await asyncio.to_thread(self._write_partition, path, events)
            environment = os.environ.copy()
            if self.api_key:
                environment["BRAINTRUST_API_KEY"] = self.api_key
            process = await asyncio.create_subprocess_exec(
                "bt",
                "sync",
                "push",
                f"{kind}:{name}",
                "--project",
                project,
                "--in",
                str(path),
                "--root",
                str(self.sync_root / digest),
                "--workers",
                str(self.workers),
                "--api-url",
                self.api_url,
                "--no-input",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            return_code = process.returncode
        if return_code:
            details = output.decode(errors="replace").strip()
            raise RuntimeError(
                f"bt sync push failed with exit code {return_code}"
                + (f":\n{details}" if details else "")
            )
        path.unlink(missing_ok=True)

    @staticmethod
    def _write_partition(path: Path, events: list[dict[str, Any]]) -> None:
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as output:
            for event in events:
                output.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
        temporary.replace(path)
