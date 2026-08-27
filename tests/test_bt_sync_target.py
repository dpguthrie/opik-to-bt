import asyncio

from opik_to_bt.bt_sync_target import BtSyncTarget
from opik_to_bt.config import Settings
from opik_to_bt.tuning import RuntimeTuning


def settings() -> Settings:
    return Settings(
        braintrust_url="https://api.example.test",
        braintrust_api_key="test-key",
    )


def test_handle_survives_checkpoint_round_trip(tmp_path) -> None:
    first = BtSyncTarget(tmp_path, settings())
    project = first._handle("project_logs", "source project", "source project")
    dataset = first._handle("dataset", "source project", "golden set")

    restored = BtSyncTarget(tmp_path, settings())
    assert restored._decode(project) == (
        "project_logs",
        "source project",
        "source project",
    )
    assert restored._decode(dataset) == ("dataset", "source project", "golden set")


async def test_apply_tags_resolves_the_object_by_name_then_patches(tmp_path) -> None:
    requests = []
    target = BtSyncTarget(tmp_path, settings())

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        return {"objects": [{"id": "bt-object-1"}]} if method == "GET" else {}

    target._request = fake_request

    await target.apply_tags(target._handle("dataset", "my project", "golden set"), ["curated"])
    await target.apply_tags(target._handle("experiment", "my project", "run 1"), ["baseline"])
    # Logs have no taggable object, and an empty tag list has nothing to write.
    await target.apply_tags(target._handle("project_logs", "my project", "my project"), ["skip"])
    await target.apply_tags(target._handle("dataset", "my project", "golden set"), [])

    assert requests == [
        ("GET", "/v1/dataset?project_name=my+project&dataset_name=golden+set", None),
        ("PATCH", "/v1/dataset/bt-object-1", {"tags": ["curated"]}),
        ("GET", "/v1/experiment?project_name=my+project&experiment_name=run+1", None),
        ("PATCH", "/v1/experiment/bt-object-1", {"tags": ["baseline"]}),
    ]


async def test_apply_tags_skips_objects_that_were_never_uploaded(tmp_path) -> None:
    requests = []
    target = BtSyncTarget(tmp_path, settings())

    def fake_request(method, path, payload=None):
        requests.append(method)
        return {"objects": []}

    target._request = fake_request

    await target.apply_tags(target._handle("dataset", "project", "empty set"), ["curated"])

    assert requests == ["GET"]


async def test_prompt_writes_resolve_real_project_and_use_rest_api(tmp_path) -> None:
    requests = []
    target = BtSyncTarget(tmp_path, settings())
    project = await target.create_project("my project", "Migrated from Opik")

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        if path.startswith("/v1/project?"):
            return {"objects": []}
        if path == "/v1/project":
            return {"id": "project-1"}
        if method == "GET":
            return {"objects": []}
        return {"id": "prompt-1", "_xact_id": "xact-1", **payload}

    target._request = fake_request
    definition = {
        "name": "Greeting",
        "slug": "greeting-12345678",
        "prompt_data": {
            "prompt": {"type": "completion", "content": "Hello"},
            "template_format": "mustache",
        },
    }

    assert await target.get_prompt(project, definition["slug"]) is None
    written = await target.write_prompt(project, definition, update=False)
    await target.write_prompt(project, definition, update=True)

    assert written["id"] == "prompt-1"
    assert requests == [
        ("GET", "/v1/project?project_name=my+project", None),
        (
            "POST",
            "/v1/project",
            {"name": "my project", "description": "Migrated from Opik"},
        ),
        (
            "GET",
            "/v1/prompt?project_id=project-1&slug=greeting-12345678",
            None,
        ),
        ("POST", "/v1/prompt", {"project_id": "project-1", **definition}),
        ("PUT", "/v1/prompt", {"project_id": "project-1", **definition}),
    ]


async def test_function_and_project_score_writes_use_rest_api(tmp_path) -> None:
    requests = []
    target = BtSyncTarget(tmp_path, settings())
    project = await target.create_project("my project", None)

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        if path.startswith("/v1/project?"):
            return {"objects": []}
        if path == "/v1/project":
            return {"id": "project-1"}
        if method == "GET":
            return {"objects": []}
        kind = "fn-1" if "function" in path else "score-1"
        return {"id": kind, **(payload or {})}

    target._request = fake_request
    function = {
        "name": "Hallucination",
        "slug": "hallucination-12345678",
        "function_type": "scorer",
        "function_data": {"type": "prompt"},
        "prompt_data": {"prompt": {"type": "chat", "messages": []}},
    }
    score = {
        "name": "Hallucination",
        "score_type": "online",
        "config": {"online": {"sampling_rate": 1, "scorers": []}},
    }

    assert await target.get_function(project, function["slug"]) is None
    await target.write_function(project, function, update=False)
    await target.write_function(project, function, update=True)
    assert await target.get_project_score(project, score["name"]) is None
    await target.write_project_score(project, score, update=False)
    await target.write_project_score(project, score, update=True)

    assert ("POST", "/v1/function", {"project_id": "project-1", **function}) in requests
    assert ("PUT", "/v1/function", {"project_id": "project-1", **function}) in requests
    assert ("POST", "/v1/project_score", {"project_id": "project-1", **score}) in requests
    assert ("PUT", "/v1/project_score", {"project_id": "project-1", **score}) in requests


async def test_view_and_review_flag_writes_use_rest_api(tmp_path) -> None:
    requests = []
    target = BtSyncTarget(tmp_path, settings())
    project = await target.create_project("my project", None)

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        if path.startswith("/v1/project?"):
            return {"objects": []}
        if path == "/v1/project":
            return {"id": "project-1"}
        if method == "GET":
            return {"objects": []}
        return {"id": "view-1", **(payload or {})}

    target._request = fake_request
    view = {
        "name": "Hallucination backlog",
        "object_type": "project",
        "view_type": "for_review_project_log",
        "view_data": {"search": {"filter": ["metadata.opik_annotation_queue_id = 'queue-1'"]}},
        "options": {"layout": "kanban"},
    }
    events = [{"id": "opik:trace:trace-9", "_is_merge": True}]

    assert await target.get_view(project, view["name"], view_type=view["view_type"]) is None
    await target.write_view(project, view, update=False)
    await target.write_view(project, view, update=True)
    await target.flag_logs_for_review(project, events)

    assert (
        "POST",
        "/v1/view",
        {"object_id": "project-1", **view},
    ) in requests
    assert (
        "PUT",
        "/v1/view",
        {"object_id": "project-1", **view},
    ) in requests
    assert (
        "POST",
        "/v1/project_logs/project-1/insert",
        {"events": events},
    ) in requests


async def test_monitor_view_writes_inject_project_id(tmp_path) -> None:
    requests = []
    target = BtSyncTarget(tmp_path, settings())
    project = await target.create_project("my project", None)

    def fake_request(method, path, payload=None):
        requests.append((method, path, payload))
        if path.startswith("/v1/project?"):
            return {"objects": []}
        if path == "/v1/project":
            return {"id": "project-1"}
        if method == "GET":
            return {"objects": []}
        return {"id": "view-1", **(payload or {})}

    target._request = fake_request
    view = {
        "name": "Prod overview",
        "object_type": "project",
        "view_type": "monitor",
        "view_data": {
            "custom_charts": {
                "version": "0.0.0",
                "layout": {"type": "linear", "order": ["w-traces"]},
                "charts": {
                    "w-traces": {
                        "title": "Traces",
                        "definition": {"type": "monitorTimeseries", "measures": []},
                    }
                },
            }
        },
        "options": {
            "viewType": "monitor",
            "options": {"type": "project", "spanType": "range", "rangeValue": "7d"},
        },
    }
    await target.write_view(project, view, update=False)
    posted = next(
        payload for method, path, payload in requests if method == "POST" and path == "/v1/view"
    )
    assert posted["options"]["options"]["projectId"] == "project-1"


async def test_each_partition_gets_independent_bt_sync_state(tmp_path, monkeypatch) -> None:
    commands = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"Push complete", None

    environments = []

    async def create_process(*args, **kwargs):
        commands.append(args)
        environments.append(kwargs["env"])
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    target = BtSyncTarget(
        tmp_path,
        settings(),
        RuntimeTuning.conservative(),
        fresh=True,
    )
    dataset = target._handle("dataset", "project", "dataset")
    first = tmp_path / "first.ndjson"
    second = tmp_path / "second.ndjson"
    first.write_bytes(b'{"id":"one"}\n')
    second.write_bytes(b'{"id":"two"}\n')

    await target.insert_dataset(dataset, first, partition_key="pages:1-2")
    await target.insert_dataset(dataset, second, partition_key="pages:3-4")

    roots = [command[command.index("--root") + 1] for command in commands]
    assert len(commands) == 2
    assert len(set(roots)) == 2
    assert all("--no-input" in command for command in commands)
    assert all("--fresh" in command for command in commands)
    assert all(env["BRAINTRUST_API_KEY"] == "test-key" for env in environments)
    assert all(env is not None for env in environments)
    assert list(target.stage_dir.glob("*.ndjson")) == []
