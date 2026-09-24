"""SQLite-backed job history service."""

import logging
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

#: Bump when the schema changes, and add the step to _MIGRATIONS.
SCHEMA_VERSION = 2

_DDL = """
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

# version reached → statements that get there from the previous version.
# Additive only: an older build must still be able to read the table.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE jobs ADD COLUMN audio_secs REAL",),
}


class HistoryService:
    """
    Manages the persistent job history stored in an SQLite database.

    Thread-safety: each call opens its own short-lived connection and closes
    it again; SQLite's WAL mode handles concurrent readers safely.

    History is a convenience, never a reason to fail: if the database is
    unreadable it is set aside as ``history.db.corrupt`` and a fresh one is
    started, and if a *newer* SetupTTS created it (an old copy of the app
    launched after an upgrade) it is left untouched and history is simply
    shown read-only.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._read_only = False
        self._init_db()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    @property
    def read_only(self) -> bool:
        return self._read_only

    def add_job(self, job: Job) -> Job:
        """Insert a job and return it with its new id."""
        if self._read_only:
            return job
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs
                    (text_preview, voice, rate, output_path, created_at,
                     duration_secs, status, error_message, audio_secs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    job.audio_seconds,
                ),
            )
            job.id = cur.lastrowid
        return job

    def get_jobs(self, limit: int = 100) -> list[Job]:
        """Return up to *limit* most recent jobs."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        jobs = []
        for row in rows:
            try:
                jobs.append(self._row_to_job(row))
            except (ValueError, KeyError, TypeError):
                logger.warning("Skipping unreadable history row id=%s", row["id"])
        return jobs

    def delete_job(self, job_id: int) -> None:
        if self._read_only:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def clear_history(self) -> None:
        if self._read_only:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs")

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create_or_migrate()
        except sqlite3.DatabaseError:
            # "file is not a database", "database disk image is malformed", …
            logger.warning("History database is unreadable — starting a new one",
                           exc_info=True)
            self._set_aside_corrupt_db()
            self._create_or_migrate()

    def _create_or_migrate(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                logger.warning(
                    "History database schema v%d is newer than this build (v%d) — "
                    "opening read-only. A newer SetupTTS has been used on this "
                    "computer.", version, SCHEMA_VERSION,
                )
                self._read_only = True
                conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchall()
                return
            conn.executescript(_DDL)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
            for target in range(max(version, 1) + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS.get(target, ()):
                    # Idempotent: a column may exist if a previous attempt
                    # added it but died before user_version was written.
                    if "ADD COLUMN" in statement and statement.split()[5] in columns:
                        continue
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchall()

    def _set_aside_corrupt_db(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            src = self._db_path.with_name(self._db_path.name + suffix)
            if src.exists():
                try:
                    src.replace(src.with_name(src.name + ".corrupt"))
                except OSError:
                    src.unlink(missing_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """
        A connection that commits on success and is always *closed*.

        ``with sqlite3.connect(...)`` only commits or rolls back — it never
        closes — so every call leaked a connection (and, on Windows, a file
        handle on history.db) until garbage collection got round to it.
        """
        conn = sqlite3.connect(str(self._db_path), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with closing(conn):
            with conn:
                yield conn

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        keys = row.keys()
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
            audio_seconds=row["audio_secs"] if "audio_secs" in keys else None,
        )
