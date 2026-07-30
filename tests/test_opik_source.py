import asyncio

import pytest

from opik_to_bt.opik_source import (
    AdaptiveRequestGate,
    OpikSource,
    retry_delay,
    trace_id_range_filter,
)


async def test_page_stream_yields_pages_without_accumulating_results() -> None:
    source = object.__new__(OpikSource)
    source.page_size = 2
    source.request_options = {}
    requested = []

    def endpoint(*, page, size, request_options):
        del size, request_options
        requested.append(page)
        values = list(range((page - 1) * 2, min(page * 2, 5)))
        return {"content": values, "total": 5}

    async def call(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    source._call = call
    pages = [page async for page in source._page_stream(endpoint)]

    assert [page.number for page in pages] == [1, 2, 3]
    assert [page.items for page in pages] == [[0, 1], [2, 3], [4]]
    assert [page.total for page in pages] == [5, 5, 5]
    assert requested == [1, 2, 3]


def test_retry_delay_honors_opik_rate_limit_reset() -> None:
    error = RuntimeError("rate limited")
    error.headers = {
        "ratelimit-reset": "53",
        "opik-get-spans-remaining-limit-ttl-millis": "53907",
    }

    assert retry_delay(error, 1, jitter=False) == pytest.approx(54.157)


def test_trace_id_range_filter_bounds_uuid7_chunk() -> None:
    filters = trace_id_range_filter(
        {
            "019faf70-0022-7646-9a92-3119a9b5cba7",
            "019faf82-9f1a-705e-9a46-459d4c52fbc6",
        }
    )

    assert filters is not None
    assert "trace_id" in filters
    assert trace_id_range_filter({"custom-trace-id"}) is None


async def test_call_retries_through_shared_adaptive_gate(monkeypatch) -> None:
    class RateLimited(RuntimeError):
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = {"ratelimit-reset": "0"}

    source = object.__new__(OpikSource)
    source.retry_attempts = 3
    source.request_slots = asyncio.Semaphore(2)
    source.request_gate = AdaptiveRequestGate()
    retries = []
    source.on_retry = lambda **details: retries.append(details)
    attempts = 0

    def endpoint():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimited
        return "ok"

    monkeypatch.setattr("opik_to_bt.opik_source.retry_delay", lambda *_: 0.001)

    assert await source._call(endpoint) == "ok"
    assert attempts == 2
    assert retries[0]["attempt"] == 2


async def test_span_pages_bulk_export_omits_trace_id_filter() -> None:
    source = object.__new__(OpikSource)
    captured = {}

    class Spans:
        get_spans_by_project = object()

    class RestClient:
        spans = Spans()

    class Client:
        rest_client = RestClient()

    source.client = Client()

    async def page_stream(function, /, *args, **kwargs):
        del function, args
        captured.update(kwargs)
        if False:
            yield

    source._page_stream = page_stream
    assert [
        page
        async for page in source.span_pages(
            "project",
            start=None,
            end=None,
        )
    ] == []
    assert captured["project_name"] == "project"
    assert "trace_id" not in captured
