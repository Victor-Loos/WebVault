import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from ..services.history import (
    library_sites,
    related_captures,
    serialize_capture,
    site_key,
)
from ..state import job_store
from ..storage import get_collection_payloads, get_s3_client
from ..utils import canonical_seed_url
from ..views import render_capture_page

logger = logging.getLogger("webvault.history")
router = APIRouter()


@router.get("/api/search")
async def search_archives(
    q: str = Query(min_length=2, max_length=200),
    collection: str = Query(default="", max_length=100),
):
    terms = [term.replace('"', '""') for term in q.split() if term]
    if not terms:
        return {"results": []}
    fts_query = " AND ".join(f'"{term}"*' for term in terms)
    try:
        results = await asyncio.to_thread(job_store.search_archive, fts_query, collection)
    except Exception as exc:
        logger.exception("Archive search failed")
        raise HTTPException(status_code=503, detail="Archive search is unavailable.") from exc
    jobs = job_store.load_all()
    for result in results:
        job = jobs.get(result["job_id"], {})
        result["archive_url"] = job.get("archive_url", "")
    return {"results": results}


@router.get("/api/library")
async def capture_library():
    sites = library_sites(job_store)
    known_archives = {
        (str(site.get("collection") or ""), str(site.get("archive_filename") or ""))
        for site in sites
    }
    collections = await asyncio.to_thread(get_collection_payloads)
    for collection in collections:
        unknown_files = [
            file
            for file in collection.get("files", [])
            if (collection["name"], file["name"]) not in known_archives
        ]
        if not unknown_files:
            continue
        latest = unknown_files[0]
        sites.append(
            {
                "site_key": f"legacy:{collection['name']}",
                "job_id": "",
                "seed_url": "",
                "title": collection["title"],
                "collection": collection["name"],
                "latest_capture": latest.get("last_modified") or "",
                "pages_count": 0,
                "size_bytes": sum(int(file.get("size_bytes", 0)) for file in unknown_files),
                "version_count": len(unknown_files),
                "archive_url": "",
                "legacy": True,
            }
        )
    sites.sort(key=lambda item: str(item.get("latest_capture") or ""), reverse=True)
    collection_items = [
        {
            "name": collection["name"],
            "title": collection["title"],
            "file_count": collection["file_count"],
            "size_bytes": sum(
                int(file.get("size_bytes", 0)) for file in collection.get("files", [])
            ),
            "latest_capture": collection.get("latest_capture") or "",
        }
        for collection in collections
    ]
    return {"sites": sites, "collections": collection_items}


@router.get("/api/captures/history")
async def capture_history_for_url(url: str):
    try:
        key = canonical_seed_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    matches = [
        serialize_capture(job)
        for job in job_store.load_all().values()
        if job.get("status") == "completed" and site_key(job) == key
    ]
    matches.sort(
        key=lambda job: str(job.get("completed_at") or job.get("created_at") or ""),
        reverse=True,
    )
    return {"site_key": key, "count": len(matches), "captures": matches}


@router.get("/api/captures/{job_id}")
async def capture_details_api(job_id: str):
    selected, history = related_captures(job_store, job_id)
    if selected is None:
        raise HTTPException(status_code=404, detail="Capture not found.")
    return {
        "capture": serialize_capture(selected),
        "history": [serialize_capture(job) for job in history],
    }


@router.delete("/api/captures/{job_id}")
def delete_capture(job_id: str):
    job = job_store.load_all().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Capture not found.")
    if job.get("status") in {"queued", "running", "uploading"}:
        raise HTTPException(status_code=409, detail="Cancel the active capture before deleting it.")

    collection = str(job.get("collection") or "")
    filename = str(job.get("archive_filename") or "")
    archive_key = str(job.get("archive_key") or "")
    expected_key = f"{collection}/{filename}" if collection and filename else ""
    if archive_key and archive_key != expected_key:
        raise HTTPException(status_code=409, detail="Capture archive metadata is inconsistent.")
    if expected_key:
        s3 = get_s3_client()
        try:
            s3.delete_object(Bucket=settings.garage_bucket, Key=expected_key)
            s3.delete_object(Bucket=settings.garage_bucket, Key=f"{expected_key}.metadata.json")
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Could not delete the capture archive."
            ) from exc

    if Path(job_id).name == job_id:
        (settings.crawl_dir / f"{job_id}.yaml").unlink(missing_ok=True)
        shutil.rmtree(settings.crawl_dir / "collections" / job_id, ignore_errors=True)
    job_store.delete(job_id)

    remaining = [
        candidate
        for candidate in job_store.load_all().values()
        if site_key(candidate) == site_key(job) and candidate.get("status") == "completed"
    ]
    remaining.sort(key=lambda candidate: str(candidate.get("completed_at") or ""), reverse=True)
    next_url = f"/captures/{remaining[0]['job_id']}" if remaining else "/#library"
    return {"deleted": True, "next_url": next_url}


@router.get("/captures/{job_id}", response_class=HTMLResponse)
async def capture_details_page(job_id: str, request: Request) -> HTMLResponse:
    selected, history = related_captures(job_store, job_id)
    if selected is None:
        raise HTTPException(status_code=404, detail="Capture not found.")
    return render_capture_page(
        serialize_capture(selected),
        [serialize_capture(job) for job in history],
        csrf_token=getattr(request.state, "csrf_token", ""),
    )
