"""SQLite-backed job history service."""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

_DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text_preview    TEXT    NOT NULL,
    voice           TEXT    NOT NULL,
    rate            TEXT    NOT NULL,
    output_path     TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    duration_secs   REAL    NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'completed',
    error_message   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at DESC);
"""


class HistoryService:
    """
    Manages the persistent job history stored in an SQLite database.

    Thread-safety: each call opens its own short-lived connection;
    SQLite's WAL mode handles concurrent reads safely.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def add_job(self, job: Job) -> Job:
        """Insert a job and return it with its new id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs
                    (text_preview, voice, rate, output_path, created_at,
                     duration_secs, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.text_preview[:80],
                    job.voice,
                    job.rate,
                    job.output_path,
                    job.created_at.isoformat(),
                    job.duration_seconds,
                    job.status.value,
                    job.error_message,
                ),
            )
            conn.commit()
            job.id = cur.lastrowid
        return job

    def get_jobs(self, limit: int = 100) -> list[Job]:
        """Return up to *limit* most recent jobs."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def delete_job(self, job_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()

    def clear_history(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs")
            conn.commit()

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=5,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            text_preview=row["text_preview"],
            voice=row["voice"],
            rate=row["rate"],
            output_path=row["output_path"],
            created_at=datetime.fromisoformat(row["created_at"]),
            duration_seconds=row["duration_secs"],
            status=JobStatus(row["status"]),
            error_message=row["error_message"],
        )
