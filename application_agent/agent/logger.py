from __future__ import annotations

from pathlib import Path
from typing import Any

from application_agent.agent.ledger import DEFAULT_LEDGER_PATH, Ledger


DEFAULT_DB_PATH = DEFAULT_LEDGER_PATH
# The agent's own statuses, mapped onto the ledger's; the original is kept in the event.
AGENT_STATUS_TO_LEDGER = {
    "started": "filling",
    "paused_before_submit": "paused",
    "paused_for_review": "paused",
    "max_steps_reached": "paused",
}


class ApplicationLogger:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def log(self, *, url: str, title: str, ats: str, status: str, details: dict[str, Any] | None = None) -> None:
        Ledger(self.db_path).record(
            url,
            status=AGENT_STATUS_TO_LEDGER.get(status, status),
            source="application_agent",
            title=title,
            ats=ats or None,
            details={"agentStatus": status, **(details or {})},
        )
