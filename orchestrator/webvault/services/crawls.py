import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..jobs import JobStore
from ..models import CrawlConfig
from ..utils import (
    DEFAULT_GROUP,
    canonical_seed_url,
    format_timestamp,
    slugify,
    suggest_browsertrix_name,
    validate_seed_target,
)


class CrawlQueueFullError(RuntimeError):
    pass


async def enqueue_crawl(
    config: CrawlConfig,
    store: JobStore,
    crawl_dir: Path,
    screencast_port: int,
    allow_private_crawls: bool,
    max_queued_jobs: int,
    profile_dir: Path,
    replace: bool = False,
) -> dict[str, Any]:
    seeds = await asyncio.gather(
        *(
            asyncio.to_thread(
                validate_seed_target,
                seed,
                allow_private_crawls,
            )
            for seed in config.seeds
        )
    )
    seeds = [seed for seed in seeds if seed]
    if not seeds:
        raise ValueError("Please provide at least one valid seed URL.")

    active_jobs = sum(
        job.get("status") in {"queued", "running", "uploading"} for job in store.load_all().values()
    )
    if active_jobs >= max_queued_jobs:
        raise CrawlQueueFullError(
            "The crawl queue is full. Try again after an active capture finishes."
        )

    group = slugify(config.collection or "") if config.collection else DEFAULT_GROUP
    job_id = suggest_browsertrix_name(seeds)
    config_data = config.model_dump(exclude_none=True)
    profile_id = config_data.pop("profileId", None)
    interactive = bool(config_data.pop("interactive", False))
    schedule_interval = str(config_data.pop("scheduleInterval", "none"))
    retention_count = int(config_data.pop("retentionCount", 0))
    if interactive:
        config_data["debugAccessBrowser"] = True
    if profile_id:
        profile_path = profile_dir / f"{profile_id}.tar.gz"
        if (
            not profile_path.is_file()
            or profile_path.is_symlink()
            or profile_path.parent.resolve() != profile_dir.resolve()
        ):
            raise ValueError("The selected browser login profile is unavailable.")
        config_data["profile"] = f"/profiles/{profile_path.name}"
    config_data.update(collection=job_id, seeds=seeds)
    config_data["screencastPort"] = screencast_port
    config_data["text"] = config_data.get("text") or ["to-pages"]

    crawl_dir.mkdir(parents=True, exist_ok=True)
    config_file = crawl_dir / f"{job_id}.yaml"
    config_file.write_text(yaml.safe_dump(config_data, sort_keys=False))
    crawl_request = config.model_dump(exclude_none=True)
    crawl_request.update(seeds=seeds, collection=group)
    created_at = datetime.now(UTC)
    store.save(
        job_id,
        {
            "collection": group,
            "primary_seed": seeds[0],
            "site_key": canonical_seed_url(seeds[0]),
            "created_at": created_at.isoformat(),
            "status": "queued",
            "started_at": format_timestamp(created_at),
            "message": "Queued for the crawl worker.",
            "crawl_request": crawl_request,
            "pages_crawled": 0,
            "pages_total": 0,
            "pages_failed": 0,
            "live_url": "/live/",
            "replace": replace,
            "interactive": interactive,
            "cancel_requested": False,
            "schedule_id": job_id if schedule_interval != "none" else "",
            "schedule_interval": schedule_interval,
            "retention_count": retention_count,
        },
    )
    return {"job_id": job_id, "collection": group, "status": "queued"}
