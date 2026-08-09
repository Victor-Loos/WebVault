import ipaddress
import json
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit


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


def canonical_seed_url(seed: str) -> str:
    normalized = normalize_seed_url(seed)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if port and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def validate_seed_target(seed: str, allow_private: bool = False) -> str:
    normalized = normalize_seed_url(seed)
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Seed URLs cannot contain credentials.")
    try:
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError(f"Invalid URL: {seed}") from exc
    if not host:
        raise ValueError(f"Invalid URL: {seed}")
    if allow_private:
        return normalized

    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Seed host could not be resolved: {host}") from exc
    if not addresses:
        raise ValueError(f"Seed host could not be resolved: {host}")

    for address in addresses:
        raw_address = str(address[4][0]).split("%", 1)[0]
        ip = ipaddress.ip_address(raw_address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise ValueError(
                "Private, loopback, link-local, and reserved crawl targets are blocked."
            )
    return normalized


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
    # Include microseconds so simultaneous requests cannot overwrite one another.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{slug}-{stamp}"


def extract_base_group(wacz_path: Path) -> str:
    name = wacz_path.stem.removeprefix(".wacz").removesuffix(".wacz")
    stamp_idx = name.rfind("-")
    while stamp_idx > 0 and len(name) - stamp_idx < 7:
        stamp_idx = name.rfind("-", 0, stamp_idx)
    base = name[:stamp_idx] if stamp_idx > 0 else name
    return slugify(base) or DEFAULT_GROUP


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
