"""One application ledger (roadmap Phase 2 #1).

The extension backend's Track endpoint, the dashboard's apply queue and the application
agent all record applications here, in application_agent/data/applications.sqlite3:

- shortlisted_jobs: one row per job (matched by ATS job id when known, else exact URL)
- application_events: the status history of each job
- application_reports: the agent's fill reports (unchanged)

Stdlib only and free of import side effects, so server.py can import it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER_PATH = ROOT / "application_agent/data/applications.sqlite3"
# The extension backend's old store. Its rows are copied in once; the file is never written.
LEGACY_BACKEND_DB = ROOT / "autofill_extension/generated/applications.sqlite3"

# A job's status only moves forward along this order; an earlier-stage event is still
# recorded in its history. failed/skipped are set explicitly (set_status).
STATUS_ORDER = {
    "shortlisted": 0,
    "queued": 0,
    "running": 1,
    "filling": 1,
    "filled": 2,
    "paused": 3,
    "submitted": 4,
    "confirmed": 5,
    "interview": 6,
    "rejected": 6,
}
ALL_STATUSES = set(STATUS_ORDER) | {"failed", "skipped"}
# Statuses that mean the job has only been picked, not applied to.
NOT_YET_APPLIED = {"shortlisted", "queued"}

_DEFAULT = object()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_ats_job_id(url: str) -> tuple[str | None, str | None]:
    """(ats, job id) for URLs whose ATS job id is stable, else (None, None)."""
    text = str(url or "")
    match = re.search(r"[?&]gh_jid=(\d+)", text) or re.search(r"greenhouse\.io/[^?#]*?/jobs/(\d+)", text)
    if match:
        return "greenhouse", match.group(1)
    uuid = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    match = re.search(rf"jobs\.lever\.co/[^/]+/{uuid}", text, re.I)
    if match:
        return "lever", match.group(1).lower()
    match = re.search(rf"jobs\.ashbyhq\.com/[^/]+/{uuid}", text, re.I)
    if match:
        return "ashby", match.group(1).lower()
    return None, None


class Ledger:
    def __init__(self, db_path: Path = DEFAULT_LEDGER_PATH, legacy_db: Any = _DEFAULT) -> None:
        self.db_path = Path(db_path)
        if legacy_db is _DEFAULT:
            # Only the real ledger imports the real backend store; a ledger at any other
            # path (tests, tools) never reads it.
            legacy_db = LEGACY_BACKEND_DB if self.db_path == DEFAULT_LEDGER_PATH else None
        self.legacy_db = Path(legacy_db) if legacy_db else None

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        ensure_schema(connection)
        self._import_legacy_once(connection)
        return connection

    def record(
        self,
        url: str,
        *,
        status: str,
        source: str,
        title: str | None = None,
        company: str | None = None,
        ats: str | None = None,
        ats_job_id: str | None = None,
        details: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> int:
        """Upsert the job and append a status event. Returns the job id."""
        connection = self.connect()
        try:
            with connection:
                return self._record(
                    connection, url, status=status, source=source, title=title, company=company,
                    ats=ats, ats_job_id=ats_job_id, details=details, created_at=created_at,
                )
        finally:
            connection.close()

    def set_status(self, job_id: int, status: str, *, source: str, details: dict[str, Any] | None = None) -> None:
        """Force a status (dashboard moves, failed/skipped) and record the event."""
        connection = self.connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE shortlisted_jobs SET status = ?, updated_at = ? WHERE id = ?", (status, utc_now(), job_id)
                )
                self._append_event(connection, job_id, status, source, details)
        finally:
            connection.close()

    def add_event(self, job_id: int, status: str, *, source: str, details: dict[str, Any] | None = None) -> None:
        """Record an event without changing the job's status."""
        connection = self.connect()
        try:
            with connection:
                self._append_event(connection, job_id, status, source, details)
        finally:
            connection.close()

    def events(self, job_id: int) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM application_events WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        finally:
            connection.close()
        return [{**dict(row), "details": _parse_json(row["details"], {})} for row in rows]

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Jobs that have been applied to (not just shortlisted), latest update first, in
        the shape the backend's GET /applications has always returned."""
        connection = self.connect()
        try:
            placeholders = ",".join("?" for _ in NOT_YET_APPLIED)
            rows = connection.execute(
                f"SELECT * FROM shortlisted_jobs WHERE status NOT IN ({placeholders})"
                " ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*sorted(NOT_YET_APPLIED), limit),
            ).fetchall()
            jobs = []
            for row in rows:
                events = connection.execute(
                    "SELECT source, details FROM application_events WHERE job_id = ? ORDER BY id DESC", (row["id"],)
                ).fetchall()
                counts = next(
                    (details for details in (_parse_json(event["details"], {}) for event in events) if "filledCount" in details),
                    {},
                )
                jobs.append({
                    "id": row["id"],
                    "url": row["url"],
                    "title": row["title"],
                    "status": row["status"],
                    "filled_count": int(counts.get("filledCount") or 0),
                    "mapped_count": int(counts.get("mappedCount") or 0),
                    "source": events[0]["source"] if events else row["source"],
                    "created_at": row["created_at"],
                })
        finally:
            connection.close()
        return jobs

    def _record(self, connection, url, *, status, source, title, company, ats, ats_job_id, details, created_at) -> int:
        url = str(url or "").strip()
        if not url:
            raise ValueError("Job URL is required")
        if not ats_job_id:
            # The ATS and job id parsed from the URL go together; a caller's own ATS label
            # ("generic" from page detection) must not be paired with a parsed id.
            derived_ats, derived_id = normalize_ats_job_id(url)
            if derived_id:
                ats, ats_job_id = derived_ats, derived_id
        now = created_at or utc_now()

        row = find_job(connection, url, ats, ats_job_id)

        if row is None:
            initial = status if status in ALL_STATUSES else "shortlisted"
            cursor = connection.execute(
                """
                INSERT INTO shortlisted_jobs
                    (created_at, updated_at, source, title, company, url, status, priority, metadata, ats, ats_job_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, '{}', ?, ?)
                """,
                (now, now, source, title or "", company or "", url, initial, ats, ats_job_id),
            )
            job_id = cursor.lastrowid
        else:
            job_id = row["id"]
            current = row["status"]
            moves_forward = status in STATUS_ORDER and (
                current not in STATUS_ORDER or STATUS_ORDER[status] >= STATUS_ORDER[current]
            )
            connection.execute(
                """
                UPDATE shortlisted_jobs SET
                    updated_at = ?,
                    status = ?,
                    title = COALESCE(NULLIF(title, ''), ?),
                    company = COALESCE(NULLIF(company, ''), ?),
                    ats = COALESCE(ats, ?),
                    ats_job_id = COALESCE(ats_job_id, ?)
                WHERE id = ?
                """,
                (now, status if moves_forward else current, title or "", company or "", ats, ats_job_id, job_id),
            )

        self._append_event(connection, job_id, status, source, details, created_at=now)
        return job_id

    @staticmethod
    def _append_event(connection, job_id, status, source, details, created_at=None) -> None:
        connection.execute(
            "INSERT INTO application_events (job_id, created_at, status, source, details) VALUES (?, ?, ?, ?, ?)",
            (job_id, created_at or utc_now(), status, source, json.dumps(details or {}, ensure_ascii=True, sort_keys=True)),
        )

    def _import_legacy_once(self, connection: sqlite3.Connection) -> None:
        if not self.legacy_db or not self.legacy_db.exists():
            return
        if connection.execute("SELECT 1 FROM ledger_meta WHERE key = 'legacy_backend_import'").fetchone():
            return
        legacy = sqlite3.connect(f"file:{self.legacy_db}?mode=ro", uri=True)
        legacy.row_factory = sqlite3.Row
        try:
            has_table = legacy.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'applications'"
            ).fetchone()
            rows = legacy.execute("SELECT * FROM applications ORDER BY id").fetchall() if has_table else []
        finally:
            legacy.close()
        with connection:
            for row in rows:
                if not str(row["url"] or "").strip():
                    continue  # the old Track endpoint stored empty URLs; there is no job to file them under
                self._record(
                    connection,
                    row["url"],
                    status=row["status"] or "filled",
                    source=row["source"] or "chrome_extension",
                    title=row["title"],
                    company=None,
                    ats=None,
                    ats_job_id=None,
                    details={
                        "filledCount": row["filled_count"] or 0,
                        "mappedCount": row["mapped_count"] or 0,
                        "legacyId": row["id"],
                    },
                    created_at=row["created_at"],
                )
            connection.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('legacy_backend_import', ?)",
                (json.dumps({"rows": len(rows), "from": str(self.legacy_db), "at": utc_now()}),),
            )


def find_job(connection: sqlite3.Connection, url: str, ats: str | None, ats_job_id: str | None) -> sqlite3.Row | None:
    """The job a URL belongs to: by ATS job id when known, else by exact URL."""
    if ats and ats_job_id:
        row = connection.execute(
            "SELECT * FROM shortlisted_jobs WHERE ats = ? AND ats_job_id = ? ORDER BY id LIMIT 1", (ats, ats_job_id)
        ).fetchone()
        if row is not None:
            return row
    return connection.execute("SELECT * FROM shortlisted_jobs WHERE url = ?", (url,)).fetchone()


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS shortlisted_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT NOT NULL UNIQUE,
            category TEXT,
            salary TEXT,
            posted TEXT,
            status TEXT NOT NULL DEFAULT 'shortlisted',
            priority INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS application_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            job_id INTEGER,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            filled_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            corrected_count INTEGER NOT NULL DEFAULT 0,
            report TEXT,
            FOREIGN KEY(job_id) REFERENCES shortlisted_jobs(id)
        );
        CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT,
            details TEXT,
            FOREIGN KEY(job_id) REFERENCES shortlisted_jobs(id)
        );
        CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(shortlisted_jobs)")}
    for column in ("ats", "ats_job_id"):
        if column not in columns:
            connection.execute(f"ALTER TABLE shortlisted_jobs ADD COLUMN {column} TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ats_job ON shortlisted_jobs (ats, ats_job_id)")
    connection.commit()


def _parse_json(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback
