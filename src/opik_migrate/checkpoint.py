from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Checkpoint:
    def __init__(self, path: Path, *, resume: bool = True) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "completed": [],
            "targets": {},
            "cursors": {},
            "page_sizes": {},
            "values": {},
        }
        if resume and path.exists():
            self.data = json.loads(path.read_text())
            self.data.setdefault("cursors", {})
            self.data.setdefault("page_sizes", {})
            self.data.setdefault("values", {})

    def completed(self, key: str) -> bool:
        return key in self.data["completed"]

    def mark_completed(self, key: str) -> None:
        if key not in self.data["completed"]:
            self.data["completed"].append(key)
        self.save()

    def target(self, kind: str, source_id: str) -> str | None:
        return self.data["targets"].get(kind, {}).get(source_id)

    def set_target(self, kind: str, source_id: str, target_id: str) -> None:
        self.data["targets"].setdefault(kind, {})[source_id] = target_id
        self.save()

    def cursor(self, key: str) -> int:
        return int(self.data["cursors"].get(key, 1))

    def set_cursor(self, key: str, next_page: int) -> None:
        self.data["cursors"][key] = next_page
        self.save()

    def bind_page_size(self, key: str, page_size: int) -> None:
        existing = self.data["page_sizes"].get(key)
        if existing is not None and existing != page_size and self.cursor(key) > 1:
            raise RuntimeError(
                f"Cannot resume {key!r} with page size {page_size}; "
                f"its checkpoint uses {existing}. Restore the original "
                "OPIK_MIGRATE_PAGE_SIZE or start with a fresh state directory."
            )
        self.data["page_sizes"][key] = page_size
        self.save()

    def value(self, key: str) -> Any:
        return self.data["values"].get(key)

    def set_value(self, key: str, value: Any) -> None:
        self.data["values"][key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)
