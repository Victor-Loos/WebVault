from typing import Any

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..models import CrawlConfig
from ..services.crawls import CrawlQueueFullError, enqueue_crawl
from ..state import job_store

router = APIRouter()
CRAWL_DIR = settings.crawl_dir
SCREENCAST_PORT = settings.screencast_port


@router.get("/api/jobs")
async def list_jobs() -> dict[str, Any]:
    jobs = sorted(
        job_store.load_all().values(),
        key=lambda item: item.get("started_at", ""),
        reverse=True,
    )
    return {"jobs": jobs[:20]}


async def queue_crawl(config: CrawlConfig, replace: bool = False) -> dict[str, Any]:
    try:
        return await enqueue_crawl(
            config,
            job_store,
            CRAWL_DIR,
            SCREENCAST_PORT,
            settings.allow_private_crawls,
            settings.max_queued_jobs,
            settings.profile_dir,
            replace=replace,
        )
    except CrawlQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/collections/{collection}/crawl", status_code=202)
async def rerun_collection_crawl(collection: str, replace: bool = False) -> dict[str, Any]:
    candidates = [
        job
        for job in job_store.load_all().values()
        if job.get("collection") == collection and job.get("crawl_request")
    ]
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail="No reusable crawl configuration was found for this collection.",
        )
    source = max(candidates, key=lambda job: job.get("started_at", ""))
    try:
        config = CrawlConfig.model_validate(source["crawl_request"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.collection = collection
    return await queue_crawl(config, replace=replace)


@router.post("/api/jobs/{job_id}/rerun", status_code=202)
async def rerun_job(job_id: str, replace: bool = False) -> dict[str, Any]:
    job = job_store.load_all().get(job_id)
    if not job or not job.get("crawl_request"):
        raise HTTPException(
            status_code=404, detail="The original crawl configuration is unavailable."
        )
    try:
        config = CrawlConfig.model_validate(job["crawl_request"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await queue_crawl(config, replace=replace)


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = job_store.load_all().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Crawl job not found.")
    if job.get("status") not in {"queued", "running", "uploading"}:
        raise HTTPException(status_code=409, detail="This crawl is no longer active.")
    if job.get("status") == "queued":
        job_store.update(
            job_id,
            status="cancelled",
            cancel_requested=True,
            message="Capture cancelled before it started.",
        )
        return {"job_id": job_id, "status": "cancelled"}
    job_store.update(
        job_id,
        cancel_requested=True,
        message="Cancellation requested…",
    )
    return {"job_id": job_id, "status": "cancelling"}


@router.post("/api/crawl", status_code=202)
async def start_crawl(config: CrawlConfig) -> dict[str, Any]:
    return await queue_crawl(config)
