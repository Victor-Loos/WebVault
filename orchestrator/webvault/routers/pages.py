import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..services.history import site_key
from ..state import job_store
from ..storage import get_archive_files
from ..views import render_collection_page, render_home_page

logger = logging.getLogger("webvault.pages")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return render_home_page(getattr(request.state, "csrf_token", ""))


@router.get("/archive/{collection}", response_class=HTMLResponse)
async def view_collection(collection: str, request: Request) -> HTMLResponse:
    try:
        files = await asyncio.to_thread(get_archive_files, collection)
    except Exception as exc:
        logger.exception("Failed to load collection %s", collection)
        raise HTTPException(status_code=503, detail="Archive storage is unavailable.") from exc
    collection_jobs = [
        job
        for job in job_store.load_all().values()
        if job.get("collection") == collection and job.get("archive_filename")
    ]
    jobs_by_filename = {
        str(job["archive_filename"]): job for job in collection_jobs if job.get("job_id")
    }
    unique_files: list[dict] = []
    grouped_files: dict[str, dict] = {}
    for file in files:
        matching_job = jobs_by_filename.get(str(file.get("name") or ""), {})
        file["job_id"] = matching_job.get("job_id", "")
        file["seed_url"] = file.get("seed_url") or matching_job.get("primary_seed", "")
        crawl_key = site_key(matching_job) if matching_job else f"imported:{file['name']}"
        representative = grouped_files.get(crawl_key)
        if representative is None:
            file["version_count"] = 1
            grouped_files[crawl_key] = file
            unique_files.append(file)
        else:
            representative["version_count"] = int(representative["version_count"]) + 1
    can_rerun = any(job.get("crawl_request") for job in collection_jobs)
    return render_collection_page(
        collection,
        files,
        unique_files=unique_files,
        can_rerun=can_rerun,
        csrf_token=getattr(request.state, "csrf_token", ""),
    )
