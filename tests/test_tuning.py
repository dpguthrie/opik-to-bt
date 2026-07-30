from opik_to_bt.config import Settings
from opik_to_bt.tuning import RuntimeTuning


def test_support_overrides_do_not_require_cli_flags(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPIK_TO_BT_PAGE_SIZE", "123")
    monkeypatch.setenv("OPIK_TO_BT_PARTITION_BYTES", str(32 * 1024 * 1024))
    monkeypatch.setenv("OPIK_TO_BT_RESOURCE_WORKERS", "3")
    monkeypatch.setenv("OPIK_TO_BT_UPLOAD_PROCESSES", "2")
    monkeypatch.setenv("OPIK_TO_BT_BT_WORKERS", "5")

    tuning = RuntimeTuning.detect(tmp_path, Settings())

    assert tuning.page_size == 123
    assert tuning.partition_bytes == 32 * 1024 * 1024
    assert tuning.resource_workers == 3
    assert tuning.upload_processes == 2
    assert tuning.bt_workers == 5
