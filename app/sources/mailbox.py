"""Mailbox: reading facility notices, and the outbound side that needs consent.

Reading is safe in every mode. Sending is deliberately split into `preview`
(always allowed) and `execute` (only reachable from /confirm/{id}, never from a
model tool call).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models import SourceMode
from app.sources.base import Unavailable


class FixtureMailbox:
    """Reads data/mailbox/*.json; 'sends' by writing to data/outbox/."""

    name = "mailbox_fixture"
    mode = SourceMode.fixture

    def __init__(self, mailbox_dir: Path, outbox_dir: Path, allowed_ids: list[str] | None = None):
        self.mailbox_dir = mailbox_dir
        self.outbox_dir = outbox_dir
        self.allowed_ids = allowed_ids

    def read_messages(self, since: datetime | None = None) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for path in sorted(self.mailbox_dir.glob("*.json")):
            message = json.loads(path.read_text(encoding="utf-8"))
            if self.allowed_ids is not None and message.get("id") not in self.allowed_ids:
                continue
            if since is not None:
                received = message.get("received_at")
                if received and datetime.fromisoformat(received) < since:
                    continue
            messages.append(message)
        return messages

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        path = self.mailbox_dir / f"{message_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- outbound ------------------------------------------------------------
    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """No side effect. What the confirmation dialog shows."""
        return {
            "to": payload.get("to"),
            "subject": payload.get("subject"),
            "body": payload.get("body"),
            "would_send_via": self.name,
            "sent": False,
        }

    def execute(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        """Only called after explicit user confirmation. Idempotent by key."""
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        target = self.outbox_dir / f"{idempotency_key}.json"
        if target.exists():
            return {"sent": True, "duplicate": True, "path": str(target)}
        record = dict(payload)
        record["idempotency_key"] = idempotency_key
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"sent": True, "duplicate": False, "path": str(target)}


class GmailMailbox:
    """Live Gmail. Stage 5 only, read-only scope first."""

    name = "gmail_live"
    mode = SourceMode.unavailable

    def read_messages(self, since: datetime | None = None) -> list[dict[str, Any]]:
        raise Unavailable("gmail_live not implemented yet (Stage 5)")

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise Unavailable("gmail_live not implemented yet (Stage 5)")

    def execute(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        raise Unavailable("gmail_live not implemented yet (Stage 5)")
