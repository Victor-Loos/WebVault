import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from ..models import ProfileCreateRequest
from ..state import job_store
from ..utils import slugify, validate_seed_target
from ..views import render_profiles_page

router = APIRouter()


def list_profiles():
    if not settings.profile_dir.exists():
        return []
    profiles = []
    for path in sorted(
        settings.profile_dir.glob("*.tar.gz"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        if path.is_symlink() or not path.is_file():
            continue
        profiles.append(
            {
                "id": path.name.removesuffix(".tar.gz"),
                "name": path.name.removesuffix(".tar.gz"),
                "size_bytes": path.stat().st_size,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            }
        )
    return profiles


@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request) -> HTMLResponse:
    return render_profiles_page(getattr(request.state, "csrf_token", ""))


@router.get("/api/profiles")
async def profiles_api():
    return {"profiles": await asyncio.to_thread(list_profiles)}


@router.post("/api/profiles", status_code=202)
async def create_profile(request: ProfileCreateRequest):
    try:
        login_url = await asyncio.to_thread(
            validate_seed_target,
            request.url,
            settings.allow_private_crawls,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base_name = slugify(request.name)
    if not base_name:
        raise HTTPException(status_code=400, detail="Profile name is invalid.")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    profile_id = f"{base_name}-{stamp}"
    job_id = f"profile-{profile_id}"
    job_store.save(
        job_id,
        {
            "job_type": "profile",
            "status": "queued",
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "message": "Waiting for the login profile worker.",
            "profile_id": profile_id,
            "profile_filename": f"{profile_id}.tar.gz",
            "login_url": login_url,
            "cancel_requested": False,
        },
    )
    return {"job_id": job_id, "profile_id": profile_id, "status": "queued"}
