from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "application_agent/data/applications.sqlite3"


class ApplicationLogger:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def log(self, *, url: str, title: str, ats: str, status: str, details: dict[str, Any] | None = None) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    ats TEXT,
                    status TEXT NOT NULL,
                    details TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO applications (created_at, url, title, ats, status, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    url,
                    title,
                    ats,
                    status,
                    json.dumps(details or {}, ensure_ascii=True),
                ),
            )
