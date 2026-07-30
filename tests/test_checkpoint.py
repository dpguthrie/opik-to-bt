import pytest

from opik_to_bt.checkpoint import Checkpoint


def test_checkpoint_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = Checkpoint(path)
    state.set_target("project", "source", "target")
    state.bind_page_size("dataset:one", 500)
    state.set_value("implicit_end", "2026-01-01T00:00:00Z")
    state.set_cursor("dataset:one", 42)
    state.mark_completed("dataset:one")

    resumed = Checkpoint(path)
    assert resumed.target("project", "source") == "target"
    assert resumed.cursor("dataset:one") == 42
    assert resumed.value("implicit_end") == "2026-01-01T00:00:00Z"
    assert resumed.completed("dataset:one")

    with pytest.raises(RuntimeError, match="page size 100"):
        resumed.bind_page_size("dataset:one", 100)
