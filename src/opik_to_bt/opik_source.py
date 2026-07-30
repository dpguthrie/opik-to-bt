from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

from opik import Opik

from opik_to_bt.config import Settings
from opik_to_bt.pipeline import Page
from opik_to_bt.util import as_dict

RetryCallback = Callable[..., None]


def _headers(error: BaseException) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (getattr(error, "headers", None) or {}).items()
    }


def retry_delay(error: BaseException, attempt: int, *, jitter: bool = True) -> float:
    """Prefer the server's rate-limit window, then use bounded exponential backoff."""
    headers = _headers(error)
    candidates: list[float] = []
    for name in ("retry-after", "ratelimit-reset"):
        with contextlib.suppress(KeyError, ValueError):
            candidates.append(float(headers[name]))
    for name in (
        "opik-get-spans-remaining-limit-ttl-millis",
        "opik-get-traces-remaining-limit-ttl-millis",
        "opik-search-spans-remaining-limit-ttl-millis",
    ):
        with contextlib.suppress(KeyError, ValueError):
            candidates.append(float(headers[name]) / 1000)
    delay = min(120.0, max(candidates, default=min(60.0, 2 ** (attempt - 1))))
    # A small buffer avoids releasing every waiting request at the exact reset boundary.
    delay = max(0.1, delay) + 0.25
    return delay + random.uniform(0, min(1.0, delay * 0.1)) if jitter else delay


def _retryable(error: BaseException) -> bool:
    status = getattr(error, "status_code", None)
    if status is not None:
        return int(status) in {408, 425, 429} or int(status) >= 500
    return isinstance(error, (ConnectionError, OSError, TimeoutError)) or type(
        error
    ).__module__.startswith(("httpx", "httpcore"))


def trace_id_range_filter(trace_ids: set[str]) -> str | None:
    """Build Opik's efficient UUIDv7 span-scan bound for a trace chunk."""
    milliseconds = []
    for trace_id in trace_ids:
        try:
            parsed = uuid.UUID(trace_id)
        except ValueError:
            return None
        if parsed.version != 7:
            return None
        milliseconds.append(parsed.int >> 80)
    if not milliseconds:
        return None

    def floor_uuid7(value: int) -> str:
        prefix = f"{max(value, 0):012x}"
        return f"{prefix[:8]}-{prefix[8:12]}-0000-0000-000000000000"

    lower = floor_uuid7(min(milliseconds) - 1)
    upper = floor_uuid7(max(milliseconds) + 1)
    from opik.api_objects import opik_query_language

    return opik_query_language.OpikQueryLanguage.for_spans(
        f'trace_id > "{lower}" AND trace_id < "{upper}"'
    ).parsed_filters


class AdaptiveRequestGate:
    """Coordinate request pacing across all concurrent Opik resource streams."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._spacing = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request_at)
            self._next_request_at = scheduled + self._spacing
            delay = scheduled - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def throttle(self, delay: float) -> None:
        async with self._lock:
            self._next_request_at = max(self._next_request_at, time.monotonic() + delay)
            self._spacing = min(2.0, max(0.1, self._spacing * 2))

    async def success(self) -> None:
        async with self._lock:
            self._spacing = max(0.0, self._spacing * 0.9 - 0.01)


class OpikSource:
    def __init__(
        self,
        settings: Settings,
        *,
        page_size: int = 500,
        request_workers: int = 4,
        on_retry: RetryCallback | None = None,
    ) -> None:
        kwargs = {
            "host": settings.opik_url,
            "api_key": settings.opik_api_key,
            "workspace": settings.opik_workspace,
        }
        self.client = Opik(**{key: value for key, value in kwargs.items() if value})
        self.page_size = page_size
        self.retry_attempts = settings.retry_attempts
        self.request_slots = asyncio.Semaphore(request_workers)
        self.request_gate = AdaptiveRequestGate()
        self.on_retry = on_retry
        self.request_options = {
            "timeout_in_seconds": int(settings.timeout_seconds),
            # Centralize retries here so all resource workers honor the same rate-limit window.
            "max_retries": 0,
        }

    async def _call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(1, self.retry_attempts + 1):
            await self.request_gate.wait()
            try:
                async with self.request_slots:
                    result = await asyncio.to_thread(function, *args, **kwargs)
            except Exception as error:
                if not _retryable(error) or attempt >= self.retry_attempts:
                    raise
                delay = retry_delay(error, attempt)
                await self.request_gate.throttle(delay)
                if self.on_retry:
                    self.on_retry(
                        delay=delay,
                        reason=f"HTTP {getattr(error, 'status_code', 'request error')}",
                        attempt=attempt + 1,
                    )
                continue
            await self.request_gate.success()
            return result
        raise RuntimeError("unreachable")

    async def _page_stream(
        self,
        function: Any,
        /,
        *args: Any,
        start_page: int = 1,
        **kwargs: Any,
    ) -> AsyncIterator[Page]:
        page = start_page
        seen = (start_page - 1) * self.page_size
        while True:
            response = await self._call(
                function,
                *args,
                page=page,
                size=self.page_size,
                request_options=self.request_options,
                **kwargs,
            )
            raw = as_dict(response)
            content = list(raw.get("content") or raw.get("items") or [])
            if not content:
                return
            total = raw.get("total") or raw.get("total_count")
            yield Page(page, content, int(total) if total is not None else None)
            seen += len(content)
            if len(content) < self.page_size or (total is not None and seen >= int(total)):
                return
            page += 1

    async def _collect(self, pages: AsyncIterator[Page]) -> list[Any]:
        return [item async for page in pages for item in page.items]

    async def project_pages(self, *, start_page: int = 1) -> AsyncIterator[Page]:
        async for page in self._page_stream(
            self.client.rest_client.projects.find_projects,
            start_page=start_page,
        ):
            yield page

    async def projects(self) -> list[Any]:
        return await self._collect(self.project_pages())

    async def dataset_pages(self, project_id: str, *, start_page: int = 1) -> AsyncIterator[Page]:
        async for page in self._page_stream(
            self.client.rest_client.datasets.find_datasets,
            project_id=project_id,
            start_page=start_page,
        ):
            yield page

    async def datasets(self, project_id: str) -> list[Any]:
        return await self._collect(self.dataset_pages(project_id))

    async def dataset_item_pages(
        self, dataset_id: str, *, start_page: int = 1
    ) -> AsyncIterator[Page]:
        async for page in self._page_stream(
            self.client.rest_client.datasets.get_dataset_items,
            dataset_id,
            truncate=False,
            start_page=start_page,
        ):
            yield page

    async def dataset_items(self, dataset_id: str) -> list[Any]:
        return await self._collect(self.dataset_item_pages(dataset_id))

    async def experiment_pages(
        self, project_id: str, *, start_page: int = 1
    ) -> AsyncIterator[Page]:
        async for page in self._page_stream(
            self.client.rest_client.experiments.find_experiments,
            project_id=project_id,
            start_page=start_page,
        ):
            yield page

    async def experiments(self, project_id: str) -> list[Any]:
        return await self._collect(self.experiment_pages(project_id))

    async def experiment_item_pages(
        self,
        experiment_id: str,
        dataset_id: str,
        *,
        start_page: int = 1,
    ) -> AsyncIterator[Page]:
        async for page in self._page_stream(
            self.client.rest_client.datasets.find_dataset_items_with_experiment_items,
            dataset_id,
            experiment_ids=json.dumps([experiment_id]),
            truncate=False,
            start_page=start_page,
        ):
            items = []
            for dataset_item in page.items:
                dataset_raw = as_dict(dataset_item)
                dataset_data = dict(dataset_raw.get("data") or {})
                dataset_data.setdefault("id", dataset_raw.get("id"))
                for result in dataset_raw.get("experiment_items") or []:
                    result_raw = as_dict(result)
                    result_raw["dataset_item_id"] = result_raw.get(
                        "dataset_item_id"
                    ) or dataset_raw.get("id")
                    result_raw["dataset_item_data"] = dataset_data
                    items.append(result_raw)
            yield Page(page.number, items)

    async def experiment_items(
        self, experiment_id: str, dataset_id: str | None = None
    ) -> list[Any]:
        if dataset_id:
            return await self._collect(self.experiment_item_pages(experiment_id, dataset_id))
        experiment = await self._call(self.client.get_experiment_by_id, experiment_id)
        return list(await self._call(experiment.get_items, max_results=None, truncate=False))

    async def trace_pages(
        self,
        project_name: str,
        *,
        start: datetime | None,
        end: datetime | None,
        start_page: int = 1,
    ) -> AsyncIterator[Page]:
        async for page in self._page_stream(
            self.client.rest_client.traces.get_traces_by_project,
            project_name=project_name,
            from_time=start,
            to_time=end,
            truncate=False,
            start_page=start_page,
        ):
            yield page

    async def traces(
        self,
        project_name: str,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> list[Any]:
        return await self._collect(self.trace_pages(project_name, start=start, end=end))

    async def span_pages(
        self,
        project_name: str,
        *,
        trace_id: str | None = None,
        start: datetime | None,
        end: datetime | None,
        start_page: int = 1,
        filters: str | None = None,
    ) -> AsyncIterator[Page]:
        async for page in self._page_stream(
            self.client.rest_client.spans.get_spans_by_project,
            project_name=project_name,
            from_time=start,
            to_time=end,
            truncate=False,
            start_page=start_page,
            **({"trace_id": trace_id} if trace_id else {}),
            **({"filters": filters} if filters else {}),
        ):
            yield page

    async def span_pages_for_traces(
        self,
        project_name: str,
        *,
        trace_ids: set[str],
        start: datetime | None,
        end: datetime | None,
    ) -> AsyncIterator[Page]:
        filters = trace_id_range_filter(trace_ids)
        # UUIDv7 is Opik's normal ID shape. For custom IDs, retain a bounded
        # project/date scan and discard non-member trace IDs client-side.
        async for page in self.span_pages(
            project_name,
            start=None if filters else start,
            end=None if filters else end,
            filters=filters,
        ):
            yield page

    async def spans(
        self,
        project_name: str,
        *,
        trace_id: str,
        start: datetime | None,
        end: datetime | None,
    ) -> list[Any]:
        return await self._collect(
            self.span_pages(
                project_name,
                trace_id=trace_id,
                start=start,
                end=end,
            )
        )
