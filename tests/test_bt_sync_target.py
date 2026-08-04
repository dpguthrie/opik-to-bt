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
