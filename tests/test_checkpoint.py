import pytest

from opik_to_bt.checkpoint import Checkpoint


def test_checkpoint_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = Checkpoint(path)
    state.set_target("project", "source", "target")
    state.bind_page_size("dataset:one", 500)
    state.set_value("implicit_end", "2026-01-01T00:00:00Z")
    state.set_position("dataset:one", 42, 17)
    state.mark_completed("dataset:one")

    resumed = Checkpoint(path)
    assert resumed.target("project", "source") == "target"
    assert resumed.cursor("dataset:one") == 42
    assert resumed.offset("dataset:one") == 17
    assert resumed.value("implicit_end") == "2026-01-01T00:00:00Z"
    assert resumed.completed("dataset:one")

    with pytest.raises(RuntimeError, match="page size 100"):
        resumed.bind_page_size("dataset:one", 100)


def test_old_checkpoint_defaults_to_page_boundary(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        '{"completed":[],"targets":{},"cursors":{"logs:one":3},"page_sizes":{},"values":{}}\n'
    )

    resumed = Checkpoint(path)

    assert resumed.cursor("logs:one") == 3
    assert resumed.offset("logs:one") == 0
