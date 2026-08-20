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


async def test_prompt_versions_are_returned_oldest_first() -> None:
    source = object.__new__(OpikSource)
    source.page_size = 2
    source.request_options = {}

    class Prompts:
        @staticmethod
        def get_prompt_versions(prompt_id, *, page, size, request_options):
            del prompt_id, size, request_options
            pages = {
                1: {"content": [{"id": "v3"}, {"id": "v2"}], "total": 3},
                2: {"content": [{"id": "v1"}], "total": 3},
            }
            return pages[page]

    class RestClient:
        prompts = Prompts()

    class Client:
        rest_client = RestClient()

    source.client = Client()

    async def call(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    source._call = call

    versions = await source.prompt_versions("prompt-1")
    assert [version["id"] for version in versions] == ["v1", "v2", "v3"]


async def test_evaluators_page_through_automation_rule_client() -> None:
    source = object.__new__(OpikSource)
    source.page_size = 2
    source.request_options = {}
    captured = {}

    class Evaluators:
        @staticmethod
        def find_evaluators(*, project_id, page, size, request_options):
            captured.update(
                {
                    "project_id": project_id,
                    "page": page,
                    "size": size,
                    "request_options": request_options,
                }
            )
            return {"content": [{"id": "rule-1"}], "total": 1}

    class RestClient:
        automation_rule_evaluators = Evaluators()

    class Client:
        rest_client = RestClient()

    source.client = Client()

    async def call(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    source._call = call
    evaluators = await source.evaluators("project-1")
    assert [item["id"] for item in evaluators] == ["rule-1"]
    assert captured["project_id"] == "project-1"


async def test_feedback_definitions_and_annotation_queues_page() -> None:
    source = object.__new__(OpikSource)
    source.page_size = 50
    source.request_options = {}
    captured = {}

    class Feedback:
        @staticmethod
        def find_feedback_definitions(*, page, size, request_options):
            captured["feedback"] = {"page": page, "size": size}
            del request_options
            return {"content": [{"id": "def-1", "name": "Grounded"}], "total": 1}

    class Queues:
        @staticmethod
        def find_annotation_queues(*, filters, page, size, request_options):
            captured["queues"] = {"filters": filters, "page": page, "size": size}
            del request_options
            return {
                "content": [{"id": "queue-1", "name": "Hallucination backlog"}],
                "total": 1,
            }

    class Traces:
        @staticmethod
        def get_traces_by_project(**kwargs):
            captured["traces"] = kwargs
            return {"content": [{"id": "trace-9"}], "total": 1}

        @staticmethod
        def get_trace_threads(**kwargs):
            captured["threads"] = kwargs
            return {"content": [{"id": "thread-1"}], "total": 1}

    class RestClient:
        feedback_definitions = Feedback()
        annotation_queues = Queues()
        traces = Traces()

    class Client:
        rest_client = RestClient()

    source.client = Client()

    async def call(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    source._call = call
    definitions = await source.feedback_definitions()
    queues = await source.annotation_queues("project-1")
    traces = await source.annotation_queue_traces("project-1", "queue-1")
    threads = await source.annotation_queue_threads("project-1", "queue-1")
    assert [item["id"] for item in definitions] == ["def-1"]
    assert [item["id"] for item in queues] == ["queue-1"]
    assert [item["id"] for item in traces] == ["trace-9"]
    assert [item["id"] for item in threads] == ["thread-1"]
    assert "project-1" in captured["queues"]["filters"]
    assert captured["traces"]["annotation_queue_id"] == "queue-1"


async def test_dashboards_page_workspace_and_project() -> None:
    source = object.__new__(OpikSource)
    source.page_size = 50
    source.request_options = {}
    captured = []

    class Dashboards:
        @staticmethod
        def find_dashboards(*, page, size, request_options, project_id=None):
            captured.append({"page": page, "size": size, "project_id": project_id})
            del request_options
            items = [{"id": "dash-workspace", "name": "Workspace"}]
            if project_id:
                items = [{"id": "dash-project", "name": "Project", "project_id": project_id}]
            return {"content": items, "total": 1}

    class RestClient:
        dashboards = Dashboards()

    class Client:
        rest_client = RestClient()

    source.client = Client()

    async def call(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    source._call = call
    workspace = await source.dashboards()
    project = await source.dashboards("project-1")
    assert [item["id"] for item in workspace] == ["dash-workspace"]
    assert [item["id"] for item in project] == ["dash-project"]
    assert captured[0]["project_id"] is None
    assert captured[1]["project_id"] == "project-1"
    assert captured[0]["size"] == 50
