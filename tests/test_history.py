from pathlib import Path

from webvault.jobs import JobStore
from webvault.services.history import library_sites, related_captures, serialize_capture


def completed_job(job_id: str, completed_at: str, seed: str):
    return {
        "job_id": job_id,
        "status": "completed",
        "collection": "docs",
        "created_at": completed_at,
        "completed_at": completed_at,
        "primary_seed": seed,
        "crawl_request": {"seeds": [seed], "collection": "docs", "depth": 1},
        "archive_url": f"/replay/docs/{job_id}.wacz",
        "archive_filename": f"{job_id}.wacz",
        "archive_size": 100,
        "archive_metadata": {"title": "Documentation", "pages_count": 4},
        "pages_crawled": 4,
    }


def test_library_deduplicates_by_canonical_seed_and_keeps_history(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    store.save("old", completed_job("old", "2026-01-01T00:00:00+00:00", "https://EXAMPLE.com"))
    store.save("new", completed_job("new", "2026-02-01T00:00:00+00:00", "https://example.com/"))

    sites = library_sites(store)
    selected, history = related_captures(store, "new")

    assert len(sites) == 1
    assert sites[0]["job_id"] == "new"
    assert sites[0]["version_count"] == 2
    assert selected is not None
    assert [capture["job_id"] for capture in history] == ["new", "old"]
    assert serialize_capture(selected)["download_url"] == "/download/docs/new.wacz"


def test_different_seed_paths_remain_distinct_library_entries(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    store.save(
        "docs", completed_job("docs", "2026-01-01T00:00:00+00:00", "https://example.com/docs")
    )
    store.save(
        "news", completed_job("news", "2026-01-02T00:00:00+00:00", "https://example.com/news")
    )

    assert len(library_sites(store)) == 2
