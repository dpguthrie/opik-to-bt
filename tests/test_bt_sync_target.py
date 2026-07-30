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
    )
    dataset = target._handle("dataset", "project", "dataset")

    await target.insert_dataset(dataset, [{"id": "one"}], partition_key="pages:1-2")
    await target.insert_dataset(dataset, [{"id": "two"}], partition_key="pages:3-4")

    roots = [command[command.index("--root") + 1] for command in commands]
    assert len(commands) == 2
    assert len(set(roots)) == 2
    assert all("--no-input" in command for command in commands)
    assert all(env["BRAINTRUST_API_KEY"] == "test-key" for env in environments)
    assert all(env is not None for env in environments)
    assert list(target.stage_dir.glob("*.ndjson")) == []
