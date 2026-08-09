import sqlite3
import time
from pathlib import Path

from webvault.sessions import SessionStore


def test_sessions_are_random_expiring_and_revocable(tmp_path: Path):
    store = SessionStore(tmp_path / "state.db")
    first_token, first_csrf = store.create()
    second_token, second_csrf = store.create()

    assert first_token != second_token
    assert first_csrf != second_csrf
    assert store.validate(first_token) == first_csrf
    assert store.validate(second_token) == second_csrf

    store.revoke(first_token)
    assert store.validate(first_token) is None
    assert store.validate(second_token) == second_csrf


def test_expired_sessions_are_rejected_and_removed(tmp_path: Path):
    database = tmp_path / "state.db"
    store = SessionStore(database)
    token, _csrf = store.create(ttl_seconds=60)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE web_sessions SET expires_at = ?",
            (time.time() - 1,),
        )

    assert store.validate(token) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 0
