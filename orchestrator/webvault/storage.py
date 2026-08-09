import gzip
import io
import json
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import FlexibleChecksumError

from .config import settings
from .utils import (
    format_bytes,
    format_duration,
    format_timestamp,
    friendly_title,
    read_jsonl_lines,
    seed_label,
)

logger = logging.getLogger("webvault")
CRAWL_DIR = settings.crawl_dir
GARAGE_ENDPOINT = settings.garage_endpoint
GARAGE_ACCESS_KEY = settings.garage_access_key
GARAGE_SECRET_KEY = settings.garage_secret_key
GARAGE_BUCKET = settings.garage_bucket


def wacz_contains_url(path: Path, target_url: str):
    target = target_url.split("#", 1)[0]
    try:
        with zipfile.ZipFile(path) as archive:
            indexes = [
                name
                for name in archive.namelist()
                if name.startswith("indexes/")
                and (name.endswith(".cdxj") or name.endswith(".cdx.gz"))
            ]
            for name in indexes:
                content = archive.read(name)
                if name.endswith(".gz"):
                    content = gzip.decompress(content)
                for raw_line in content.decode("utf-8", errors="replace").splitlines():
                    parts = raw_line.split(" ", 2)
                    if len(parts) != 3:
                        continue
                    try:
                        record = json.loads(parts[2])
                    except json.JSONDecodeError:
                        continue
                    if str(record.get("url") or "").split("#", 1)[0] == target:
                        return True
    except (OSError, zipfile.BadZipFile, gzip.BadGzipFile):
        return False
    return False


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=GARAGE_ENDPOINT,
        aws_access_key_id=GARAGE_ACCESS_KEY,
        aws_secret_access_key=GARAGE_SECRET_KEY,
        region_name="garage",
        config=Config(s3={"addressing_style": "path"}),
    )


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
                keys.append(
                    {
                        "group": group,
                        "name": wacz_name,
                        "key": key,
                        "size": obj.get("Size", 0),
                        "last_modified": obj.get("LastModified"),
                    }
                )
        if not result.get("IsTruncated"):
            break
        continuation_token = result.get("NextContinuationToken")
    return keys


def extract_wacz_metadata(body: bytes | Path, fallback_name: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "seed_url": "",
        "page_title": "",
        "pages_count": 0,
        "started_at": "",
        "duration": "",
    }

    source = body if isinstance(body, Path) else io.BytesIO(body)
    with zipfile.ZipFile(source, "r") as archive:
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
            name for name in archive.namelist() if "/logs/" in name or name.startswith("logs/")
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
            metadata["duration"] = format_duration((timestamps[-1] - timestamps[0]).total_seconds())

    metadata["title"] = (
        metadata["page_title"]
        or seed_label(metadata["seed_url"])
        or friendly_title(Path(fallback_name).stem)
    )
    return metadata


def extract_search_documents(path: Path, collection: str):
    documents: list[dict[str, str]] = []
    with zipfile.ZipFile(path) as archive:
        page_files = [
            name
            for name in archive.namelist()
            if name.startswith("pages/") and name.endswith(".jsonl")
        ]
        for page_file in page_files:
            for entry in read_jsonl_lines(archive.read(page_file)):
                url = str(entry.get("url") or "")
                title = str(entry.get("title") or "")
                body = entry.get("text") or entry.get("content") or ""
                if isinstance(body, list):
                    body = "\n".join(str(part) for part in body)
                if not isinstance(body, str):
                    body = str(body)
                if url or title or body:
                    documents.append(
                        {
                            "collection": collection,
                            "url": url[:8_192],
                            "title": title[:2_048],
                            "body": body[:2_000_000],
                        }
                    )
    return documents


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
                sidecar = s3.get_object(Bucket=GARAGE_BUCKET, Key=f"{key}.metadata.json")
                metadata = json.loads(sidecar["Body"].read())
            except Exception:
                # Older archives may not have a sidecar. Do not download the
                # entire WACZ just to render a listing; replay remains available.
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
                description_bits.append(f"{metadata['pages_count']} pages")
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


def list_collection_markers():
    collections: set[str] = set()
    s3 = get_s3_client()
    continuation_token = None
    while True:
        params: dict[str, Any] = {"Bucket": GARAGE_BUCKET}
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        result = s3.list_objects_v2(**params)
        for obj in result.get("Contents", []):
            key = str(obj.get("Key") or "")
            if key.endswith("/.webvault-collection") and "/" in key:
                collections.add(key.rsplit("/", 1)[0])
        if not result.get("IsTruncated"):
            break
        continuation_token = result.get("NextContinuationToken")
    return collections


def get_collection_payloads() -> list[dict[str, Any]]:
    collections_dict: dict[str, dict[str, Any]] = {
        group: {
            "name": group,
            "title": friendly_title(group),
            "label": friendly_title(group),
            "files": [],
            "total_size": 0,
        }
        for group in list_collection_markers()
    }
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
        collections_dict[group]["files"].append(
            {
                "collection": group,
                "name": wacz["name"],
                "title": wacz["name"]
                .removesuffix(".wacz")
                .replace("_", " ")
                .replace("-", " ")
                .title(),
                "description": "",
                "size": format_bytes(wacz["size"]),
                "size_bytes": wacz["size"],
                "seed_url": "",
                "pages_count": 0,
                "last_modified": wacz["last_modified"].isoformat() if wacz["last_modified"] else "",
            }
        )
        collections_dict[group]["total_size"] += wacz["size"]

    collections: list[dict[str, Any]] = []
    for coll in collections_dict.values():
        files = sorted(coll["files"], key=lambda f: f.get("last_modified", ""), reverse=True)
        latest = files[0] if files else {}
        collections.append(
            {
                "name": coll["name"],
                "title": friendly_title(coll["name"]),
                "label": coll["label"],
                "file_count": len(files),
                "total_size": format_bytes(coll["total_size"]),
                "latest_capture": latest.get("last_modified", ""),
                "files": files,
            }
        )

    collections.sort(
        key=lambda item: item["files"][0].get("last_modified", "") if item["files"] else "",
        reverse=True,
    )
    return collections


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


def upload_archive_to_garage(wacz_path: Path, group: str):
    key = f"{group}/{wacz_path.name}"
    metadata: dict[str, Any] = {}
    # upload_file uses boto3's managed, multipart transfer and does not load a
    # potentially multi-gigabyte crawl archive into orchestrator memory.
    s3 = get_s3_client()
    s3.upload_file(
        str(wacz_path),
        GARAGE_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/wacz"},
    )
    try:
        metadata = extract_wacz_metadata(wacz_path, wacz_path.name)
        s3.put_object(
            Bucket=GARAGE_BUCKET,
            Key=f"{key}.metadata.json",
            Body=json.dumps(metadata).encode(),
            ContentType="application/json",
        )
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning("Could not create metadata sidecar for %s: %s", key, exc)
    return key, metadata


def repair_archive_from_local(collection: str, filename: str) -> bytes:
    local_path = find_local_wacz(collection, filename)
    if not local_path:
        raise FileNotFoundError(
            f"No matching local WACZ found for {collection}/{filename} to repair object storage."
        )

    logger.warning(
        "Repairing corrupted archive object for %s/%s from %s", collection, filename, local_path
    )
    upload_archive_to_garage(local_path, collection)
    return local_path.read_bytes()


def read_archive_bytes(collection: str, filename: str) -> bytes:
    key = f"{collection}/{filename}"
    try:
        response = get_s3_client().get_object(Bucket=GARAGE_BUCKET, Key=key)
        return response["Body"].read()
    except FlexibleChecksumError:
        return repair_archive_from_local(collection, filename)


def open_archive_stream(collection: str, filename: str):
    key = f"{collection}/{filename}"
    response = get_s3_client().get_object(Bucket=GARAGE_BUCKET, Key=key)
    return response["Body"]


def get_archive_size(collection: str, filename: str) -> int:
    # Object storage is authoritative. A local crawl with a matching filename
    # must not make a deleted or moved S3 object appear to still exist.
    key = f"{collection}/{filename}"
    response = get_s3_client().head_object(Bucket=GARAGE_BUCKET, Key=key)
    return int(response.get("ContentLength", 0))


def get_archive_version_token(collection: str, filename: str) -> str:
    key = f"{collection}/{filename}"
    response = get_s3_client().head_object(Bucket=GARAGE_BUCKET, Key=key)
    size = int(response.get("ContentLength", 0))
    last_modified = response.get("LastModified")
    modified_value = "0"
    if last_modified is not None:
        modified_value = str(int(last_modified.timestamp()))
    return f"{size}-{modified_value}"


def read_archive_range(collection: str, filename: str, start: int, end: int) -> bytes:
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


def delete_other_collection_archives(group: str, keep_key: str):
    s3 = get_s3_client()
    for item in list_all_wacz_keys():
        if item["group"] == group and item["key"] != keep_key:
            s3.delete_object(Bucket=GARAGE_BUCKET, Key=item["key"])
            s3.delete_object(Bucket=GARAGE_BUCKET, Key=f"{item['key']}.metadata.json")


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
