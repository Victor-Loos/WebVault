import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = {"queued", "running", "uploading"}
LEASED_STATUSES = {"running", "uploading"}
INTERRUPTED_MESSAGE = "Orchestrator restarted before this job completed."
EXPIRED_LEASE_MESSAGE = "Worker lease expired; job returned to the queue."


class JobStore:
    def __init__(self, database_path: Path, legacy_json_path: Path | None = None):
        self.database_path = database_path
        self.legacy_json_path = legacy_json_path
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            migrated_legacy_jobs = False
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS crawl_jobs (
                        job_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version < 1:
                    migrated_legacy_jobs = self._migrate_legacy_jobs(connection)
                    connection.execute("PRAGMA user_version = 1")
                self._migrate_schema_v2(connection)
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS archive_search USING fts5(
                        job_id UNINDEXED,
                        collection UNINDEXED,
                        url,
                        title,
                        body,
                        tokenize='unicode61'
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS service_heartbeats (
                        service_name TEXT PRIMARY KEY,
                        last_seen_at REAL NOT NULL,
                        state TEXT NOT NULL
                    )
                    """
                )
                if version < 2:
                    connection.execute("PRAGMA user_version = 2")
            if migrated_legacy_jobs and self.legacy_json_path:
                migrated_path = self.legacy_json_path.with_suffix(".json.migrated")
                try:
                    self.legacy_json_path.replace(migrated_path)
                except OSError:
                    pass
            self._initialized = True

    def _migrate_schema_v2(self, connection: sqlite3.Connection):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(crawl_jobs)").fetchall()}
        migrations = {
            "lease_owner": "ALTER TABLE crawl_jobs ADD COLUMN lease_owner TEXT",
            "lease_expires_at": "ALTER TABLE crawl_jobs ADD COLUMN lease_expires_at REAL",
            "attempts": ("ALTER TABLE crawl_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
            "available_at": (
                "ALTER TABLE crawl_jobs ADD COLUMN available_at REAL NOT NULL DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS crawl_jobs_available_idx
            ON crawl_jobs (available_at, updated_at)
            """
        )

    def _migrate_legacy_jobs(self, connection: sqlite3.Connection):
        if not self.legacy_json_path or not self.legacy_json_path.exists():
            return False
        try:
            jobs = json.loads(self.legacy_json_path.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        if not isinstance(jobs, dict):
            return False
        for job_id, payload in jobs.items():
            if not isinstance(payload, dict):
                continue
            payload.setdefault("job_id", job_id)
            connection.execute(
                "INSERT OR IGNORE INTO crawl_jobs (job_id, payload) VALUES (?, ?)",
                (job_id, json.dumps(payload)),
            )
        return True

    @staticmethod
    def _load_payload(job_id: str, raw_payload: str):
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("job_id", job_id)
        return payload

    @staticmethod
    def _lease_duration(lease_seconds: float):
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
            raise TypeError("lease_seconds must be a number")
        duration = float(lease_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        return duration

    @staticmethod
    def _validate_worker(worker_id: str):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must not be empty")

    @staticmethod
    def _available_at(changes: dict[str, Any]):
        if "available_at" not in changes:
            return None
        value = changes["available_at"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("available_at must be a number")
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError("available_at must be finite")
        return timestamp

    def load_all(self, mark_interrupted: bool = False):
        self._initialize()
        with self._lock, self._connect() as connection:
            if mark_interrupted:
                connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT job_id, payload FROM crawl_jobs ORDER BY updated_at DESC"
            ).fetchall()
            jobs: dict[str, dict[str, Any]] = {}
            for job_id, raw_payload in rows:
                payload = self._load_payload(job_id, raw_payload)
                if payload is None:
                    continue
                if mark_interrupted and payload.get("status") in ACTIVE_STATUSES:
                    payload.update(status="failed", message=INTERRUPTED_MESSAGE)
                    connection.execute(
                        """
                        UPDATE crawl_jobs
                        SET payload = ?, lease_owner = NULL, lease_expires_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE job_id = ?
                        """,
                        (json.dumps(payload), job_id),
                    )
                jobs[job_id] = payload
            return jobs

    def save(self, job_id: str, payload: dict[str, Any]):
        self._initialize()
        stored = {**payload, "job_id": job_id}
        available_at = self._available_at(stored)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if available_at is None:
                connection.execute(
                    """
                    INSERT INTO crawl_jobs (job_id, payload, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(job_id) DO UPDATE SET
                        payload = excluded.payload,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (job_id, json.dumps(stored)),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO crawl_jobs (job_id, payload, updated_at, available_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        payload = excluded.payload,
                        updated_at = CURRENT_TIMESTAMP,
                        available_at = excluded.available_at
                    """,
                    (job_id, json.dumps(stored), available_at),
                )

    def update(self, job_id: str, **changes: Any):
        """Atomically merge changes into an existing job payload."""
        self._initialize()
        available_at = self._available_at(changes)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM crawl_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            payload = self._load_payload(job_id, row[0])
            if payload is None:
                return None
            payload.update(changes)
            payload["job_id"] = job_id
            if available_at is None:
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET payload = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = ?
                    """,
                    (json.dumps(payload), job_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET payload = ?, available_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = ?
                    """,
                    (json.dumps(payload), available_at, job_id),
                )
            return payload

    def claim_next(self, worker_id: str, lease_seconds: float):
        """Claim the oldest due queued job, or return None when no job is ready."""
        self._validate_worker(worker_id)
        duration = self._lease_duration(lease_seconds)
        self._initialize()
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id, payload
                FROM crawl_jobs
                WHERE COALESCE(available_at, 0) <= ?
                ORDER BY updated_at ASC, rowid ASC
                """,
                (now,),
            ).fetchall()
            for job_id, raw_payload in rows:
                payload = self._load_payload(job_id, raw_payload)
                if payload is None or payload.get("status") != "queued":
                    continue
                payload.update(job_id=job_id, status="running")
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET payload = ?, lease_owner = ?, lease_expires_at = ?,
                        attempts = COALESCE(attempts, 0) + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = ?
                    """,
                    (json.dumps(payload), worker_id, now + duration, job_id),
                )
                return payload
            return None

    def renew_lease(self, job_id: str, worker_id: str, lease_seconds: float):
        """Extend an unexpired lease held by worker_id."""
        self._validate_worker(worker_id)
        duration = self._lease_duration(lease_seconds)
        self._initialize()
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload, lease_owner, lease_expires_at
                FROM crawl_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            payload = self._load_payload(job_id, row[0])
            lease_expires_at = row[2]
            if (
                payload is None
                or payload.get("status") not in LEASED_STATUSES
                or row[1] != worker_id
                or not isinstance(lease_expires_at, (int, float))
                or lease_expires_at <= now
            ):
                return False
            connection.execute(
                "UPDATE crawl_jobs SET lease_expires_at = ? WHERE job_id = ?",
                (now + duration, job_id),
            )
            return True

    def release(self, job_id: str, worker_id: str, status: str, **changes: Any):
        """Finish or requeue a job, provided worker_id still owns its lease."""
        self._validate_worker(worker_id)
        if not isinstance(status, str) or not status:
            raise ValueError("status must not be empty")
        requested_available_at = self._available_at(changes)
        self._initialize()
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload, lease_owner, lease_expires_at, available_at
                FROM crawl_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            payload = self._load_payload(job_id, row[0])
            lease_expires_at = row[2]
            if (
                payload is None
                or payload.get("status") not in LEASED_STATUSES
                or row[1] != worker_id
                or not isinstance(lease_expires_at, (int, float))
                or lease_expires_at <= now
            ):
                return False
            payload.update(changes)
            payload.update(job_id=job_id, status=status)
            available_at = requested_available_at
            if available_at is None:
                available_at = now if status == "queued" else row[3]
            connection.execute(
                """
                UPDATE crawl_jobs
                SET payload = ?, lease_owner = NULL, lease_expires_at = NULL,
                    available_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                """,
                (json.dumps(payload), available_at, job_id),
            )
            return True

    def requeue_expired(self):
        """Make expired running or uploading jobs available for another worker."""
        self._initialize()
        now = time.time()
        requeued = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id, payload
                FROM crawl_jobs
                WHERE lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (now,),
            ).fetchall()
            for job_id, raw_payload in rows:
                payload = self._load_payload(job_id, raw_payload)
                if payload is None or payload.get("status") not in LEASED_STATUSES:
                    continue
                payload.update(status="queued", message=EXPIRED_LEASE_MESSAGE)
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET payload = ?, lease_owner = NULL, lease_expires_at = NULL,
                        available_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = ?
                    """,
                    (json.dumps(payload), now, job_id),
                )
                requeued += 1
        return requeued

    def replace_search_documents(self, job_id: str, documents: list[dict[str, str]]):
        self._initialize()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM archive_search WHERE job_id = ?", (job_id,))
            connection.executemany(
                """
                INSERT INTO archive_search (job_id, collection, url, title, body)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        job_id,
                        document.get("collection", ""),
                        document.get("url", ""),
                        document.get("title", ""),
                        document.get("body", ""),
                    )
                    for document in documents
                ],
            )

    def has_search_documents(self, job_id: str):
        self._initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM archive_search WHERE job_id = ? LIMIT 1", (job_id,)
            ).fetchone()
        return row is not None

    def search_archive(self, query: str, collection: str = "", limit: int = 50):
        self._initialize()
        with self._lock, self._connect() as connection:
            sql = """
                SELECT job_id, collection, url, title,
                       snippet(archive_search, 4, '<mark>', '</mark>', ' … ', 24),
                       bm25(archive_search)
                FROM archive_search
                WHERE archive_search MATCH ?
            """
            params: list[Any] = [query]
            if collection:
                sql += " AND collection = ?"
                params.append(collection)
            sql += " ORDER BY bm25(archive_search) LIMIT ?"
            params.append(max(1, min(limit, 200)))
            rows = connection.execute(sql, params).fetchall()
        return [
            {
                "job_id": row[0],
                "collection": row[1],
                "url": row[2],
                "title": row[3],
                "snippet": row[4],
            }
            for row in rows
        ]

    def heartbeat(self, service_name: str, state: str):
        self._initialize()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO service_heartbeats (service_name, last_seen_at, state)
                VALUES (?, ?, ?)
                ON CONFLICT(service_name) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    state = excluded.state
                """,
                (service_name, time.time(), state),
            )

    def get_heartbeat(self, service_name: str):
        self._initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT last_seen_at, state
                FROM service_heartbeats
                WHERE service_name = ?
                """,
                (service_name,),
            ).fetchone()
        if row is None:
            return None
        return {"last_seen_at": float(row[0]), "state": str(row[1])}

    def delete(self, job_id: str):
        self._initialize()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM crawl_jobs WHERE job_id = ?", (job_id,))
