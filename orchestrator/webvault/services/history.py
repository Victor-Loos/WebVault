from datetime import datetime
from typing import Any
from urllib.parse import unquote

from ..jobs import JobStore
from ..utils import canonical_seed_url, seed_label


def primary_seed(job: dict[str, Any]):
    value = str(job.get("primary_seed") or "").strip()
    if value:
        return value
    request = job.get("crawl_request")
    if not isinstance(request, dict):
        return ""
    seeds = request.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        return ""
    return str(seeds[0]).strip()


def site_key(job: dict[str, Any]):
    stored = str(job.get("site_key") or "").strip()
    if stored:
        return stored
    seed = primary_seed(job)
    if not seed:
        return ""
    try:
        return canonical_seed_url(seed)
    except ValueError:
        return seed


def sort_value(job: dict[str, Any]):
    return str(job.get("completed_at") or job.get("created_at") or job.get("started_at") or "")


def related_captures(store: JobStore, selected_job_id: str):
    jobs = store.load_all()
    selected = jobs.get(selected_job_id)
    if selected is None:
        return None, []
    selected_key = site_key(selected)
    history = [job for job in jobs.values() if selected_key and site_key(job) == selected_key]
    history.sort(key=sort_value, reverse=True)
    return selected, history


def library_sites(store: JobStore):
    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in store.load_all().values():
        if job.get("status") != "completed" or not job.get("archive_url"):
            continue
        key = site_key(job)
        if not key:
            continue
        grouped.setdefault(key, []).append(job)

    sites: list[dict[str, Any]] = []
    for key, captures in grouped.items():
        captures.sort(key=sort_value, reverse=True)
        latest = captures[0]
        metadata = latest.get("archive_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        seed = primary_seed(latest)
        sites.append(
            {
                "site_key": key,
                "job_id": latest["job_id"],
                "seed_url": seed,
                "title": metadata.get("title") or seed_label(seed) or key,
                "collection": latest.get("collection") or "not-defined",
                "latest_capture": latest.get("completed_at")
                or latest.get("created_at")
                or latest.get("started_at")
                or "",
                "pages_count": metadata.get("pages_count") or latest.get("pages_crawled") or 0,
                "size_bytes": latest.get("archive_size") or 0,
                "version_count": len(captures),
                "archive_url": latest.get("archive_url") or "",
                "archive_filename": latest.get("archive_filename")
                or unquote(str(latest.get("archive_url") or "").rsplit("/", 1)[-1]),
                "can_rerun": bool(latest.get("crawl_request")),
            }
        )
    sites.sort(key=lambda item: str(item["latest_capture"]), reverse=True)
    return sites


def serialize_capture(job: dict[str, Any]):
    metadata = job.get("archive_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "collection": job.get("collection"),
        "seed_url": primary_seed(job),
        "site_key": site_key(job),
        "created_at": job.get("created_at") or job.get("started_at") or "",
        "completed_at": job.get("completed_at") or "",
        "pages_crawled": job.get("pages_crawled") or 0,
        "pages_total": job.get("pages_total") or 0,
        "pages_failed": job.get("pages_failed") or 0,
        "message": job.get("message") or "",
        "interactive": bool(job.get("interactive")),
        "archive_url": job.get("archive_url") or "",
        "download_url": str(job.get("archive_url") or "").replace("/replay/", "/download/", 1),
        "archive_filename": job.get("archive_filename") or "",
        "archive_size": job.get("archive_size") or 0,
        "archive_metadata": metadata,
        "crawl_request": job.get("crawl_request") or {},
        "can_rerun": bool(job.get("crawl_request")),
    }


def parse_capture_date(value: str):
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
