import asyncio
import io
import json
import logging
import os
import re
import subprocess
import zipfile
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import boto3
import yaml
from botocore.exceptions import FlexibleChecksumError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from botocore.config import Config


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webvault")

APP_DIR = Path("/app")


def resolve_runtime_path(env_name: str, container_default: str, local_default: Path) -> Path:
    value = os.getenv(env_name)
    if value:
        return Path(value)

    container_path = Path(container_default)
    return container_path if container_path.exists() else local_default


CRAWL_DIR = resolve_runtime_path("CRAWL_OUTPUT_DIR", "/crawls", Path("/crawls"))
REPLAYWEB_DIR = resolve_runtime_path("REPLAYWEB_DIR", "/replayweb", Path("/replayweb"))

GARAGE_ENDPOINT = os.getenv("GARAGE_ENDPOINT", "http://garage:3900")
GARAGE_ACCESS_KEY = os.getenv("GARAGE_ACCESS_KEY", "")
GARAGE_SECRET_KEY = os.getenv("GARAGE_SECRET_KEY", "")

if not GARAGE_ACCESS_KEY or not GARAGE_SECRET_KEY:
    raise ValueError("Missing GARAGE_ACCESS_KEY or GARAGE_SECRET_KEY. Run setup.sh first.")

GARAGE_BUCKET = os.getenv("GARAGE_BUCKET", "archives")
CRAWLER_CONTAINER_NAME = os.getenv("CRAWLER_CONTAINER_NAME", "webvault-crawler")

WAIT_UNTIL_OPTIONS = {
    "domcontentloaded",
    "load",
    "networkidle0",
    "networkidle2",
    "load,networkidle2",
}

JOBS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="WebVault")


app.mount("/img", StaticFiles(directory=APP_DIR / "img"), name="img")


class CrawlConfig(BaseModel):
    seeds: list[str]
    collection: str | None = None
    workers: int = 1
    depth: int = 1
    waitUntil: str = "load"
    generateWACZ: bool = True
    generateCDX: bool = True
    combineWARC: bool = True
    headless: bool = True
    text: list[str] = Field(default_factory=lambda: ["to-pages"])

    @field_validator("workers")
    @classmethod
    def validate_workers(cls, value: int) -> int:
        if value < 1 or value > 10:
            raise ValueError("workers must be between 1 and 10")
        return value

    @field_validator("depth")
    @classmethod
    def validate_depth(cls, value: int) -> int:
        if value < 0 or value > 5:
            raise ValueError("depth must be between 0 and 5")
        return value

    @field_validator("waitUntil")
    @classmethod
    def validate_wait_until(cls, value: str) -> str:
        if value not in WAIT_UNTIL_OPTIONS:
            raise ValueError("unsupported waitUntil value")
        return value


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=GARAGE_ENDPOINT,
        aws_access_key_id=GARAGE_ACCESS_KEY,
        aws_secret_access_key=GARAGE_SECRET_KEY,
        region_name="garage",
        config=Config(s3={"addressing_style": "path"}),
    )


def normalize_seed_url(seed: str) -> str:
    cleaned = seed.strip()
    if not cleaned:
        return ""

    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"

    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid URL: {seed}")

    return cleaned


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def friendly_title(value: str) -> str:
    value = value.replace("_", " ").replace("-", " ").strip()
    if not value:
        return "Untitled Archive"
    return " ".join(part[:1].upper() + part[1:] for part in value.split())


def seed_label(seed_url: str) -> str:
    if not seed_url:
        return ""

    parsed = urlparse(seed_url)
    label = parsed.netloc.removeprefix("www.")
    if parsed.path and parsed.path != "/":
        trimmed_path = parsed.path.strip("/")
        if trimmed_path:
            label = f"{label}/{trimmed_path[:40]}"
    return label


DEFAULT_GROUP = "not-defined"


def suggest_browsertrix_name(seeds: list[str]) -> str:
    parsed = urlparse(seeds[0])
    host = parsed.netloc.removeprefix("www.")
    path_bits = [part for part in parsed.path.split("/") if part][:2]
    base = "-".join([host, *path_bits]) if path_bits else host
    slug = slugify(base) or "archive"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{stamp}"


def extract_base_group(wacz_path: Path) -> str:
    name = wacz_path.stem.removeprefix(".wacz").removesuffix(".wacz")
    stamp_idx = name.rfind("-")
    while stamp_idx > 0 and len(name) - stamp_idx < 7:
        stamp_idx = name.rfind("-", 0, stamp_idx)
    base = name[:stamp_idx] if stamp_idx > 0 else name
    return slugify(base) or DEFAULT_GROUP


def iter_collection_groups() -> list[str]:
    groups: set[str] = set()
    s3 = get_s3_client()
    continuation_token = None
    while True:
        params: dict[str, Any] = {"Bucket": GARAGE_BUCKET, "Delimiter": "/"}
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        result = s3.list_objects_v2(**params)
        for prefix in result.get("CommonPrefixes", []):
            group_prefix = prefix.get("Prefix", "").rstrip("/")
            if group_prefix:
                groups.add(group_prefix)
        if not result.get("IsTruncated"):
            break
        continuation_token = result.get("NextContinuationToken")
    return sorted(groups)


def list_all_wacz_keys() -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    s3 = get_s3_client()
    continuation_token = None
    while True:
        params: dict[str, Any] = {"Bucket": GARAGE_BUCKET}
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        result = s3.list_objects_v2(**params)
        for obj in result.get("Contents", []):
            key = obj.get("Key", "")
            if key.endswith(".wacz"):
                wacz_name = key.split("/")[-1]
                group = key.split("/")[0]
                keys.append({
                    "group": group,
                    "name": wacz_name,
                    "key": key,
                    "size": obj.get("Size", 0),
                    "last_modified": obj.get("LastModified"),
                })
        if not result.get("IsTruncated"):
            break
        continuation_token = result.get("NextContinuationToken")
    return keys


def format_bytes(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    if unit == 0:
        return f"{int(size)}{units[unit]}"
    return f"{size:.1f}{units[unit]}"


def format_timestamp(value: datetime | None) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def update_job(job_id: str, **changes: Any) -> None:
    job = JOBS.setdefault(job_id, {})
    job.update(changes)
    job.setdefault("job_id", job_id)


def read_jsonl_lines(raw: bytes) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            items.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return items


def extract_wacz_metadata(body: bytes, fallback_name: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "seed_url": "",
        "page_title": "",
        "pages_count": 0,
        "started_at": "",
        "duration": "",
    }

    with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
        page_files = sorted(
            name
            for name in archive.namelist()
            if name.startswith("pages/") and name.endswith(".jsonl")
        )
        total_pages = 0
        for page_file in page_files:
            entries = read_jsonl_lines(archive.read(page_file))
            total_pages += len(entries)
            if not metadata["seed_url"]:
                for entry in entries:
                    url = entry.get("url")
                    if url:
                        metadata["seed_url"] = url
                        title = entry.get("title", "").strip()
                        if title and title.lower() != url.lower():
                            metadata["page_title"] = title
                        break
        if total_pages:
            metadata["pages_count"] = total_pages

        log_files = sorted(
            name
            for name in archive.namelist()
            if "/logs/" in name or name.startswith("logs/")
        )
        timestamps: list[datetime] = []
        for log_file in log_files[:1]:
            log_content = archive.read(log_file).decode("utf-8", errors="ignore")
            if not metadata["seed_url"]:
                match = re.search(r'"url":"(https?://[^"]+)"', log_content)
                if match:
                    metadata["seed_url"] = match.group(1)

            crawl_stats = re.findall(r'"crawled":(\d+).*?"total":(\d+)', log_content)
            if crawl_stats:
                metadata["pages_count"] = int(crawl_stats[-1][1])

            for match in re.findall(r'"timestamp":"([^"]+)"', log_content):
                try:
                    timestamps.append(datetime.fromisoformat(match.replace("Z", "+00:00")))
                except ValueError:
                    continue

        if timestamps:
            metadata["started_at"] = format_timestamp(timestamps[0])
            metadata["duration"] = format_duration(
                (timestamps[-1] - timestamps[0]).total_seconds()
            )

    metadata["title"] = (
        metadata["page_title"]
        or seed_label(metadata["seed_url"])
        or friendly_title(Path(fallback_name).stem)
    )
    return metadata


def get_archive_files(collection: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    s3 = get_s3_client()
    continuation_token = None

    while True:
        params: dict[str, Any] = {
            "Bucket": GARAGE_BUCKET,
            "Prefix": f"{collection}/",
        }
        if continuation_token:
            params["ContinuationToken"] = continuation_token

        result = s3.list_objects_v2(**params)

        for obj in result.get("Contents", []):
            key = obj.get("Key", "")
            filename = key.split("/")[-1]
            if not filename.endswith(".wacz"):
                continue

            try:
                body = read_archive_bytes(collection, filename)
                metadata = extract_wacz_metadata(body, filename)
            except Exception as exc:
                logger.warning("Could not read WACZ metadata for %s: %s", key, exc)
                metadata = {
                    "title": friendly_title(Path(filename).stem),
                    "seed_url": "",
                    "page_title": "",
                    "pages_count": 0,
                    "started_at": "",
                    "duration": "",
                }

            description_bits = [bit for bit in [metadata["started_at"]] if bit]
            if metadata["pages_count"]:
                description_bits.append(f'{metadata["pages_count"]} pages')
            if metadata["duration"]:
                description_bits.append(metadata["duration"])
            if metadata["seed_url"]:
                description_bits.append(seed_label(metadata["seed_url"]))

            last_modified = obj.get("LastModified")
            files.append(
                {
                    "collection": collection,
                    "name": filename,
                    "title": metadata["title"],
                    "description": " • ".join(description_bits),
                    "size": format_bytes(obj.get("Size", 0)),
                    "size_bytes": obj.get("Size", 0),
                    "seed_url": metadata["seed_url"],
                    "pages_count": metadata["pages_count"],
                    "last_modified": last_modified.isoformat() if last_modified else "",
                }
            )

        if not result.get("IsTruncated"):
            break

        continuation_token = result.get("NextContinuationToken")

    files.sort(key=lambda item: item.get("last_modified", ""), reverse=True)
    return files


def get_collection_payloads() -> list[dict[str, Any]]:
    collections_dict: dict[str, dict[str, Any]] = {}
    for wacz in list_all_wacz_keys():
        group = wacz["group"]
        if group not in collections_dict:
            collections_dict[group] = {
                "name": group,
                "title": friendly_title(group),
                "label": friendly_title(group),
                "files": [],
                "total_size": 0,
            }
        collections_dict[group]["files"].append({
            "collection": group,
            "name": wacz["name"],
            "title": wacz["name"].removesuffix(".wacz").replace("_", " ").replace("-", " ").title(),
            "description": "",
            "size": format_bytes(wacz["size"]),
            "size_bytes": wacz["size"],
            "seed_url": "",
            "pages_count": 0,
            "last_modified": wacz["last_modified"].isoformat() if wacz["last_modified"] else "",
        })
        collections_dict[group]["total_size"] += wacz["size"]

    collections: list[dict[str, Any]] = []
    for coll in collections_dict.values():
        files = sorted(coll["files"], key=lambda f: f.get("last_modified", ""), reverse=True)
        latest = files[0] if files else {}
        collections.append({
            "name": coll["name"],
            "title": friendly_title(coll["name"]),
            "label": coll["label"],
            "file_count": len(files),
            "total_size": format_bytes(coll["total_size"]),
            "latest_capture": latest.get("description", ""),
            "files": files,
        })

    collections.sort(
        key=lambda item: item["files"][0].get("last_modified", "") if item["files"] else "",
        reverse=True,
    )
    return collections


def render_layout(title: str, body: str, extra_head: str = "", extra_script: str = "") -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <link rel="icon" type="image/svg+xml" href="/img/icon.svg">
    <style>
        :root {{
            --bg: #f5efe3;
            --panel: rgba(255, 251, 244, 0.88);
            --panel-strong: #fffdf8;
            --text: #211c17;
            --muted: #675c50;
            --line: rgba(69, 52, 37, 0.14);
            --accent: #c45c2c;
            --accent-strong: #9c4219;
            --accent-soft: rgba(196, 92, 44, 0.14);
            --secondary: #134e4a;
            --shadow: 0 24px 70px rgba(50, 30, 14, 0.12);
            --radius: 10px;
            --top-accent: linear-gradient(90deg, var(--accent), var(--secondary));
        }}
        * {{
            box-sizing: border-box;
        }}
        html {{
            background:
                radial-gradient(circle at top left, rgba(196, 92, 44, 0.14), transparent 34rem),
                radial-gradient(circle at top right, rgba(19, 78, 74, 0.10), transparent 24rem),
                linear-gradient(180deg, #fbf5eb 0%, #f2e8d9 100%);
            min-height: 100%;
        }}
        body {{
            margin: 0;
            color: var(--text);
            font-family: ui-rounded, "Avenir Next", "Trebuchet MS", "Segoe UI", sans-serif;
            min-height: 100vh;
        }}
        a {{
            color: inherit;
        }}
        .shell {{
            width: min(1180px, calc(100% - 32px));
            margin: 0 auto;
            padding: 24px 0 48px;
        }}
        .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 22px;
        }}
        .brand {{
            display: flex;
            align-items: center;
            gap: 12px;
            text-decoration: none;
        }}
.brand-mark {{
            width: 420px;
            height: 70px;
            flex-shrink: 0;
        }}
        .brand-mark img {{
            width: 420px;
            height: 70px;
            display: block;
        }}
        .brand-copy {{
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .brand-copy strong {{
            font-size: 1rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }}
        .brand-copy span {{
            color: var(--muted);
            font-size: 0.9rem;
        }}
        .nav {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }}
        .nav-link,
        .button,
        button {{
            appearance: none;
            border: none;
            border-radius: 999px;
            background: var(--panel-strong);
            color: var(--text);
            cursor: pointer;
            font: inherit;
            text-decoration: none;
            transition: transform 160ms ease, box-shadow 160ms ease, background 160ms ease;
        }}
        .nav-link,
        .button {{
            padding: 11px 16px;
            box-shadow: 0 10px 24px rgba(44, 23, 8, 0.08);
        }}
        .nav-link:hover,
        .button:hover,
        button:hover {{
            transform: translateY(-1px);
            box-shadow: 0 14px 28px rgba(44, 23, 8, 0.12);
        }}
        .button.primary,
        button.primary {{
            background: linear-gradient(135deg, var(--accent), var(--accent-strong));
            color: white;
        }}
        .button.secondary {{
            background: rgba(19, 78, 74, 0.10);
            color: var(--secondary);
        }}
        .hero {{
            padding: 32px 32px 28px;
            margin-bottom: 14px;
            position: relative;
            background: linear-gradient(
                135deg,
                rgba(196, 92, 44, 0.10) 0%,
                transparent 55%
            );
            border-radius: 0;
            border: none;
            box-shadow: none;
            backdrop-filter: none;
        }}
        .hero::before {{
            content: "";
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 3px;
            background: var(--top-accent);
        }}
        .hero::after {{
            content: "";
            position: absolute;
            bottom: 0; left: 0;
            width: 120px;
            height: 120px;
            background: radial-gradient(
                circle at bottom left,
                rgba(245, 239, 227, 0.50) 0%,
                rgba(245, 239, 227, 0.20) 40%,
                transparent 70%
            );
            pointer-events: none;
        }}
        .hero h1 {{
            margin: 0 0 12px;
            font-size: clamp(2rem, 5vw, 3.8rem);
            line-height: 0.96;
            letter-spacing: -0.05em;
            max-width: 11ch;
        }}
        .hero p {{
            margin: 0;
            max-width: 56rem;
            color: var(--muted);
            font-size: 1.02rem;
            line-height: 1.6;
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px;
            margin-top: 26px;
        }}
        .stat {{
            padding: 18px;
            border-radius: 18px;
            background: rgba(255,255,255,0.55);
            border: 1px solid rgba(69, 52, 37, 0.08);
        }}
        .stat strong {{
            display: block;
            font-size: 1.9rem;
            margin-bottom: 6px;
        }}
        .stat span {{
            color: var(--muted);
        }}
        .tab-strip {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 16px;
        }}
        .tab-button {{
            padding: 11px 16px;
            border-radius: 999px;
            background: rgba(255,255,255,0.58);
            border: 1px solid rgba(69, 52, 37, 0.08);
            color: var(--muted);
        }}
        .tab-button.active {{
            background: var(--accent-soft);
            color: var(--accent-strong);
            border-color: rgba(196, 92, 44, 0.16);
        }}
        .grid {{
            display: grid;
            gap: 22px;
        }}
        .two-up {{
            grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.75fr);
        }}
        .panel {{
            background: rgba(255, 251, 244, 0.90);
            backdrop-filter: blur(16px);
            border: none;
            border-radius: var(--radius);
            box-shadow:
                var(--shadow),
                inset 0 0 0 1px rgba(196, 92, 44, 0.12);
            padding: 24px;
        }}
        .two-up > .panel:first-of-type {{
            background: rgba(255, 251, 244, 0.90);
            box-shadow:
                var(--shadow),
                inset 0 0 0 1px rgba(196, 92, 44, 0.12),
                inset 4px 0 24px rgba(196, 92, 44, 0.06);
        }}
        .two-up > .panel:last-of-type {{
            background: rgba(255, 251, 244, 0.90);
            box-shadow:
                var(--shadow),
                inset 0 0 0 1px rgba(196, 92, 44, 0.12),
                inset 4px 0 24px rgba(19, 78, 74, 0.06);
        }}
        .panel h2,
        .panel h3 {{
            margin: 0 0 12px;
            letter-spacing: -0.03em;
        }}
        .panel-subtitle {{
            margin: 0 0 18px;
            color: var(--muted);
            line-height: 1.5;
        }}
        .stack {{
            display: grid;
            gap: 14px;
        }}
        label {{
            display: block;
            font-size: 0.92rem;
            font-weight: 700;
            margin-bottom: 8px;
        }}
        .hint {{
            font-size: 0.88rem;
            color: var(--muted);
            margin-top: 8px;
        }}
        input,
        textarea,
        select {{
            width: 100%;
            border: 1px solid var(--line);
            border-radius: 18px;
            background: rgba(255,255,255,0.88);
            color: var(--text);
            font: inherit;
            padding: 14px 16px;
            outline: none;
            transition: border-color 160ms ease, box-shadow 160ms ease;
        }}
        input:focus,
        textarea:focus,
        select:focus {{
            border-color: rgba(196, 92, 44, 0.45);
            box-shadow: 0 0 0 4px rgba(196, 92, 44, 0.12);
        }}
        textarea {{
            min-height: 132px;
            resize: vertical;
        }}
        .field-grid {{
            display: grid;
            gap: 14px;
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }}
        .status-banner {{
            display: none;
            padding: 14px 16px;
            border-radius: 18px;
            background: rgba(19, 78, 74, 0.10);
            color: var(--secondary);
        }}
        .status-banner.error {{
            background: rgba(163, 34, 34, 0.10);
            color: #991b1b;
        }}
        .job-list,
        .archive-list,
        .file-list {{
            display: grid;
            gap: 14px;
        }}
        .job-item,
        .archive-item,
        .file-item {{
            padding: 18px;
            border-radius: 20px;
            border: 1px solid rgba(69, 52, 37, 0.08);
            background: rgba(255,255,255,0.62);
        }}
        .job-item strong,
        .archive-item strong,
        .file-item strong {{
            display: block;
            font-size: 1.02rem;
            margin-bottom: 6px;
        }}
        .meta {{
            color: var(--muted);
            font-size: 0.92rem;
            line-height: 1.5;
        }}
        .badge-row,
        .action-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 14px;
        }}
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(19, 78, 74, 0.10);
            color: var(--secondary);
            font-size: 0.86rem;
            font-weight: 700;
        }}
        .badge.running {{
            background: rgba(196, 92, 44, 0.12);
            color: var(--accent-strong);
        }}
        .badge.error {{
            background: rgba(163, 34, 34, 0.10);
            color: #991b1b;
        }}
        .empty {{
            padding: 24px;
            border-radius: 20px;
            background: rgba(255,255,255,0.55);
            color: var(--muted);
            text-align: center;
            border: 1px dashed rgba(69, 52, 37, 0.14);
        }}
        .modal {{
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.45);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }}
        .modal-content {{
            background: var(--panel-strong);
            border-radius: 20px;
            padding: 24px;
            min-width: 400px;
            max-width: 500px;
            box-shadow: 0 24px 64px rgba(0,0,0,0.2);
            color: var(--text);
        }}
        .modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        .modal-header h3 {{
            margin: 0;
            font-size: 1.2rem;
        }}
        .modal-close {{
            background: none;
            border: none;
            font-size: 1.5rem;
            cursor: pointer;
            color: var(--muted);
        }}
        .modal-close:hover {{
            color: var(--text);
        }}
        .modal-filename {{
            background: var(--line);
            padding: 10px 14px;
            border-radius: 10px;
            font-size: 0.9rem;
            word-break: break-all;
            margin-bottom: 20px;
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        .form-group label {{
            display: block;
            margin-bottom: 6px;
            font-weight: 600;
        }}
        .form-group input {{
            width: 100%;
            padding: 10px 14px;
            border: 1px solid rgba(69,52,37,0.2);
            border-radius: 10px;
            font-size: 1rem;
            box-sizing: border-box;
        }}
        .modal-footer {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }}
        .inline-link {{
            color: var(--accent-strong);
            text-decoration: none;
        }}
        .inline-link:hover {{
            text-decoration: underline;
        }}
        iframe.replay-shell {{
            width: 100%;
            min-height: calc(100vh - 220px);
            border: none;
            border-radius: 18px;
            background: white;
            box-shadow: 0 18px 40px rgba(38, 20, 8, 0.12);
        }}
        replay-web-page {{
            display: block;
            width: 100vw;
            height: 100vh;
        }}
        .panel.replay-panel {{
            padding: 12px;
        }}
        @media (max-width: 920px) {{
            .two-up,
            .field-grid,
            .stats {{
                grid-template-columns: 1fr;
            }}
            .topbar {{
                align-items: flex-start;
                flex-direction: column;
            }}
            .shell {{
                width: min(100% - 20px, 1180px);
            }}
            .hero,
            .panel {{
                padding: 20px;
            }}
        }}
        @media (prefers-color-scheme: dark) {{
            :root {{
                --bg: #1a1a1a;
                --panel: rgba(30, 30, 30, 0.92);
                --panel-strong: #2a2a2a;
                --text: #e8e6e3;
                --muted: #9a958e;
                --line: rgba(232, 230, 227, 0.12);
                --accent: #e07848;
                --accent-strong: #c45c2c;
                --accent-soft: rgba(224, 120, 72, 0.18);
--secondary: #5eead4;
                --top-accent: linear-gradient(90deg, #e07848, #5eead4);
            }}
            html {{
                background:
                    radial-gradient(circle at top left, rgba(224, 120, 72, 0.12), transparent 34rem),
                    radial-gradient(circle at top right, rgba(94, 234, 212, 0.08), transparent 24rem),
                    linear-gradient(180deg, #1e1e1e 0%, #141414 100%);
            }}
            .brand-mark {{
            }}
            .stat {{
                background: rgba(42, 42, 42, 0.60);
                border-color: rgba(232, 230, 227, 0.10);
            }}
            .stat strong {{
                color: var(--text);
            }}
            .tab-button {{
                background: rgba(42, 42, 42, 0.60);
                border-color: rgba(232, 230, 227, 0.10);
                color: var(--muted);
            }}
            .panel {{
                background: rgba(30, 30, 30, 0.92);
                box-shadow: var(--shadow), inset 0 0 0 1px rgba(224, 120, 72, 0.15);
            }}
            .two-up > .panel:first-of-type {{
                background: rgba(30, 30, 30, 0.92);
                box-shadow: var(--shadow), inset 0 0 0 1px rgba(224, 120, 72, 0.15), inset 4px 0 24px rgba(224, 120, 72, 0.08);
            }}
            .two-up > .panel:last-of-type {{
                background: rgba(30, 30, 30, 0.92);
                box-shadow: var(--shadow), inset 0 0 0 1px rgba(224, 120, 72, 0.15), inset 4px 0 24px rgba(94, 234, 212, 0.08);
            }}
            input,
            textarea,
            select {{
                background: rgba(42, 42, 42, 0.80);
                border-color: rgba(232, 230, 227, 0.15);
                color: var(--text);
            }}
            .job-item,
            .archive-item,
            .file-item {{
                background: rgba(42, 42, 42, 0.60);
                border-color: rgba(232, 230, 227, 0.10);
            }}
            .empty {{
                background: rgba(42, 42, 42, 0.50);
                border-color: rgba(232, 230, 227, 0.08);
                color: var(--muted);
            }}
            .nav-link,
            .button,
            button {{
                background: var(--panel-strong);
                color: var(--text);
            }}
            .hero::after {{
                background: radial-gradient(
                    circle at bottom left,
                    rgba(30, 30, 30, 0.60) 0%,
                    rgba(30, 30, 30, 0.25) 40%,
                    transparent 70%
                );
            }}
        }}
    </style>
    {extra_head}
</head>
<body>
    {body}
    {extra_script}
</body>
</html>"""
    return HTMLResponse(content=html)


def render_home_page() -> HTMLResponse:
    body = """
    <div class="shell">
        <div class="topbar">
            <a class="brand" href="/">
                <img class="brand-mark" src="/img/webvault.svg" alt="WebVault logo" aria-hidden="true">
            </a>
            <div class="nav"></div>
        </div>

        <section class="hero">
            <h1>Archive the modern web.</h1>
            <p>
                WebVault drives Browsertrix crawls, stores WACZ files in Garage, and opens them directly in ReplayWeb.
            </p>
            
            <div class="stats">
                <div class="stat">
                    <strong id="statCollections">0</strong>
                    <span>Collections</span>
                </div>
                <div class="stat">
                    <strong id="statFiles">0</strong>
                    <span>WACZ files</span>
                </div>
                <div class="stat">
                    <strong id="statJobs">0</strong>
                    <span>Recent jobs</span>
                </div>
            </div>
        </section>

        <div class="tab-strip">
            <button class="tab-button active" data-tab-target="crawl" type="button">Capture</button>
            <button class="tab-button" data-tab-target="archives" type="button">Replay</button>
            <button class="tab-button" data-tab-target="about" type="button">Settings</button>
        </div>

        <section class="grid two-up tab-panel" id="panel-crawl">
            <article class="panel">
                <h2>Start a fresh crawl</h2>
                <p class="panel-subtitle">
                    Enter one or more seed URLs. If you leave the collection field empty, WebVault will generate a clearer slug from the first seed plus a timestamp.
                </p>
                <div class="status-banner" id="crawlStatus"></div>
                <form id="crawlForm" class="stack">
                    <div>
                        <label for="seeds">Seed URLs</label>
                        <textarea id="seeds" placeholder="https://example.com&#10;https://example.com/docs"></textarea>
                        <div class="hint">One URL per line. Plain domains are okay too; they will be normalized to HTTPS.</div>
                    </div>
                    <div>
                        <label for="collection">Collection</label>
                        <input id="collection" type="text" list="collectionList" placeholder="Leave blank for 'not-defined'">
                        <datalist id="collectionList"></datalist>
                        <div class="hint" id="collectionHint">Type a new name to create, or select existing. Creates in 'not-defined' if blank.</div>
                    </div>
                    <div class="field-grid">
                        <div>
                            <label for="workers">Workers</label>
                            <input id="workers" type="number" min="1" max="10" value="1">
                        </div>
                        <div>
                            <label for="depth">Depth</label>
                            <input id="depth" type="number" min="0" max="5" value="1">
                        </div>
                        <div>
                            <label for="waitUntil">Wait until</label>
                            <select id="waitUntil">
                                <option value="domcontentloaded">DOM content loaded</option>
                                <option value="load" selected>Load</option>
                                <option value="networkidle0">Network idle</option>
                                <option value="networkidle2">Network idle (2 requests)</option>
                            </select>
                        </div>
                    </div>
                    <div class="action-row">
                        <button class="button primary" id="crawlSubmit" type="submit">Start crawl</button>
                    </div>
                </form>
            </article>

            <aside class="panel">
                <h3>Recent jobs</h3>
                <p class="panel-subtitle">
                    Crawler runs execute inside the dedicated <code>webvault-crawler</code> container and upload completed WACZ files into Garage when finished.
                </p>
                <div class="job-list" id="jobList">
                    <div class="empty">No crawl jobs yet.</div>
                </div>
            </aside>
        </section>

        <section class="panel tab-panel" id="panel-archives" style="display:none;">
            <h2>Collections</h2>
            <p class="panel-subtitle">
                All archive collections with their crawl files.
            </p>
            <div class="archive-list" id="archiveList">
                <div class="empty">Loading archives…</div>
            </div>
        </section>

        <section class="panel tab-panel" id="panel-about" style="display:none;">
            <h2>Collections</h2>
            <p class="panel-subtitle">Manage your archive collections. Rename to reorganize or delete to remove permanently.</p>
            <div class="collection-list" id="collectionListPanel">
                <div class="empty">Loading collections…</div>
            </div>
        </section>

        <div id="modifyModal" class="modal" style="display:none;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>Modify File</h3>
                    <button class="modal-close" onclick="closeModifyModal()">&times;</button>
                </div>
                <div class="modal-body">
                    <p class="modal-filename" id="modalFilename"></p>
                    <div class="form-group">
                        <label for="newCollection">Move to collection:</label>
                        <input type="text" id="newCollection" list="collectionList" placeholder="Enter collection name">
                        <datalist id="collectionList"></datalist>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="button" onclick="moveFile()">Move</button>
                    <button class="button" onclick="deleteFile()" style="color:#991b1b;">Delete</button>
                    <button class="button" onclick="closeModifyModal()">Cancel</button>
                </div>
            </div>
        </div>
    </div>
    """

    script = """
    <script>
        const state = {
            archives: [],
            jobs: [],
            collections: [],
        };

        function slugify(value) {
            return value
                .toLowerCase()
                .replace(/https?:\\/\\//g, "")
                .replace(/[^a-z0-9]+/g, "-")
                .replace(/^-+|-+$/g, "")
                .replace(/-{2,}/g, "-");
        }

        function suggestCollectionName(rawSeed) {
            const trimmed = rawSeed.trim();
            if (!trimmed) {
                return "";
            }

            let normalized = trimmed;
            if (!normalized.includes("://")) {
                normalized = "https://" + normalized;
            }

            try {
                const url = new URL(normalized);
                const pathBits = url.pathname.split("/").filter(Boolean).slice(0, 2);
                const parts = [url.hostname.replace(/^www\\./, ""), ...pathBits].filter(Boolean);
                const base = slugify(parts.join("-")) || "archive";
                const now = new Date();
                const stamp = [
                    now.getFullYear(),
                    String(now.getMonth() + 1).padStart(2, "0"),
                    String(now.getDate()).padStart(2, "0")
                ].join("") + "-" + [
                    String(now.getHours()).padStart(2, "0"),
                    String(now.getMinutes()).padStart(2, "0"),
                    String(now.getSeconds()).padStart(2, "0")
                ].join("");
                return `${base}-${stamp}`;
            } catch (error) {
                return "";
            }
        }

        function escapeHtml(value) {
            return String(value ?? "")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#39;");
        }

        function showTab(target) {
            document.querySelectorAll(".tab-panel").forEach((panel) => {
                panel.style.display = panel.id === `panel-${target}` ? "" : "none";
            });

            document.querySelectorAll("[data-tab-target]").forEach((button) => {
                const active = button.dataset.tabTarget === target;
                button.classList.toggle("active", active);
            });

            if (target === "archives") {
                loadArchives();
            }
            if (target === "about") {
                loadCollectionsPanel();
            }
        }

        function setStatus(message, type = "") {
            const banner = document.getElementById("crawlStatus");
            banner.textContent = message;
            banner.className = "status-banner" + (type ? ` ${type}` : "");
            banner.style.display = message ? "block" : "none";
        }

        async function loadCollectionsList() {
            try {
                const response = await fetch("/api/collections");
                const data = await response.json();
                state.collections = data.collections || [];
                const datalist = document.getElementById("collectionList");
                if (datalist) {
                    datalist.innerHTML = state.collections
                        .map((c) => `<option value="${escapeHtml(c.name)}">`)
                        .join("");
                }
            } catch (error) {
                console.warn("Could not load collections list:", error.message);
            }
        }

        async function loadArchives() {
            const list = document.getElementById("archiveList");
            try {
                const response = await fetch("/api/archives");
                const data = await response.json();
                state.archives = data.collections || [];

                document.getElementById("statCollections").textContent = String(state.archives.length);
                document.getElementById("statFiles").textContent = String(
                    state.archives.reduce((count, collection) => count + collection.file_count, 0)
                );

                if (!state.archives.length) {
                    list.innerHTML = '<div class="empty">No archives yet. Start a crawl and the finished WACZ files will appear here.</div>';
                    return;
                }

                list.innerHTML = state.archives.map((collection) => {
                    const latest = collection.latest_capture ? `<div class="meta">${escapeHtml(collection.latest_capture)}</div>` : "";
                    return `
                        <article class="archive-item">
                            <strong>${escapeHtml(collection.name)}</strong>
                            <div class="meta">${collection.file_count} file${collection.file_count === 1 ? "" : "s"} • ${escapeHtml(collection.total_size)}</div>
                            ${latest}
                            <div class="action-row">
                                <a class="button primary" href="/archive/${encodeURIComponent(collection.name)}">Open collection</a>
                            </div>
                        </article>
                    `;
                }).join("");
            } catch (error) {
                list.innerHTML = `<div class="empty">Could not load archives: ${escapeHtml(error.message)}</div>`;
            }
        }

        async function loadJobs() {
            const list = document.getElementById("jobList");
            try {
                const response = await fetch("/api/jobs");
                const data = await response.json();
                state.jobs = data.jobs || [];
                document.getElementById("statJobs").textContent = String(state.jobs.length);

                if (!state.jobs.length) {
                    list.innerHTML = '<div class="empty">No crawl jobs yet.</div>';
                    return;
                }

                list.innerHTML = state.jobs.map((job) => {
                    const statusClass = job.status === "failed" ? "error" : (job.status === "running" || job.status === "queued" ? "running" : "");
                    const message = job.message ? `<div class="meta">${escapeHtml(job.message)}</div>` : "";
                    return `
                        <article class="job-item">
                            <div class="badge-row">
                                <span class="badge ${statusClass}">${escapeHtml(job.status || "unknown")}</span>
                            </div>
                            <strong>${escapeHtml(job.collection || job.job_id)}</strong>
                            <div class="meta">${escapeHtml(job.started_at || "")}</div>
                            ${message}
                        </article>
                    `;
                }).join("");
            } catch (error) {
                list.innerHTML = `<div class="empty">Could not load jobs: ${escapeHtml(error.message)}</div>`;
            }
        }

        async function loadCollectionsPanel() {
            const panel = document.getElementById("collectionListPanel");
            if (!panel) return;
            panel.innerHTML = '<div class="empty">Loading…</div>';
            const data = await fetch("/api/collections").then((r) => r.json()).catch(() => ({}));
            const list = data.collections || [];
            if (!list.length) {
                panel.innerHTML = '<div class="empty">No collections yet.</div>';
                return;
            }
            panel.innerHTML = list.map((c) => `
                <article class="archive-item">
                    <strong>${escapeHtml(c.title || c.name)}</strong>
                    <div class="meta">${c.file_count} file${c.file_count !== 1 ? 's' : ''}</div>
                    <div class="action-row">
                        <button class="button" onclick="renameCollection('${escapeHtml(c.name)}')">Rename</button>
                        <button class="button" onclick="deleteCollection('${escapeHtml(c.name)}')" style="color: #991b1b;">Delete</button>
                    </div>
                </article>
            `).join("");
        }

        async function renameCollection(oldName) {
            const newName = prompt("Enter new name for collection:", oldName);
            if (!newName || newName === oldName) return;
            try {
                const res = await fetch("/api/collections/" + encodeURIComponent(oldName), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name: newName }),
                });
                const result = await res.json();
                if (result.error) {
                    alert(result.error);
                } else {
                    alert("Renamed to '" + result.name + "' (" + result.files + " files)");
                    loadCollectionsPanel();
                    loadCollectionsList();
                    loadArchives();
                }
            } catch (e) {
                alert("Failed: " + e.message);
            }
        }

        async function deleteCollection(name) {
            if (!confirm("Delete all archives in '" + name + "'? This cannot be undone.")) return;
            try {
                const res = await fetch("/api/collections/" + encodeURIComponent(name), {
                    method: "DELETE",
                });
                const result = await res.json();
                alert(result.message);
                loadCollectionsPanel();
                loadCollectionsList();
                loadArchives();
            } catch (e) {
                alert("Failed: " + e.message);
            }
        }

        document.getElementById("crawlForm").addEventListener("submit", async (event) => {
            event.preventDefault();

            const seeds = document.getElementById("seeds").value
                .split("\\n")
                .map((seed) => seed.trim())
                .filter(Boolean);

            if (!seeds.length) {
                setStatus("Add at least one seed URL before starting a crawl.", "error");
                return;
            }

            const submit = document.getElementById("crawlSubmit");
            submit.disabled = true;
            submit.textContent = "Starting…";
            setStatus("Starting crawl…");

            const payload = {
                seeds,
                collection: document.getElementById("collection").value.trim() || null,
                workers: Number.parseInt(document.getElementById("workers").value, 10),
                depth: Number.parseInt(document.getElementById("depth").value, 10),
                waitUntil: document.getElementById("waitUntil").value,
                generateWACZ: true,
                generateCDX: true,
                combineWARC: true
            };

            try {
                const response = await fetch("/api/crawl", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                const result = await response.json();

                if (!response.ok || result.error) {
                    throw new Error(result.error || "Crawl start failed");
                }

                setStatus(`Crawl queued for ${result.collection}. The archive list will update once the WACZ upload finishes.`);
                document.getElementById("collection").value = "";
                document.getElementById("crawlForm").reset();
                document.getElementById("collectionHint").textContent = "Suggested name will appear here once you add a seed.";
                loadJobs();
            } catch (error) {
                setStatus(error.message || String(error), "error");
            } finally {
                submit.disabled = false;
                submit.textContent = "Start crawl";
            }
        });

        document.querySelectorAll("[data-tab-target]").forEach((button) => {
            button.addEventListener("click", () => showTab(button.dataset.tabTarget));
        });

        document.getElementById("seeds").addEventListener("input", (event) => {
            const firstSeed = event.target.value.split("\\n").map((seed) => seed.trim()).find(Boolean) || "";
            const suggested = suggestCollectionName(firstSeed);
            document.getElementById("collectionHint").textContent = suggested
                ? `Suggested automatic slug: ${suggested}`
                : "Suggested name will appear here once you add a seed.";
        });

        let currentFileCollection = "";
        let currentFileName = "";

        function showModifyModal(collection, filename) {
            currentFileCollection = collection;
            currentFileName = filename;
            document.getElementById("modalFilename").textContent = currentFileCollection + "/" + currentFileName;
            document.getElementById("newCollection").value = currentFileCollection;
            document.getElementById("modifyModal").style.display = "flex";
            loadCollectionsForModal();
        }

        function closeModifyModal() {
            document.getElementById("modifyModal").style.display = "none";
        }

        async function loadCollectionsForModal() {
            try {
                const res = await fetch("/api/collections");
                const data = await res.json();
                const datalist = document.getElementById("collectionList");
                datalist.innerHTML = (data.collections || []).map(c =>
                    `<option value="${escapeHtml(c.name)}">`).join("");
            } catch (e) {
                console.warn("Could not load collections:", e.message);
            }
        }

        async function moveFile() {
            const newCollection = document.getElementById("newCollection").value.trim();
            if (!newCollection) {
                alert("Please enter a collection name.");
                return;
            }
            if (newCollection === currentFileCollection) {
                closeModifyModal();
                return;
            }
            try {
                const res = await fetch("/api/files/" + encodeURIComponent(currentFileCollection) + "/" + encodeURIComponent(currentFileName), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ collection: newCollection })
                });
                const result = await res.json();
                if (result.error) {
                    alert(result.error);
                } else {
                    alert("Moved to '" + result.to + "'");
                    closeModifyModal();
                    window.location.reload();
                }
            } catch (e) {
                alert("Failed: " + e.message);
            }
        }

        async function deleteFile() {
            if (!confirm("Delete '" + currentFileName + "'? This cannot be undone.")) return;
            try {
                const res = await fetch("/api/files/" + encodeURIComponent(currentFileCollection) + "/" + encodeURIComponent(currentFileName), {
                    method: "DELETE"
                });
                const result = await res.json();
                if (result.error) {
                    alert(result.error);
                } else {
                    alert("Deleted '" + result.file + "'");
                    closeModifyModal();
                    window.location.reload();
                }
            } catch (e) {
                alert("Failed: " + e.message);
            }
        }

        loadCollectionsList();
        loadArchives();
        loadJobs();
        window.setInterval(loadJobs, 4000);
        window.setInterval(loadArchives, 10000);
    </script>
    """

    return render_layout("WebVault", body, extra_script=script)


def render_collection_page(collection: str, files: list[dict[str, Any]]) -> HTMLResponse:
    collection_title = friendly_title(collection)
    body = f"""
    <div class="shell">
        <div class="topbar">
            <a class="brand" href="/">
                <img class="brand-mark" src="/img/webvault.svg" alt="WebVault logo" aria-hidden="true">
            </a>
        </div>
        <section class="hero">
            <h1>{escape(collection_title)}</h1>
            <p>Open the latest WACZ files in ReplayWeb or download them directly. Titles use crawl metadata when present, with the collection slug as a stable fallback.</p>
        </section>
        <section class="panel">
            <h2>Files</h2>
            <p class="panel-subtitle">{len(files)} file{'s' if len(files) != 1 else ''} in this collection.</p>
            <div class="file-list">
                {"".join(
                    f'''
                    <article class="file-item">
                        <strong>{escape(file["title"])}</strong>
                        <div class="meta">{escape(file["description"] or "No additional metadata")}</div>
                        <div class="badge-row">
                            <span class="badge">{escape(file["size"])}</span>
                        </div>
                        <div class="action-row">
                            <a class="button primary" href="/replay/{quote(collection)}/{quote(file["name"])}">Replay</a>
                            <a class="button" href="/download/{quote(collection)}/{quote(file["name"])}">Download</a>
                            <button class="button" onclick="showModifyModal('{escape(collection)}', '{escape(file["name"])}')">Modify</button>
                        </div>
                    </article>
                    '''
                    for file in files
                ) or '<div class="empty">No WACZ files were found in this collection.</div>'}
            </div>
        </section>

        <div id="modifyModal" class="modal" style="display:none;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>Modify File</h3>
                    <button class="modal-close" onclick="closeModifyModal()">&times;</button>
                </div>
                <div class="modal-body">
                    <p class="modal-filename" id="modalFilename"></p>
                    <div class="form-group">
                        <label for="newCollection">Move to collection:</label>
                        <input type="text" id="newCollection" list="collectionList" placeholder="Enter collection name">
                        <datalist id="collectionList"></datalist>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="button" onclick="moveFile()">Move</button>
                    <button class="button" onclick="deleteFile()" style="color:#991b1b;">Delete</button>
                    <button class="button" onclick="closeModifyModal()">Cancel</button>
                </div>
            </div>
        </div>
    </div>
    <script>
        let currentFileCollection = "";
        let currentFileName = "";

        function showModifyModal(collection, filename) {{
            currentFileCollection = collection;
            currentFileName = filename;
            document.getElementById("modalFilename").textContent = currentFileCollection + "/" + currentFileName;
            document.getElementById("newCollection").value = currentFileCollection;
            document.getElementById("modifyModal").style.display = "flex";
            loadCollectionsForModal();
        }}

        function closeModifyModal() {{
            document.getElementById("modifyModal").style.display = "none";
        }}

        async function loadCollectionsForModal() {{
            try {{
                const res = await fetch("/api/collections");
                const data = await res.json();
                const datalist = document.getElementById("collectionList");
                datalist.innerHTML = (data.collections || []).map(c =>
                    `<option value="${{encodeURIComponent(c.name)}}">`).join("");
            }} catch (e) {{
                console.warn("Could not load collections:", e.message);
            }}
        }}

        async function moveFile() {{
            const newCollection = document.getElementById("newCollection").value.trim();
            if (!newCollection) {{
                alert("Please enter a collection name.");
                return;
            }}
            if (newCollection === currentFileCollection) {{
                closeModifyModal();
                return;
            }}
            try {{
                const res = await fetch("/api/files/" + encodeURIComponent(currentFileCollection) + "/" + encodeURIComponent(currentFileName), {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ collection: newCollection }})
                }});
                const result = await res.json();
                if (result.error) {{
                    alert(result.error);
                }} else {{
                    alert("Moved to '" + result.to + "'");
                    closeModifyModal();
                    window.location.reload();
                }}
            }} catch (e) {{
                alert("Failed: " + e.message);
            }}
        }}

        async function deleteFile() {{
            if (!confirm("Delete '" + currentFileName + "'? This cannot be undone.")) return;
            try {{
                const res = await fetch("/api/files/" + encodeURIComponent(currentFileCollection) + "/" + encodeURIComponent(currentFileName), {{
                    method: "DELETE"
                }});
                const result = await res.json();
                if (result.error) {{
                    alert(result.error);
                }} else {{
                    alert("Deleted '" + result.file + "'");
                    closeModifyModal();
                    window.location.reload();
                }}
            }} catch (e) {{
                alert("Failed: " + e.message);
            }}
        }}
    </script>
    """
    return render_layout(f"{collection_title} · WebVault", body)


def render_replay_page(source: str, title: str, initial_url: str = "") -> HTMLResponse:
    extra_head = f"""
    <script src="/replay/ui.js"></script>
    """
    initial_url_attr = (
        f'url="{escape(initial_url, quote=True)}"' if initial_url else 'view="pages"'
    )
    body = f"""
    <replay-web-page
        source="{escape(source, quote=True)}"
        replaybase="/wb/"
        swName="sw.js"
        embed="default"
        loading="eager"
        {initial_url_attr}
    ></replay-web-page>
    <script>
        const localhostNames = new Set(["localhost", "127.0.0.1", "[::1]"]);
        if (!window.isSecureContext && !localhostNames.has(window.location.hostname)) {{
            console.warn("ReplayWeb requires HTTPS or localhost for service worker replay in most browsers.");
        }}
    </script>
    """
    return render_layout(title, body, extra_head=extra_head)


def render_replay_wrapper_page(
    source: str,
    title: str,
    back_href: str,
    back_label: str,
    download_href: str,
    initial_url: str = "",
) -> HTMLResponse:
    replay_url = f"/replay/?source={quote(source, safe='')}&title={quote(title, safe='')}"
    if initial_url:
        replay_url += f"&url={quote(initial_url, safe='')}"
    body = f"""
    <div class="shell">
        <div class="topbar">
            <a class="brand" href="{escape(back_href, quote=True)}">
                <img class="brand-mark" src="/img/webvault.svg" alt="WebVault logo" aria-hidden="true">
                <span class="brand-copy">
                    <strong>{escape(title)}</strong>
                    <span>{escape(back_label)}</span>
                </span>
            </a>
            <div class="nav">
                <a class="nav-link" href="{escape(back_href, quote=True)}">Back</a>
                <a class="button" href="{escape(download_href, quote=True)}">Download WACZ</a>
            </div>
        </div>
        <section class="panel replay-panel">
            <iframe class="replay-shell" src="{escape(replay_url, quote=True)}" title="{escape(title, quote=True)}"></iframe>
        </section>
    </div>
    """
    return render_layout(f"{title} · Replay", body)


def mark_uncached(response: HTMLResponse) -> HTMLResponse:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def uncached_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if extra:
        headers.update(extra)
    return headers


def ensure_crawler_container_ready() -> None:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CRAWLER_CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Crawler container is unavailable."
        raise RuntimeError(message)
    if result.stdout.strip().lower() != "true":
        raise RuntimeError(f"{CRAWLER_CONTAINER_NAME} exists but is not running.")


def find_local_wacz(job_id: str, filename: str | None = None) -> Path | None:
    candidate_roots = [
        CRAWL_DIR / "collections" / job_id,
        CRAWL_DIR / job_id,
    ]

    for root in candidate_roots:
        if root.exists():
            if filename:
                exact = root / filename
                if exact.exists():
                    return exact
            matches = sorted(root.glob("*.wacz" if not filename else filename))
            if matches:
                return matches[0]

    pattern = filename if filename else f"{job_id}*.wacz"
    matches = sorted(CRAWL_DIR.rglob(pattern))
    return matches[0] if matches else None


def upload_archive_to_garage(wacz_path: Path, group: str) -> str:
    key = f"{group}/{wacz_path.name}"
    body = wacz_path.read_bytes()
    get_s3_client().put_object(
        Bucket=GARAGE_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/wacz",
    )
    return key


def repair_archive_from_local(collection: str, filename: str) -> bytes:
    local_path = find_local_wacz(collection, filename)
    if not local_path:
        raise FileNotFoundError(
            f"No matching local WACZ found for {collection}/{filename} to repair object storage."
        )

    logger.warning("Repairing corrupted archive object for %s/%s from %s", collection, filename, local_path)
    upload_archive_to_garage(local_path, collection)
    return local_path.read_bytes()


def read_archive_bytes(collection: str, filename: str) -> bytes:
    key = f"{collection}/{filename}"
    try:
        response = get_s3_client().get_object(Bucket=GARAGE_BUCKET, Key=key)
        return response["Body"].read()
    except FlexibleChecksumError:
        return repair_archive_from_local(collection, filename)


def get_archive_size(collection: str, filename: str) -> int:
    local_path = find_local_wacz(collection, filename)
    if local_path:
        return local_path.stat().st_size

    key = f"{collection}/{filename}"
    response = get_s3_client().head_object(Bucket=GARAGE_BUCKET, Key=key)
    return int(response.get("ContentLength", 0))


def get_archive_version_token(collection: str, filename: str) -> str:
    local_path = find_local_wacz(collection, filename)
    if local_path:
        stat = local_path.stat()
        return f"{stat.st_size}-{stat.st_mtime_ns}"

    key = f"{collection}/{filename}"
    response = get_s3_client().head_object(Bucket=GARAGE_BUCKET, Key=key)
    size = int(response.get("ContentLength", 0))
    last_modified = response.get("LastModified")
    modified_value = "0"
    if last_modified is not None:
        modified_value = str(int(last_modified.timestamp()))
    return f"{size}-{modified_value}"


def read_archive_range(collection: str, filename: str, start: int, end: int) -> bytes:
    local_path = find_local_wacz(collection, filename)
    if local_path:
        with local_path.open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start + 1)

    key = f"{collection}/{filename}"
    try:
        response = get_s3_client().get_object(
            Bucket=GARAGE_BUCKET,
            Key=key,
            Range=f"bytes={start}-{end}",
        )
        return response["Body"].read()
    except FlexibleChecksumError:
        body = repair_archive_from_local(collection, filename)
        return body[start : end + 1]


def parse_range_header(range_header: str, size: int) -> tuple[int, int]:
    if not range_header.startswith("bytes="):
        raise ValueError("Unsupported range unit.")

    value = range_header.split("=", 1)[1].split(",", 1)[0].strip()
    start_str, end_str = value.split("-", 1)

    if not start_str:
        length = int(end_str)
        if length <= 0:
            raise ValueError("Invalid suffix range.")
        start = max(size - length, 0)
        end = size - 1
    else:
        start = int(start_str)
        end = int(end_str) if end_str else size - 1

    if start < 0 or start >= size:
        raise ValueError("Range start out of bounds.")
    if end < start:
        raise ValueError("Range end before start.")

    return start, min(end, size - 1)


async def run_crawl_job(job_id: str, group: str, config_file: Path) -> None:
    update_job(
        job_id,
        status="running",
        message="Crawler is running inside the Browsertrix container.",
    )

    command = [
        "docker",
        "exec",
        CRAWLER_CONTAINER_NAME,
        "crawl",
        "--config",
        f"/crawls/{config_file.name}",
    ]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            error_output = (result.stderr or result.stdout or "Crawler exited with a non-zero status.").strip()
            update_job(job_id, status="failed", message=error_output)
            logger.error("Crawl %s failed: %s", job_id, error_output)
            return

        wacz_path = None
        for _ in range(15):
            wacz_path = find_local_wacz(job_id)
            if wacz_path:
                break
            await asyncio.sleep(2)

        if not wacz_path:
            message = "Crawl finished but no WACZ output was found under /crawls."
            update_job(job_id, status="failed", message=message)
            logger.error("Crawl %s completed without WACZ output", job_id)
            return

        update_job(job_id, status="uploading", message=f"Uploading {wacz_path.name} to Garage…")
        key = await asyncio.to_thread(upload_archive_to_garage, wacz_path, group)
        update_job(job_id, status="completed", message=f"Uploaded to {key}")
        logger.info("Uploaded crawl %s to %s", job_id, key)
    except Exception as exc:
        logger.exception("Unexpected crawl error for %s", job_id)
        update_job(job_id, status="failed", message=str(exc))


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return render_home_page()


@app.get("/api/archives")
async def list_archives() -> dict[str, Any]:
    try:
        return {"collections": get_collection_payloads()}
    except Exception as exc:
        logger.exception("Failed to list archives")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/jobs")
async def list_jobs() -> dict[str, Any]:
    jobs = sorted(
        JOBS.values(),
        key=lambda item: item.get("started_at", ""),
        reverse=True,
    )
    return {"jobs": jobs[:20]}


@app.get("/api/collections")
async def list_collections_api() -> dict[str, Any]:
    try:
        collections = get_collection_payloads()
        return {"collections": [{"name": c["name"], "title": c["title"], "file_count": c["file_count"]} for c in collections]}
    except Exception as exc:
        logger.exception("Failed to list collections")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/collections/{old_name}")
async def rename_collection(old_name: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid JSON body"}
    new_name = slugify((body or {}).get("name", ""))
    if not new_name:
        return {"error": "New collection name is required"}
    if new_name == old_name:
        return {"name": new_name, "message": "No change needed"}

    all_keys = list_all_wacz_keys()
    to_move = [k for k in all_keys if k["group"] == old_name]
    if not to_move:
        return {"error": f"No collection named '{old_name}' found"}

    old_group = slugify(old_name) or old_name
    s3 = get_s3_client()
    for k in to_move:
        new_key = f"{new_name}/{k['name']}"
        s3.copy_object(Bucket=GARAGE_BUCKET, Key=new_key, CopySource={"Bucket": GARAGE_BUCKET, "Key": k["key"]})
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=k["key"])
        logger.info("Renamed %s → %s", k["key"], new_key)

    logger.info("Collection renamed: %s → %s (%d files)", old_name, new_name, len(to_move))
    return {"name": new_name, "files": len(to_move), "message": f"Renamed to '{new_name}'"}


@app.delete("/api/collections/{name}")
async def delete_collection(name: str) -> dict[str, Any]:
    all_keys = list_all_wacz_keys()
    to_delete = [k for k in all_keys if k["group"] == name]
    if not to_delete:
        return {"deleted": 0}

    s3 = get_s3_client()
    for k in to_delete:
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=k["key"])
        logger.info("Deleted %s", k["key"])

    logger.info("Collection deleted: %s (%d files)", name, len(to_delete))
    return {"deleted": len(to_delete), "message": f"Deleted {len(to_delete)} files from '{name}'"}


@app.post("/api/files/{collection}/{filename}")
async def move_file(collection: str, filename: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid JSON body"}
    new_collection = slugify((body or {}).get("collection", ""))
    if not new_collection:
        return {"error": "New collection name is required"}

    s3 = get_s3_client()
    old_key = f"{collection}/{filename}"
    new_key = f"{new_collection}/{filename}"

    try:
        s3.copy_object(Bucket=GARAGE_BUCKET, Key=new_key, CopySource={"Bucket": GARAGE_BUCKET, "Key": old_key})
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=old_key)
        logger.info("Moved file %s → %s", old_key, new_key)
        return {"moved": True, "from": collection, "to": new_collection, "file": filename}
    except Exception as exc:
        logger.exception("Failed to move file")
        return {"error": str(exc)}


@app.delete("/api/files/{collection}/{filename}")
async def delete_file(collection: str, filename: str) -> dict[str, Any]:
    s3 = get_s3_client()
    key = f"{collection}/{filename}"

    try:
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=key)
        logger.info("Deleted file %s", key)
        return {"deleted": True, "file": filename}
    except Exception as exc:
        logger.exception("Failed to delete file")
        return {"error": str(exc)}


@app.get("/archive/{collection}", response_class=HTMLResponse)
async def view_collection(collection: str) -> HTMLResponse:
    try:
        files = get_archive_files(collection)
    except Exception as exc:
        logger.exception("Failed to load collection %s", collection)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return render_collection_page(collection, files)


def absolute_route_url(request: Request, route_name: str, **path_params: str) -> str:
    absolute = request.url_for(route_name, **path_params)
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        absolute = absolute.replace(scheme=forwarded_proto.split(",")[0].strip())
    return str(absolute)


@app.get("/replay/{collection}/{filename}", response_class=HTMLResponse)
async def replay_archive(collection: str, filename: str, request: Request) -> HTMLResponse:
    source_url = absolute_route_url(
        request,
        "replay_wacz",
        collection=collection,
        filename=filename,
    )
    try:
        version_token = get_archive_version_token(collection, filename)
        source_url = f"{source_url}?v={quote(version_token, safe='')}"
    except Exception as exc:
        logger.warning("Could not build replay version token for %s/%s: %s", collection, filename, exc)
    initial_url = ""
    try:
        files = get_archive_files(collection)
        for file in files:
            if file["name"] == filename:
                initial_url = file.get("seed_url", "")
                break
    except Exception as exc:
        logger.warning("Could not load archive metadata for %s/%s: %s", collection, filename, exc)
    return mark_uncached(render_replay_wrapper_page(
        source=source_url,
        title=friendly_title(Path(filename).stem),
        back_href=f"/archive/{quote(collection)}",
        back_label=friendly_title(collection),
        download_href=f"/download/{quote(collection)}/{quote(filename)}",
        initial_url=initial_url,
    ))


@app.get("/replay/")
async def replay_index(request: Request) -> HTMLResponse:
    source = request.query_params.get("source", "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="Missing source query parameter.")

    title = request.query_params.get("title", "Replay")
    initial_url = request.query_params.get("url", "").strip()
    return mark_uncached(render_replay_page(source=source, title=title, initial_url=initial_url))


@app.get("/replay/w/", response_class=HTMLResponse)
async def replay_frame() -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Replay Frame</title>
    <style>
        html, body {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            background: #ffffff;
        }
        replay-app-main {
            display: block;
            width: 100%;
            height: 100%;
        }
    </style>
</head>
<body>
    <replay-app-main></replay-app-main>
</body>
</html>"""
    return mark_uncached(HTMLResponse(content=html))


@app.get("/wb/", response_class=HTMLResponse)
async def replay_scope_shell() -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Replay Scope</title>
    <style>
        html, body {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            background: #ffffff;
        }
        replay-app-main {
            display: block;
            width: 100%;
            height: 100%;
        }
    </style>
</head>
<body>
    <replay-app-main></replay-app-main>
</body>
</html>"""
    return mark_uncached(HTMLResponse(content=html))


@app.get("/wb/sw.js")
async def replay_sw() -> Response:
    return Response(
        content=(REPLAYWEB_DIR / "sw.js").read_text(),
        media_type="application/javascript",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/wb/{path:path}/sw.js")
async def replay_sw_nested(path: str) -> Response:
    return await replay_sw()


@app.get("/replay/ui.js")
async def replay_ui() -> Response:
    return Response(
        content=(REPLAYWEB_DIR / "ui.js").read_text(),
        media_type="application/javascript",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.head("/replay-wacz/{collection}/{filename}")
async def replay_wacz_head(collection: str, filename: str) -> Response:
    try:
        size = get_archive_size(collection, filename)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(
        media_type="application/wacz",
        headers=uncached_headers({
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Content-Length": str(size),
        }),
    )


@app.get("/replay-wacz/{collection}/{filename}")
async def replay_wacz(request: Request, collection: str, filename: str) -> Response:
    try:
        size = get_archive_size(collection, filename)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    range_header = request.headers.get("range")
    if range_header:
        try:
            start, end = parse_range_header(range_header, size)
            body = read_archive_range(collection, filename, start, end)
        except ValueError as exc:
            return Response(
                status_code=416,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{size}",
                },
                content=str(exc),
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return Response(
            content=body,
            status_code=206,
            media_type="application/wacz",
            headers=uncached_headers({
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            }),
        )

    try:
        body = read_archive_bytes(collection, filename)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(
        content=body,
        media_type="application/wacz",
        headers=uncached_headers({
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(body)),
        }),
    )


@app.get("/download/{collection}/{filename}")
async def download_archive(collection: str, filename: str) -> StreamingResponse:
    key = f"{collection}/{filename}"
    try:
        response = get_s3_client().get_object(Bucket=GARAGE_BUCKET, Key=key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    body = response["Body"].read()
    return StreamingResponse(
        iter([body]),
        media_type="application/wacz",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.post("/api/crawl")
async def start_crawl(config: CrawlConfig) -> dict[str, Any]:
    try:
        ensure_crawler_container_ready()
    except RuntimeError as exc:
        return {"error": str(exc)}

    try:
        seeds = [normalize_seed_url(seed) for seed in config.seeds]
        seeds = [seed for seed in seeds if seed]
    except ValueError as exc:
        return {"error": str(exc)}

    if not seeds:
        return {"error": "Please provide at least one valid seed URL."}

    group = slugify(config.collection or "") if config.collection else DEFAULT_GROUP

    job_id = suggest_browsertrix_name(seeds)
    config_data = config.model_dump()
    config_data["collection"] = job_id
    config_data["seeds"] = seeds
    config_data["text"] = config_data.get("text") or ["to-pages"]

    os.makedirs(CRAWL_DIR, exist_ok=True)
    config_file = CRAWL_DIR / f"{job_id}.yaml"
    config_file.write_text(yaml.safe_dump(config_data, sort_keys=False))

    update_job(
        job_id,
        collection=group,
        status="queued",
        started_at=format_timestamp(datetime.now()),
        message="Queued and waiting for the crawler container.",
    )

    asyncio.create_task(run_crawl_job(job_id, group, config_file))
    return {"job_id": job_id, "collection": group, "status": "queued"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
