import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from webvault.jobs import EXPIRED_LEASE_MESSAGE, INTERRUPTED_MESSAGE, JobStore


def test_job_store_persists_updates(tmp_path: Path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    store.save("job-1", {"status": "queued", "pages_crawled": 0})
    store.save("job-1", {"status": "completed", "pages_crawled": 2})

    jobs = JobStore(database).load_all()

    assert jobs["job-1"]["status"] == "completed"
    assert jobs["job-1"]["pages_crawled"] == 2
    assert jobs["job-1"]["job_id"] == "job-1"


def test_job_store_marks_interrupted_jobs_failed(tmp_path: Path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    store.save("job-1", {"status": "running"})

    jobs = JobStore(database).load_all(mark_interrupted=True)

    assert jobs["job-1"] == {
        "job_id": "job-1",
        "status": "failed",
        "message": INTERRUPTED_MESSAGE,
    }


def test_job_store_migrates_legacy_json(tmp_path: Path):
    legacy_path = tmp_path / "jobs.json"
    legacy_path.write_text(json.dumps({"legacy": {"status": "completed"}}))

    jobs = JobStore(tmp_path / "jobs.db", legacy_path).load_all()

    assert jobs["legacy"] == {"job_id": "legacy", "status": "completed"}
    assert not legacy_path.exists()
    assert (tmp_path / "jobs.json.migrated").exists()


def test_job_store_migrates_legacy_jobs_into_a_populated_database(tmp_path: Path):
    database = tmp_path / "jobs.db"
    existing_store = JobStore(database)
    existing_store.save("existing", {"status": "completed"})
    # Simulate a database created before the migration marker was introduced.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 0")
    legacy_path = tmp_path / "jobs.json"
    legacy_path.write_text(json.dumps({"legacy": {"status": "completed"}}))

    jobs = JobStore(database, legacy_path).load_all()

    assert set(jobs) == {"existing", "legacy"}


def test_job_store_migrates_v1_schema_idempotently(tmp_path: Path):
    database = tmp_path / "jobs.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE crawl_jobs (
                job_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO crawl_jobs (job_id, payload) VALUES (?, ?)",
            ("old", json.dumps({"status": "queued"})),
        )
        connection.execute("PRAGMA user_version = 1")

    assert JobStore(database).load_all()["old"]["status"] == "queued"
    # A separate store initializes the already-migrated database again.
    JobStore(database).load_all()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: (row[2], row[4])
            for row in connection.execute("PRAGMA table_info(crawl_jobs)").fetchall()
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        metadata = connection.execute(
            """
            SELECT lease_owner, lease_expires_at, attempts, available_at
            FROM crawl_jobs WHERE job_id = 'old'
            """
        ).fetchone()

    assert version == 2
    assert columns["lease_owner"] == ("TEXT", None)
    assert columns["lease_expires_at"] == ("REAL", None)
    assert columns["attempts"] == ("INTEGER", "0")
    assert columns["available_at"] == ("REAL", "0")
    assert metadata == (None, None, 0, 0.0)


def test_update_atomically_merges_payload_changes(tmp_path: Path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    store.save("job-1", {"status": "queued", "pages_crawled": 0})

    updated = JobStore(database).update("job-1", pages_crawled=3, current_url="https://a.test")

    assert updated == {
        "job_id": "job-1",
        "status": "queued",
        "pages_crawled": 3,
        "current_url": "https://a.test",
    }
    assert store.update("missing", status="failed") is None
    assert store.load_all()["job-1"] == updated


def test_service_heartbeat_is_shared_across_store_instances(tmp_path: Path):
    database = tmp_path / "jobs.db"
    writer = JobStore(database)
    reader = JobStore(database)

    writer.heartbeat("crawl-worker", "idle")
    heartbeat = reader.get_heartbeat("crawl-worker")

    assert heartbeat is not None
    assert heartbeat["state"] == "idle"
    assert heartbeat["last_seen_at"] > 0
    assert reader.get_heartbeat("missing") is None


def test_claim_next_is_exclusive_across_store_instances(tmp_path: Path):
    database = tmp_path / "jobs.db"
    JobStore(database).save("only-job", {"status": "queued"})

    def claim(worker_number: int):
        return JobStore(database).claim_next(f"worker-{worker_number}", 60)

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(claim, range(8)))

    successful_claims = [claim for claim in claims if claim is not None]
    assert successful_claims == [{"job_id": "only-job", "status": "running"}]

    with sqlite3.connect(database) as connection:
        lease_owner, attempts = connection.execute(
            "SELECT lease_owner, attempts FROM crawl_jobs WHERE job_id = 'only-job'"
        ).fetchone()
    assert lease_owner.startswith("worker-")
    assert attempts == 1


def test_claim_next_uses_queue_order_and_skips_jobs_not_yet_available(tmp_path: Path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    store.save("future", {"status": "queued", "available_at": time.time() + 3600})
    store.save("oldest-due", {"status": "queued"})
    store.save("newest-due", {"status": "queued"})

    first = store.claim_next("worker", 60)
    assert first is not None
    assert first["job_id"] == "oldest-due"
    assert store.release("oldest-due", "worker", "completed")

    second = store.claim_next("worker", 60)
    assert second is not None
    assert second["job_id"] == "newest-due"
    assert store.claim_next("other-worker", 60) is None


def test_renew_and_release_require_the_current_lease_owner(tmp_path: Path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    store.save("job-1", {"status": "queued", "pages_crawled": 0})
    assert store.claim_next("owner", 60) is not None

    with sqlite3.connect(database) as connection:
        original_expiry = connection.execute(
            "SELECT lease_expires_at FROM crawl_jobs WHERE job_id = 'job-1'"
        ).fetchone()[0]

    assert not store.renew_lease("job-1", "intruder", 120)
    assert not store.release("job-1", "intruder", "failed", message="stolen")
    assert store.renew_lease("job-1", "owner", 120)

    with sqlite3.connect(database) as connection:
        renewed_expiry = connection.execute(
            "SELECT lease_expires_at FROM crawl_jobs WHERE job_id = 'job-1'"
        ).fetchone()[0]
    assert renewed_expiry > original_expiry

    assert store.release("job-1", "owner", "completed", pages_crawled=4)
    assert not store.renew_lease("job-1", "owner", 60)
    assert not store.release("job-1", "owner", "failed")
    assert store.load_all()["job-1"] == {
        "job_id": "job-1",
        "status": "completed",
        "pages_crawled": 4,
    }

    with sqlite3.connect(database) as connection:
        lease = connection.execute(
            "SELECT lease_owner, lease_expires_at FROM crawl_jobs WHERE job_id = 'job-1'"
        ).fetchone()
    assert lease == (None, None)


def test_requeue_expired_recovers_active_jobs_for_another_attempt(tmp_path: Path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    store.save("expired", {"status": "queued"})
    store.save("unleased", {"status": "running"})
    assert store.claim_next("dead-worker", 60) is not None
    store.update("expired", status="uploading")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE crawl_jobs SET lease_expires_at = ? WHERE job_id = 'expired'",
            (time.time() - 1,),
        )

    assert not store.renew_lease("expired", "dead-worker", 60)
    assert not store.release("expired", "dead-worker", "completed")
    assert store.requeue_expired() == 1
    assert store.requeue_expired() == 0
    assert store.load_all()["expired"] == {
        "job_id": "expired",
        "status": "queued",
        "message": EXPIRED_LEASE_MESSAGE,
    }
    assert store.load_all()["unleased"]["status"] == "running"

    recovered = store.claim_next("replacement", 60)
    assert recovered is not None
    assert recovered["job_id"] == "expired"
    with sqlite3.connect(database) as connection:
        owner, attempts = connection.execute(
            "SELECT lease_owner, attempts FROM crawl_jobs WHERE job_id = 'expired'"
        ).fetchone()
    assert owner == "replacement"
    assert attempts == 2


def test_archive_full_text_search(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    store.replace_search_documents(
        "job-1",
        [
            {
                "collection": "docs",
                "url": "https://example.com/guide",
                "title": "Installation guide",
                "body": "Configure the archive service securely.",
            }
        ],
    )

    assert store.has_search_documents("job-1")
    results = store.search_archive('"archive"*', "docs")
    assert results[0]["job_id"] == "job-1"
    assert results[0]["url"] == "https://example.com/guide"
    assert "<mark>archive</mark>" in results[0]["snippet"]
    assert store.search_archive('"archive"*', "other") == []
