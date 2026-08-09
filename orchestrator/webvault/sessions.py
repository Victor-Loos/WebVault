import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SESSION_TTL_SECONDS = 60 * 60 * 24 * 30


class SessionStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
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
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS web_sessions (
                        token_hash TEXT PRIMARY KEY,
                        csrf_token TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS web_sessions_expiry_idx
                    ON web_sessions (expires_at)
                    """
                )
            self._initialized = True

    @staticmethod
    def _token_hash(token: str):
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self._initialize()
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO web_sessions
                    (token_hash, csrf_token, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (self._token_hash(token), csrf_token, now, now + ttl_seconds),
            )
        return token, csrf_token

    def validate(self, token: str):
        if not token:
            return None
        self._initialize()
        now = time.time()
        token_hash = self._token_hash(token)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT csrf_token, expires_at
                FROM web_sessions
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row[1] <= now:
                connection.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
                return None
            return str(row[0])

    def revoke(self, token: str):
        if not token:
            return
        self._initialize()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM web_sessions WHERE token_hash = ?",
                (self._token_hash(token),),
            )

    def delete_expired(self):
        self._initialize()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?", (time.time(),)
            )
            return cursor.rowcount
