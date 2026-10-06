"""Structured audit log.

Append-only JSONL file (one JSON object per line) mirrored in memory for fast
reads from ``GET /log``. Entries are never edited or deleted. The submitted text
is deliberately not logged; the content store holds it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .store import new_id, now_iso

DEFAULT_PATH = "audit_log.jsonl"
MAX_LIMIT = 500


class AuditLog:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or os.environ.get("AUDIT_LOG_PATH", DEFAULT_PATH))
        self._entries: list[dict] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._entries.append(json.loads(line))
                except json.JSONDecodeError:
                    # A torn write from a crash should not take the service down.
                    continue

    def append(self, entry_type: str, payload: dict) -> dict:
        entry = {"entry_id": new_id("e"), "type": entry_type, "ts": now_iso(), **payload}
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._entries.append(entry)
        return entry

    def entries(self, *, limit: int = 50, entry_type: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        with self._lock:
            items = list(self._entries)
        if entry_type:
            items = [e for e in items if e.get("type") == entry_type]
        return list(reversed(items))[:limit]

    def __len__(self) -> int:
        return len(self._entries)
