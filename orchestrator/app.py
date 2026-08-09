import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import websockets
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from websockets.exceptions import WebSocketException
from webvault.config import APP_DIR, settings
from webvault.models import (
    CrawlConfig as CrawlConfig,
)
from webvault.models import (
    MoveFileRequest,
    RenameCollectionRequest,
)
from webvault.routers.auth import (
    AUTH_COOKIE,
    AUTH_ENABLED,
    session_auth,
)
from webvault.routers.auth import (
    router as auth_router,
)
from webvault.routers.history import router as history_router
from webvault.routers.jobs import router as jobs_router
from webvault.routers.pages import router as pages_router
from webvault.routers.profiles import router as profiles_router
from webvault.routers.stats import router as stats_router
from webvault.state import job_store, session_store
from webvault.storage import (
    get_archive_size,
    get_archive_version_token,
    extract_search_documents,
    find_local_wacz,
    get_collection_payloads,
    get_s3_client,
    list_all_wacz_keys,
    list_collection_markers,
    open_archive_stream,
    parse_range_header,
    read_archive_range,
)
from webvault.utils import (
    friendly_title,
    slugify,
)
from webvault.utils import (
    normalize_seed_url as normalize_seed_url,
)
from webvault.utils import (
    suggest_browsertrix_name as suggest_browsertrix_name,
)
from webvault.utils import (
    validate_seed_target as validate_seed_target,
)
from webvault.views import (
    STATIC_DIR,
    mark_uncached,
    render_live_control_page,
    render_live_page,
    render_replay_page,
    render_replay_wrapper_page,
    uncached_headers,
)
from webvault.views import (
    render_collection_page as render_collection_page,
)
from webvault.views import (
    render_home_page as render_home_page,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webvault")

REPLAYWEB_DIR = settings.replay_dir
GARAGE_BUCKET = settings.garage_bucket
SCREENCAST_URL = settings.resolved_screencast_url

if not settings.garage_access_key or not settings.garage_secret_key:
    raise ValueError("Missing GARAGE_ACCESS_KEY or GARAGE_SECRET_KEY. Run setup.sh first.")


def index_existing_local_archives():
    for job_id, job in job_store.load_all().items():
        if job.get("status") != "completed" or job_store.has_search_documents(job_id):
            continue
        path = find_local_wacz(job_id)
        if not path:
            continue
        try:
            documents = extract_search_documents(path, str(job.get("collection") or ""))
            job_store.replace_search_documents(job_id, documents)
        except Exception:
            logger.exception("Could not index local archive for job %s", job_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    job_store.load_all()
    session_store.delete_expired()
    indexing_task = asyncio.create_task(asyncio.to_thread(index_existing_local_archives))
    yield
    if not indexing_task.done():
        indexing_task.cancel()


app = FastAPI(title="WebVault", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

app.middleware("http")(session_auth)
app.include_router(auth_router)
app.include_router(pages_router)
app.include_router(jobs_router)
app.include_router(history_router)
app.include_router(stats_router)
app.include_router(profiles_router)


app.mount("/img", StaticFiles(directory=APP_DIR / "img"), name="img")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def archive_http_exception(exc: Exception):
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return HTTPException(status_code=404, detail="Archive not found.")
    logger.error(
        "Archive storage operation failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return HTTPException(status_code=503, detail="Archive storage is unavailable.")


@app.get("/live/control", response_class=HTMLResponse)
async def live_control(request: Request) -> HTMLResponse:
    return render_live_control_page(getattr(request.state, "csrf_token", ""))


@app.get("/api/live/targets")
async def live_control_targets() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get("http://webvault-crawler:9222/json/list")
            response.raise_for_status()
        targets = [
            {
                "id": str(target.get("id") or ""),
                "title": str(target.get("title") or "Browser page")[:200],
                "url": str(target.get("url") or "")[:2_048],
            }
            for target in response.json()
            if target.get("type") == "page" and target.get("id")
        ]
        return {"targets": targets}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Interactive browser control is not available for this crawl.",
        ) from exc


@app.get("/live/", response_class=HTMLResponse)
async def live_page(request: Request) -> HTMLResponse:
    return render_live_page(getattr(request.state, "csrf_token", ""))


@app.get("/live/view/")
@app.get("/live/view/{path:path}")
async def live_screencast(path: str = "") -> Response:
    """Authenticated HTTP proxy for Browsertrix's screencast viewer."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            upstream = await client.get(f"{SCREENCAST_URL}/{path}")
        headers = {}
        content_type = upstream.headers.get("content-type")
        if content_type:
            headers["Content-Type"] = content_type
        content = upstream.content
        if not path and content_type and "text/html" in content_type:
            content = content.replace(
                b"</style>",
                b"body{margin:0;background:#151310;color:#d8cfc1}#content{align-items:flex-start;justify-content:center;gap:12px;padding:12px}#content img{max-width:100%;height:auto!important;margin:0!important;border-radius:8px}</style>",
            )
        return Response(content, status_code=upstream.status_code, headers=headers)
    except httpx.HTTPError as exc:
        return Response(f"Live view is not available: {exc}", status_code=503)


@app.websocket("/live/ws")
@app.websocket("/live/view/ws")
async def live_screencast_ws(websocket: WebSocket) -> None:
    origin = urlsplit(websocket.headers.get("origin", ""))
    if origin.scheme not in {"http", "https"} or origin.netloc != websocket.headers.get("host", ""):
        await websocket.close(code=4403)
        return
    if AUTH_ENABLED and session_store.validate(websocket.cookies.get(AUTH_COOKIE, "")) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    upstream_url = SCREENCAST_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    try:
        async with websockets.connect(upstream_url, max_size=16 * 1024 * 1024) as upstream:

            async def browser_to_upstream():
                while True:
                    message = await websocket.receive()
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("type") == "websocket.disconnect":
                        break

            async def upstream_to_browser():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            await asyncio.gather(browser_to_upstream(), upstream_to_browser())
    except (WebSocketDisconnect, WebSocketException, OSError):
        pass


@app.get("/api/archives")
async def list_archives() -> dict[str, Any]:
    try:
        collections = await asyncio.to_thread(get_collection_payloads)
        reusable = {
            job.get("collection")
            for job in job_store.load_all().values()
            if job.get("crawl_request")
        }
        for collection in collections:
            collection["can_rerun"] = collection["name"] in reusable
        return {"collections": collections}
    except Exception as exc:
        logger.exception("Failed to list archives")
        raise HTTPException(status_code=503, detail="Archive storage is unavailable.") from exc


@app.get("/api/collections")
async def list_collections_api() -> dict[str, Any]:
    try:
        collections = await asyncio.to_thread(get_collection_payloads)
        reusable = {
            job.get("collection")
            for job in job_store.load_all().values()
            if job.get("crawl_request")
        }
        return {
            "collections": [
                {
                    "name": c["name"],
                    "title": c["title"],
                    "file_count": c["file_count"],
                    "can_rerun": c["name"] in reusable,
                }
                for c in collections
            ]
        }
    except Exception as exc:
        logger.exception("Failed to list collections")
        raise HTTPException(status_code=503, detail="Archive storage is unavailable.") from exc


@app.post("/api/collections")
def create_collection(request: RenameCollectionRequest) -> dict[str, Any]:
    name = slugify(request.name)
    if not name:
        raise HTTPException(status_code=400, detail="Collection name is invalid.")
    existing = {collection["name"] for collection in get_collection_payloads()}
    if name in existing:
        raise HTTPException(status_code=409, detail="That collection already exists.")
    get_s3_client().put_object(
        Bucket=GARAGE_BUCKET,
        Key=f"{name}/.webvault-collection",
        Body=b"",
        ContentType="application/x-webvault-collection",
    )
    logger.info("Collection created: %s", name)
    return {"name": name, "message": f"Created '{name}'"}


def copy_optional_sidecar(s3: Any, old_key: str, new_key: str):
    sidecar = f"{old_key}.metadata.json"
    try:
        s3.head_object(Bucket=GARAGE_BUCKET, Key=sidecar)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    s3.copy_object(
        Bucket=GARAGE_BUCKET,
        Key=f"{new_key}.metadata.json",
        CopySource={"Bucket": GARAGE_BUCKET, "Key": sidecar},
    )
    return True


@app.post("/api/collections/{old_name}")
def rename_collection(old_name: str, request: RenameCollectionRequest) -> dict[str, Any]:
    new_name = slugify(request.name)
    if not new_name:
        raise HTTPException(status_code=400, detail="New collection name is invalid.")
    if new_name == old_name:
        return {"name": new_name, "message": "No change needed"}

    all_keys = list_all_wacz_keys()
    to_move = [k for k in all_keys if k["group"] == old_name]
    if not to_move and old_name not in list_collection_markers():
        raise HTTPException(status_code=404, detail=f"No collection named '{old_name}' was found.")

    existing_keys = {k["key"] for k in all_keys}
    collisions = [k["name"] for k in to_move if f"{new_name}/{k['name']}" in existing_keys]
    if collisions:
        raise HTTPException(
            status_code=409,
            detail=f"Destination already contains: {', '.join(collisions)}",
        )

    s3 = get_s3_client()
    for k in to_move:
        new_key = f"{new_name}/{k['name']}"
        s3.copy_object(
            Bucket=GARAGE_BUCKET, Key=new_key, CopySource={"Bucket": GARAGE_BUCKET, "Key": k["key"]}
        )
        copied_sidecar = copy_optional_sidecar(s3, k["key"], new_key)
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=k["key"])
        if copied_sidecar:
            s3.delete_object(Bucket=GARAGE_BUCKET, Key=f"{k['key']}.metadata.json")
        logger.info("Renamed %s → %s", k["key"], new_key)

    for job_id, job in job_store.load_all().items():
        if job.get("collection") != old_name:
            continue
        crawl_request = {**(job.get("crawl_request") or {}), "collection": new_name}
        changes: dict[str, Any] = {
            "collection": new_name,
            "crawl_request": crawl_request,
        }
        archive_url = str(job.get("archive_url") or "")
        if archive_url.startswith(f"/replay/{quote(old_name)}/"):
            changes["archive_url"] = archive_url.replace(
                f"/replay/{quote(old_name)}/",
                f"/replay/{quote(new_name)}/",
                1,
            )
        job_store.update(job_id, **changes)

    s3.put_object(
        Bucket=GARAGE_BUCKET,
        Key=f"{new_name}/.webvault-collection",
        Body=b"",
        ContentType="application/x-webvault-collection",
    )
    s3.delete_object(Bucket=GARAGE_BUCKET, Key=f"{old_name}/.webvault-collection")
    logger.info("Collection renamed: %s → %s (%d files)", old_name, new_name, len(to_move))
    return {"name": new_name, "files": len(to_move), "message": f"Renamed to '{new_name}'"}


@app.delete("/api/collections/{name}")
def delete_collection(name: str) -> dict[str, Any]:
    all_keys = list_all_wacz_keys()
    to_delete = [k for k in all_keys if k["group"] == name]
    has_marker = name in list_collection_markers()
    if not to_delete and not has_marker:
        raise HTTPException(status_code=404, detail=f"No collection named '{name}' was found.")

    s3 = get_s3_client()
    for k in to_delete:
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=k["key"])
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=f"{k['key']}.metadata.json")
        logger.info("Deleted %s", k["key"])

    s3.delete_object(Bucket=GARAGE_BUCKET, Key=f"{name}/.webvault-collection")
    deleted_names = {item["name"] for item in to_delete}
    for job_id, job in job_store.load_all().items():
        if job.get("collection") == name and (
            job.get("archive_filename") in deleted_names or not job.get("archive_filename")
        ):
            job_store.update(
                job_id,
                archive_url="",
                archive_deleted=True,
                message="Archive deleted from object storage.",
            )

    logger.info("Collection deleted: %s (%d files)", name, len(to_delete))
    return {"deleted": len(to_delete), "message": f"Deleted {len(to_delete)} files from '{name}'"}


def validate_archive_identifier(collection: str, filename: str):
    if slugify(collection) != collection:
        raise HTTPException(status_code=400, detail="Collection name is invalid.")
    if Path(filename).name != filename or not filename.endswith(".wacz"):
        raise HTTPException(status_code=400, detail="Archive filename is invalid.")


@app.post("/api/files/{collection}/{filename}")
def move_file(collection: str, filename: str, request: MoveFileRequest) -> dict[str, Any]:
    validate_archive_identifier(collection, filename)
    new_collection = slugify(request.collection)
    if not new_collection:
        raise HTTPException(status_code=400, detail="New collection name is invalid.")

    if new_collection == collection:
        return {"moved": False, "from": collection, "to": collection, "file": filename}

    s3 = get_s3_client()
    old_key = f"{collection}/{filename}"
    new_key = f"{new_collection}/{filename}"

    try:
        # S3 copy silently replaces an existing object; prevent archive loss.
        destination = s3.list_objects_v2(Bucket=GARAGE_BUCKET, Prefix=new_key)
        if any(obj.get("Key") == new_key for obj in destination.get("Contents", [])):
            raise HTTPException(
                status_code=409,
                detail=f"'{new_collection}' already contains '{filename}'.",
            )
        s3.copy_object(
            Bucket=GARAGE_BUCKET, Key=new_key, CopySource={"Bucket": GARAGE_BUCKET, "Key": old_key}
        )
        copied_sidecar = copy_optional_sidecar(s3, old_key, new_key)
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=old_key)
        if copied_sidecar:
            s3.delete_object(Bucket=GARAGE_BUCKET, Key=f"{old_key}.metadata.json")
        for job_id, job in job_store.load_all().items():
            if job.get("collection") == collection and job.get("archive_filename") == filename:
                crawl_request = {**(job.get("crawl_request") or {}), "collection": new_collection}
                job_store.update(
                    job_id,
                    collection=new_collection,
                    crawl_request=crawl_request,
                    archive_key=new_key,
                    archive_url=f"/replay/{quote(new_collection)}/{quote(filename)}",
                )
        logger.info("Moved file %s → %s", old_key, new_key)
        return {"moved": True, "from": collection, "to": new_collection, "file": filename}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to move file")
        raise HTTPException(status_code=503, detail="Failed to move file.") from exc


@app.delete("/api/files/{collection}/{filename}")
def delete_file(collection: str, filename: str) -> dict[str, Any]:
    validate_archive_identifier(collection, filename)
    s3 = get_s3_client()
    key = f"{collection}/{filename}"

    try:
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=key)
        s3.delete_object(Bucket=GARAGE_BUCKET, Key=f"{key}.metadata.json")
        for job_id, job in job_store.load_all().items():
            if job.get("collection") == collection and job.get("archive_filename") == filename:
                job_store.update(
                    job_id,
                    archive_url="",
                    archive_deleted=True,
                    message="Archive deleted from object storage.",
                )
        logger.info("Deleted file %s", key)
        return {"deleted": True, "file": filename}
    except Exception as exc:
        logger.exception("Failed to delete file")
        raise HTTPException(status_code=503, detail="Failed to delete file.") from exc


def absolute_route_url(request: Request, route_name: str, **path_params: str) -> str:
    return str(request.url_for(route_name, **path_params))


def replay_initial_url(collection: str, filename: str) -> str:
    for job in job_store.load_all().values():
        if job.get("collection") != collection or job.get("archive_filename") != filename:
            continue
        seed = str(job.get("primary_seed") or "").strip()
        if not seed:
            metadata = job.get("archive_metadata")
            if isinstance(metadata, dict):
                seed = str(metadata.get("seed_url") or "").strip()
        if not seed:
            crawl_request = job.get("crawl_request")
            if isinstance(crawl_request, dict):
                seeds = crawl_request.get("seeds")
                if isinstance(seeds, list) and seeds:
                    seed = str(seeds[0]).strip()
        if seed.startswith(("http://", "https://")):
            return seed
    return ""


@app.get("/replay/{collection}/{filename}", response_class=HTMLResponse)
async def replay_archive(collection: str, filename: str, request: Request) -> HTMLResponse:
    source_url = absolute_route_url(
        request,
        "replay_wacz",
        collection=collection,
        filename=filename,
    )
    try:
        version_token = await asyncio.to_thread(get_archive_version_token, collection, filename)
        source_url = f"{source_url}?v={quote(version_token, safe='')}"
    except Exception as exc:
        logger.warning(
            "Could not build replay version token for %s/%s: %s", collection, filename, exc
        )
    # Use the capture's known seed when available so ReplayWeb opens the site
    # directly. Legacy/imported WACZ files without stored metadata still fall
    # back to ReplayWeb's captured-pages index without downloading the archive
    # just to inspect it here.
    initial_url = replay_initial_url(collection, filename)
    return mark_uncached(
        render_replay_wrapper_page(
            source=source_url,
            title=friendly_title(Path(filename).stem),
            back_href=f"/archive/{quote(collection)}",
            back_label=friendly_title(collection),
            download_href=f"/download/{quote(collection)}/{quote(filename)}",
            initial_url=initial_url,
        )
    )


@app.get("/replay/")
async def replay_index(request: Request) -> HTMLResponse:
    source = request.query_params.get("source", "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="Missing source query parameter.")
    parsed_source = urlsplit(source)
    if (
        parsed_source.scheme not in {"http", "https"}
        or parsed_source.netloc != request.url.netloc
        or not parsed_source.path.startswith("/replay-wacz/")
    ):
        raise HTTPException(status_code=400, detail="Invalid replay source.")

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
        size = await asyncio.to_thread(get_archive_size, collection, filename)
    except Exception as exc:
        raise archive_http_exception(exc) from exc

    return Response(
        media_type="application/wacz",
        headers=uncached_headers(
            {
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes",
                "Content-Length": str(size),
            }
        ),
    )


@app.get("/replay-wacz/{collection}/{filename}")
async def replay_wacz(request: Request, collection: str, filename: str) -> Response:
    try:
        size = await asyncio.to_thread(get_archive_size, collection, filename)
    except Exception as exc:
        raise archive_http_exception(exc) from exc

    range_header = request.headers.get("range")
    if range_header:
        try:
            start, end = parse_range_header(range_header, size)
            body = await asyncio.to_thread(read_archive_range, collection, filename, start, end)
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
            raise archive_http_exception(exc) from exc

        return Response(
            content=body,
            status_code=206,
            media_type="application/wacz",
            headers=uncached_headers(
                {
                    "Access-Control-Allow-Origin": "*",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                }
            ),
        )

    try:
        body = await asyncio.to_thread(open_archive_stream, collection, filename)
    except Exception as exc:
        raise archive_http_exception(exc) from exc

    def stream_body():
        try:
            yield from body.iter_chunks(chunk_size=1024 * 1024)
        finally:
            body.close()

    return StreamingResponse(
        stream_body(),
        media_type="application/wacz",
        headers=uncached_headers(
            {
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes",
                "Content-Length": str(size),
            }
        ),
    )


@app.get("/download/{collection}/{filename}")
def download_archive(collection: str, filename: str) -> StreamingResponse:
    key = f"{collection}/{filename}"
    try:
        response = get_s3_client().get_object(Bucket=GARAGE_BUCKET, Key=key)
    except Exception as exc:
        raise archive_http_exception(exc) from exc

    body = response["Body"]

    def stream_body():
        try:
            yield from body.iter_chunks(chunk_size=1024 * 1024)
        finally:
            body.close()

    safe_filename = Path(filename).name.replace('"', "")
    return StreamingResponse(
        stream_body(),
        media_type="application/wacz",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
