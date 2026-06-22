from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "application_agent/data/applications.sqlite3"


class ApplyQueue:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def add_shortlist(self, job: dict[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        now = utc_now()
        url = compact(job.get("url"))
        if not url:
            raise ValueError("Job URL is required")

        payload = {
            "source": compact(job.get("source")) or "job_board",
            "title": compact(job.get("title")),
            "company": compact(job.get("company")),
            "location": compact(job.get("location")),
            "url": url,
            "category": compact(job.get("category")),
            "salary": compact(job.get("salary")),
            "posted": compact(job.get("posted")),
            "priority": int(job.get("priority") or 0),
            "notes": compact(job.get("notes")),
            "metadata": json.dumps(job.get("metadata") or {}, ensure_ascii=True),
        }

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """
                INSERT INTO shortlisted_jobs (
                    created_at, updated_at, source, title, company, location, url,
                    category, salary, posted, status, priority, notes, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'shortlisted', ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    source=excluded.source,
                    title=COALESCE(NULLIF(excluded.title, ''), shortlisted_jobs.title),
                    company=COALESCE(NULLIF(excluded.company, ''), shortlisted_jobs.company),
                    location=COALESCE(NULLIF(excluded.location, ''), shortlisted_jobs.location),
                    category=COALESCE(NULLIF(excluded.category, ''), shortlisted_jobs.category),
                    salary=COALESCE(NULLIF(excluded.salary, ''), shortlisted_jobs.salary),
                    posted=COALESCE(NULLIF(excluded.posted, ''), shortlisted_jobs.posted),
                    priority=MAX(shortlisted_jobs.priority, excluded.priority),
                    notes=COALESCE(NULLIF(excluded.notes, ''), shortlisted_jobs.notes),
                    metadata=excluded.metadata
                """,
                (
                    now,
                    now,
                    payload["source"],
                    payload["title"],
                    payload["company"],
                    payload["location"],
                    payload["url"],
                    payload["category"],
                    payload["salary"],
                    payload["posted"],
                    payload["priority"],
                    payload["notes"],
                    payload["metadata"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM shortlisted_jobs WHERE url = ?",
                (url,),
            ).fetchone()

        return row_to_job(row)

    def list_shortlist(self, status: str | None = None) -> list[dict[str, Any]]:
        self._ensure_schema()
        query = "SELECT * FROM shortlisted_jobs"
        params: list[Any] = []
        if status and status != "all":
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY priority DESC, created_at DESC"

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()

        return [row_to_job(row) for row in rows]

    def shortlisted_urls(self) -> set[str]:
        return {job["url"] for job in self.list_shortlist()}

    def update_status(self, job_id: int, status: str) -> dict[str, Any] | None:
        self._ensure_schema()
        if status not in {"shortlisted", "queued", "running", "paused", "submitted", "failed", "skipped"}:
            raise ValueError(f"Unsupported status: {status}")

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """
                UPDATE shortlisted_jobs
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, utc_now(), job_id),
            )
            row = connection.execute("SELECT * FROM shortlisted_jobs WHERE id = ?", (job_id,)).fetchone()

        return row_to_job(row) if row else None

    def log_report(
        self,
        *,
        job_id: int | None,
        url: str,
        status: str,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_schema()
        filled = int(report.get("filled") or report.get("filledCount") or 0)
        skipped = int(report.get("skipped") or report.get("skippedCount") or len(report.get("unmapped", []) or []))
        corrected = int(report.get("corrected") or report.get("correctedCount") or 0)
        confidence = float(report.get("confidence") or 0.0)

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                """
                INSERT INTO application_reports (
                    created_at, job_id, url, status, confidence, filled_count,
                    skipped_count, corrected_count, report
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    job_id,
                    compact(url),
                    status,
                    confidence,
                    filled,
                    skipped,
                    corrected,
                    json.dumps(report, ensure_ascii=True),
                ),
            )
            row = connection.execute(
                "SELECT * FROM application_reports WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()

        return row_to_report(row)

    def list_reports(self, job_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_schema()
        query = "SELECT * FROM application_reports"
        params: list[Any] = []
        if job_id is not None:
            query += " WHERE job_id = ?"
            params.append(job_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()

        return [row_to_report(row) for row in rows]

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
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
                )
                """
            )
            connection.execute(
                """
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
                )
                """
            )


def row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = parse_json(data.get("metadata"), {})
    return data


def row_to_report(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["report"] = parse_json(data.get("report"), {})
    return data


def parse_json(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def compact(value: Any) -> str:
    return str(value or "").strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
