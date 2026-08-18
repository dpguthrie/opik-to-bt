from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from opik_to_bt.config import Settings
from opik_to_bt.tuning import RuntimeTuning

# `bt sync` writes rows, so object-level tags need Braintrust's REST API.
TAGGABLE_KINDS = {"dataset", "experiment"}


class BtSyncTarget:
    """Stage NDJSON and delegate Braintrust writes and upload state to `bt sync`."""

    def __init__(
        self,
        state_dir: Path,
        settings: Settings,
        tuning: RuntimeTuning | None = None,
        *,
        fresh: bool = False,
    ) -> None:
        tuning = tuning or RuntimeTuning.detect(state_dir, settings)
        self.state_dir = state_dir
        self.stage_dir = state_dir / "staged"
        self.sync_root = state_dir / "bt-sync"
        self.api_url = settings.braintrust_url
        self.api_key = settings.braintrust_api_key
        self.timeout = settings.timeout_seconds
        self.upload_slots = asyncio.Semaphore(tuning.upload_processes)
        self.workers = tuning.bt_workers
        self.fresh = fresh
        self._project_ids: dict[str, str] = {}
        self._project_descriptions: dict[str, str | None] = {}
        self._project_lock = asyncio.Lock()

    async def close(self) -> None:
        return None

    async def check(self) -> None:
        if not shutil.which("bt"):
            raise RuntimeError("The Braintrust CLI (`bt`) is not on PATH. Install bt >= 0.14.0.")

    async def create_project(self, name: str, description: str | None) -> str:
        self._project_descriptions[name] = description
        return self._handle("project_logs", name, name)

    async def _prompt_project_id(self, project_handle: str) -> str:
        _, project, _ = self._decode(project_handle)
        if cached := self._project_ids.get(project):
            return cached
        async with self._project_lock:
            if cached := self._project_ids.get(project):
                return cached
            query = urllib.parse.urlencode({"project_name": project})
            found = await asyncio.to_thread(self._request, "GET", f"/v1/project?{query}")
            objects = found.get("objects") or []
            if objects:
                project_id = str(objects[0]["id"])
            else:
                created = await asyncio.to_thread(
                    self._request,
                    "POST",
                    "/v1/project",
                    {
                        "name": project,
                        "description": self._project_descriptions.get(project),
                    },
                )
                project_id = str(created["id"])
            self._project_ids[project] = project_id
            return project_id

    async def get_prompt(self, project_handle: str, slug: str) -> dict[str, Any] | None:
        project_id = await self._prompt_project_id(project_handle)
        query = urllib.parse.urlencode({"project_id": project_id, "slug": slug})
        found = await asyncio.to_thread(self._request, "GET", f"/v1/prompt?{query}")
        objects = found.get("objects") or []
        return objects[0] if objects else None

    async def write_prompt(
        self,
        project_handle: str,
        definition: dict[str, Any],
        *,
        update: bool,
    ) -> dict[str, Any]:
        project_id = await self._prompt_project_id(project_handle)
        return await asyncio.to_thread(
            self._request,
            "PUT" if update else "POST",
            "/v1/prompt",
            {"project_id": project_id, **definition},
        )

    async def get_function(self, project_handle: str, slug: str) -> dict[str, Any] | None:
        project_id = await self._prompt_project_id(project_handle)
        query = urllib.parse.urlencode({"project_id": project_id, "slug": slug})
        found = await asyncio.to_thread(self._request, "GET", f"/v1/function?{query}")
        objects = found.get("objects") or []
        return objects[0] if objects else None

    async def write_function(
        self,
        project_handle: str,
        definition: dict[str, Any],
        *,
        update: bool,
    ) -> dict[str, Any]:
        project_id = await self._prompt_project_id(project_handle)
        return await asyncio.to_thread(
            self._request,
            "PUT" if update else "POST",
            "/v1/function",
            {"project_id": project_id, **definition},
        )

    async def get_project_score(self, project_handle: str, name: str) -> dict[str, Any] | None:
        project_id = await self._prompt_project_id(project_handle)
        query = urllib.parse.urlencode({"project_id": project_id, "project_score_name": name})
        found = await asyncio.to_thread(self._request, "GET", f"/v1/project_score?{query}")
        objects = found.get("objects") or []
        return objects[0] if objects else None

    async def write_project_score(
        self,
        project_handle: str,
        definition: dict[str, Any],
        *,
        update: bool,
    ) -> dict[str, Any]:
        project_id = await self._prompt_project_id(project_handle)
        return await asyncio.to_thread(
            self._request,
            "PUT" if update else "POST",
            "/v1/project_score",
            {"project_id": project_id, **definition},
        )

    async def get_view(
        self, project_handle: str, name: str, *, view_type: str
    ) -> dict[str, Any] | None:
        project_id = await self._prompt_project_id(project_handle)
        query = urllib.parse.urlencode(
            {
                "object_type": "project",
                "object_id": project_id,
                "view_type": view_type,
                "view_name": name,
            }
        )
        found = await asyncio.to_thread(self._request, "GET", f"/v1/view?{query}")
        objects = found.get("objects") or []
        return objects[0] if objects else None

    async def write_view(
        self,
        project_handle: str,
        definition: dict[str, Any],
        *,
        update: bool,
    ) -> dict[str, Any]:
        project_id = await self._prompt_project_id(project_handle)
        payload = {"object_id": project_id, **definition}
        if str(definition.get("view_type") or "") == "monitor":
            options = dict(definition.get("options") or {})
            nested = dict(options.get("options") or {})
            nested.setdefault("type", "project")
            nested["projectId"] = project_id
            payload["options"] = {**options, "viewType": "monitor", "options": nested}
        return await asyncio.to_thread(
            self._request,
            "PUT" if update else "POST",
            "/v1/view",
            payload,
        )

    async def flag_logs_for_review(self, project_handle: str, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        project_id = await self._prompt_project_id(project_handle)
        for offset in range(0, len(events), 100):
            batch = events[offset : offset + 100]
            await asyncio.to_thread(
                self._request,
                "POST",
                f"/v1/project_logs/{project_id}/insert",
                {"events": batch},
            )

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
        path: Path,
        *,
        partition_key: str | None = None,
    ) -> None:
        await self._push(dataset_id, path, partition_key)

    async def insert_experiment(
        self,
        experiment_id: str,
        path: Path,
        *,
        partition_key: str | None = None,
    ) -> None:
        await self._push(experiment_id, path, partition_key)

    async def insert_logs(
        self,
        project_id: str,
        path: Path,
        *,
        partition_key: str | None = None,
    ) -> None:
        await self._push(project_id, path, partition_key)

    async def apply_tags(self, handle: str, tags: list[str]) -> None:
        """Tag the Braintrust dataset or experiment object itself.

        Opik carries these tags on the object rather than on any row, so they have
        no event to ride along with. The object only exists once rows have been
        pushed, which is why it is resolved by name here instead of at creation.
        """
        kind, project, name = self._decode(handle)
        if not tags or kind not in TAGGABLE_KINDS:
            return
        query = urllib.parse.urlencode({"project_name": project, f"{kind}_name": name})
        found = await asyncio.to_thread(self._request, "GET", f"/v1/{kind}?{query}")
        objects = found.get("objects") or []
        if not objects:
            # Nothing was uploaded, so Braintrust has no object to tag.
            return
        await asyncio.to_thread(
            self._request,
            "PATCH",
            f"/v1/{kind}/{objects[0]['id']}",
            {"tags": tags},
        )

    def _request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        api_key = self.api_key or os.environ.get("BRAINTRUST_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Prompt, scorer, online-eval, review-score, annotation-queue, and "
                "dashboard migration need BRAINTRUST_API_KEY; `bt` login profiles "
                "do not cover direct Braintrust REST calls."
            )
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            details = error.read().decode(errors="replace").strip()
            raise RuntimeError(
                f"Braintrust {method} {path} failed with HTTP {error.code}"
                + (f": {details}" if details else "")
            ) from error

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
        source_path: Path,
        partition_key: str | None,
    ) -> None:
        kind, project, name = self._decode(handle)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        identity = f"{handle}\0{partition_key or 'single'}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        path = self.stage_dir / f"{digest}.ndjson"

        async with self.upload_slots:
            await asyncio.to_thread(source_path.replace, path)
            environment = os.environ.copy()
            if self.api_key:
                environment["BRAINTRUST_API_KEY"] = self.api_key
            command = [
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
            ]
            if self.fresh:
                command.append("--fresh")
            process = await asyncio.create_subprocess_exec(
                *command,
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
