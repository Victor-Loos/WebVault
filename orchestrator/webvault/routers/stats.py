import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from ..stats import get_server_stats, get_system_health
from ..views import mark_uncached, render_stats_page

logger = logging.getLogger("webvault.stats")
router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> HTMLResponse:
    return mark_uncached(render_stats_page(getattr(request.state, "csrf_token", "")))


@router.get("/api/health")
async def system_health(response: Response) -> dict[str, Any]:
    try:
        payload = await asyncio.to_thread(get_system_health)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return payload
    except Exception as exc:
        logger.exception("Failed to load system health")
        raise HTTPException(
            status_code=503, detail="System health is temporarily unavailable."
        ) from exc


@router.get("/api/stats")
async def server_stats(response: Response) -> dict[str, Any]:
    try:
        payload = await asyncio.to_thread(get_server_stats)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return payload
    except Exception as exc:
        logger.exception("Failed to load server statistics")
        raise HTTPException(
            status_code=503, detail="Server statistics are temporarily unavailable."
        ) from exc
