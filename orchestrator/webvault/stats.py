import os
import shutil
import socket
import time
from pathlib import Path
from typing import Any

from .config import settings
from .state import job_store
from .storage import get_s3_client, list_all_wacz_keys

PROCESS_STARTED_AT = time.time()
_SIZE_UNITS = {
    "": 1,
    "K": 1_000,
    "M": 1_000_000,
    "G": 1_000_000_000,
    "T": 1_000_000_000_000,
    "KI": 1_024,
    "MI": 1_048_576,
    "GI": 1_073_741_824,
    "TI": 1_099_511_627_776,
}


def parse_capacity_bytes(value: str):
    normalized = "".join(value.upper().split()).removesuffix("B")
    for suffix in sorted(_SIZE_UNITS, key=len, reverse=True):
        if normalized.endswith(suffix):
            number = normalized[: len(normalized) - len(suffix)] if suffix else normalized
            try:
                result = int(float(number) * _SIZE_UNITS[suffix])
            except ValueError:
                break
            if result > 0:
                return result
            break
    return None


def _read_cgroup_number(path: Path):
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    if value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _memory_stats():
    current = _read_cgroup_number(Path("/sys/fs/cgroup/memory.current"))
    limit = _read_cgroup_number(Path("/sys/fs/cgroup/memory.max"))
    if current is not None:
        return current, limit

    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw_value = line.split(":", 1)
            values[key] = int(raw_value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return 0, None
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return max(total - available, 0), total or None


def _browsertrix_status(worker_state: str) -> dict[str, str]:
    try:
        socket.getaddrinfo(settings.crawler_container_name, None)
    except OSError:
        return {
            "status": "unavailable",
            "state": "unreachable",
            "detail": "Crawler container is not reachable",
        }
    return {
        "status": "available",
        "state": "active" if worker_state == "busy" else "idle",
        "detail": "Runtime detected on the internal network",
    }


def _worker_status() -> dict[str, Any]:
    heartbeat = job_store.get_heartbeat("crawl-worker")
    if not heartbeat:
        return {
            "status": "unavailable",
            "state": "unknown",
            "heartbeat_age_seconds": None,
            "detail": "No worker heartbeat",
        }
    heartbeat_age = max(int(time.time() - heartbeat["last_seen_at"]), 0)
    status = "healthy" if heartbeat_age <= 30 else "stale"
    return {
        "status": status,
        "state": heartbeat["state"],
        "heartbeat_age_seconds": heartbeat_age,
        "detail": f"Heartbeat received {heartbeat_age}s ago",
    }


def _health_summary(
    archive_status: str,
    worker_status: str,
    worker_state: str,
    browsertrix_status: str,
) -> dict[str, str]:
    if archive_status != "available":
        return {"status": "unavailable", "label": "Archive storage unavailable"}
    if worker_status != "healthy" or browsertrix_status != "available":
        return {"status": "unavailable", "label": "Capture service unavailable"}
    if worker_state == "busy":
        return {"status": "busy", "label": "Worker busy"}
    return {"status": "ready", "label": "Ready"}


def _health_payload(archive_status: str) -> dict[str, Any]:
    worker = _worker_status()
    browsertrix = _browsertrix_status(worker["state"])
    return {
        "archive": {
            "status": archive_status,
            "state": "ready" if archive_status == "available" else "unreachable",
            "detail": "Garage bucket is accessible"
            if archive_status == "available"
            else "Garage bucket is not accessible",
        },
        "worker": worker,
        "browsertrix": browsertrix,
        "summary": _health_summary(
            archive_status,
            worker["status"],
            worker["state"],
            browsertrix["status"],
        ),
    }


def get_system_health() -> dict[str, Any]:
    archive_status = "available"
    try:
        get_s3_client(timeout_seconds=2).list_objects_v2(
            Bucket=settings.garage_bucket,
            MaxKeys=1,
        )
    except Exception:
        archive_status = "unavailable"
    return {
        "generated_at": time.time(),
        **_health_payload(archive_status),
    }


def get_server_stats() -> dict[str, Any]:
    jobs = list(job_store.load_all().values())
    archive_status = "available"
    try:
        archives = list_all_wacz_keys()
    except Exception:
        archives = []
        archive_status = "unavailable"
    disk = shutil.disk_usage(settings.crawl_dir)
    memory_used, memory_limit = _memory_stats()
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except OSError:
        load_1m = load_5m = load_15m = 0.0

    health = _health_payload(archive_status)

    statuses: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1

    archive_bytes = sum(int(item.get("size", 0)) for item in archives)
    capacity_bytes = parse_capacity_bytes(settings.garage_capacity)

    return {
        "scope": "orchestrator_container",
        "generated_at": time.time(),
        "process_uptime_seconds": max(int(time.time() - PROCESS_STARTED_AT), 0),
        "cpu_count": os.cpu_count() or 1,
        "load_average": {
            "one_minute": round(load_1m, 2),
            "five_minutes": round(load_5m, 2),
            "fifteen_minutes": round(load_15m, 2),
        },
        "memory": {"used_bytes": memory_used, "limit_bytes": memory_limit},
        "disk": {
            "used_bytes": disk.used,
            "total_bytes": disk.total,
            "free_bytes": disk.free,
        },
        "archive": {
            "status": archive_status,
            "files": len(archives),
            "bytes": archive_bytes,
            "collections": len({item["group"] for item in archives}),
            "capacity_bytes": capacity_bytes,
            "remaining_bytes": max(capacity_bytes - archive_bytes, 0)
            if capacity_bytes is not None
            else None,
        },
        "jobs": {"total": len(jobs), "statuses": statuses},
        "worker": health["worker"],
        "browsertrix": health["browsertrix"],
        "health": health["summary"],
        "services": health,
    }
