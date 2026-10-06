"""In-memory content and appeal store.

Single-process, course-project scope. Everything a database would hold lives
here behind a small interface so a SQLite swap later touches only this file.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class ContentStore:
    def __init__(self) -> None:
        self._contents: dict[str, dict] = {}
        self._appeals: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- content -------------------------------------------------------------

    def create_content(
        self,
        *,
        creator_id: str,
        text: str,
        content_type: str,
        word_count: int,
        decision: dict,
        audit_entry_id: str | None = None,
    ) -> dict:
        record = {
            "content_id": new_id("c"),
            "creator_id": creator_id,
            "content_type": content_type,
            "text": text,
            "word_count": word_count,
            "decision": decision,
            "status": "labeled",
            "appeal_id": None,
            "audit_entry_id": audit_entry_id,
            "created_at": now_iso(),
        }
        with self._lock:
            self._contents[record["content_id"]] = record
        return record

    def get_content(self, content_id: str) -> dict | None:
        return self._contents.get(content_id)

    def set_audit_entry_id(self, content_id: str, entry_id: str) -> None:
        with self._lock:
            self._contents[content_id]["audit_entry_id"] = entry_id

    # -- appeals (used from Milestone 5) -------------------------------------

    def create_appeal(
        self,
        *,
        content_id: str,
        creator_id: str,
        reasoning: str,
        evidence_url: str | None,
        original_decision: dict,
        original_entry_id: str | None,
    ) -> dict:
        appeal = {
            "appeal_id": new_id("a"),
            "content_id": content_id,
            "creator_id": creator_id,
            "reasoning": reasoning,
            "evidence_url": evidence_url,
            "original_decision": original_decision,
            "original_entry_id": original_entry_id,
            "state": "open",
            "created_at": now_iso(),
        }
        with self._lock:
            self._appeals[appeal["appeal_id"]] = appeal
            content = self._contents[content_id]
            content["status"] = "under_review"
            content["appeal_id"] = appeal["appeal_id"]
        return appeal

    def get_appeal(self, appeal_id: str) -> dict | None:
        return self._appeals.get(appeal_id)

    def open_appeals(self) -> list[dict]:
        appeals = [a for a in self._appeals.values() if a["state"] == "open"]
        return sorted(appeals, key=lambda a: a["created_at"], reverse=True)
