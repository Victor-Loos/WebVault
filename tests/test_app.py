import importlib.util
import re
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from webvault.routers import auth as auth_routes
from webvault.routers import history as history_routes
from webvault.routers import jobs as jobs_routes

PROJECT_DIR = Path(__file__).parents[1]

SPEC = importlib.util.spec_from_file_location(
    "webvault_app", PROJECT_DIR / "orchestrator" / "app.py"
)
assert SPEC is not None
assert SPEC.loader is not None
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def test_range_parser():
    assert app.parse_range_header("bytes=0-9", 100) == (0, 9)
    assert app.parse_range_header("bytes=90-", 100) == (90, 99)
    assert app.parse_range_header("bytes=-10", 100) == (90, 99)
    assert app.parse_range_header("bytes=0-999", 100) == (0, 99)


@pytest.mark.parametrize("value", ["items=0-1", "bytes=100-101", "bytes=5-4", "bytes=-0"])
def test_invalid_ranges(value):
    with pytest.raises(ValueError):
        app.parse_range_header(value, 100)


def test_seed_normalization_and_validation():
    assert app.normalize_seed_url("example.com") == "https://example.com"
    assert app.normalize_seed_url("  ") == ""
    with pytest.raises(ValueError):
        app.normalize_seed_url("file:///etc/passwd")


def test_seed_target_blocks_private_addresses_and_credentials(monkeypatch):
    def private_address(*_args, **_kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr("webvault.utils.socket.getaddrinfo", private_address)
    with pytest.raises(ValueError, match="Private"):
        app.validate_seed_target("http://internal.example")
    with pytest.raises(ValueError, match="credentials"):
        app.validate_seed_target("https://user:secret@example.com")


def test_seed_target_accepts_only_global_resolution(monkeypatch):
    def public_address(*_args, **_kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("webvault.utils.socket.getaddrinfo", public_address)
    assert app.validate_seed_target("example.com") == "https://example.com"


def test_generated_job_names_are_unique():
    names = {app.suggest_browsertrix_name(["https://example.com/docs"]) for _ in range(10)}
    assert len(names) == 10


def test_app_routes_register_and_home_page_loads():
    with TestClient(app.app) as client:
        response = client.get("/")
        stylesheet = client.get("/static/app.css")
        script = client.get("/static/home.js")
    assert response.status_code == 200
    assert "Preserve what matters" in response.text
    assert 'src="/static/home.js"' in response.text
    assert stylesheet.status_code == 200
    assert script.status_code == 200


def test_route_manifest_preserves_public_contract():
    schema_paths = app.app.openapi()["paths"]
    routes = {
        (method.upper(), path) for path, operations in schema_paths.items() for method in operations
    }
    expected = {
        ("GET", "/"),
        ("GET", "/api/archives"),
        ("GET", "/api/jobs"),
        ("GET", "/api/collections"),
        ("POST", "/api/crawl"),
        ("POST", "/api/jobs/{job_id}/rerun"),
        ("POST", "/api/jobs/{job_id}/cancel"),
        ("GET", "/archive/{collection}"),
        ("GET", "/replay/{collection}/{filename}"),
        ("GET", "/replay-wacz/{collection}/{filename}"),
        ("HEAD", "/replay-wacz/{collection}/{filename}"),
        ("GET", "/download/{collection}/{filename}"),
    }
    assert expected <= routes


def test_live_websocket_rejects_cross_origin_clients():
    with TestClient(app.app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/live/ws",
                headers={"origin": "https://evil.example"},
            ):
                pass
    assert rejected.value.code == 4403


def test_replay_rejects_attacker_controlled_sources():
    with TestClient(app.app) as client:
        external = client.get("/replay/", params={"source": "https://evil.example/a.wacz"})
        data_url = client.get("/replay/", params={"source": "data:text/html,attack"})
    assert external.status_code == 400
    assert data_url.status_code == 400


def test_absolute_route_url_honors_forwarded_proto():
    from starlette.requests import Request

    def make_request(forwarded_proto: str | None):
        headers = [(b"host", b"testserver")]
        if forwarded_proto:
            headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
        scope = {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
            "headers": headers,
            "app": app.app,
            "router": app.app.router,
        }
        return Request(scope)

    plain = app.absolute_route_url(
        make_request(None), "replay_wacz", collection="docs", filename="a.wacz"
    )
    forwarded = app.absolute_route_url(
        make_request("https"), "replay_wacz", collection="docs", filename="a.wacz"
    )
    assert plain.startswith("http://testserver/replay-wacz/docs/a.wacz")
    assert forwarded.startswith("https://testserver/replay-wacz/docs/a.wacz")


def test_delete_capture_removes_archive_and_record(monkeypatch, tmp_path):
    jobs = {
        "capture-1": {
            "job_id": "capture-1",
            "status": "completed",
            "collection": "docs",
            "archive_filename": "capture-1.wacz",
            "archive_key": "docs/capture-1.wacz",
            "primary_seed": "https://example.com/docs",
        }
    }
    deleted_objects = []

    class FakeS3:
        def delete_object(self, **kwargs):
            deleted_objects.append(kwargs["Key"])

    monkeypatch.setattr(history_routes.job_store, "load_all", lambda: jobs)
    monkeypatch.setattr(history_routes.job_store, "delete", lambda job_id: jobs.pop(job_id))
    monkeypatch.setattr(history_routes, "get_s3_client", FakeS3)
    monkeypatch.setattr(history_routes.settings, "crawl_output_dir", tmp_path)

    with TestClient(app.app) as client:
        response = client.delete("/api/captures/capture-1")

    assert response.status_code == 200
    assert response.json()["next_url"] == "/#library"
    assert jobs == {}
    assert deleted_objects == ["docs/capture-1.wacz", "docs/capture-1.wacz.metadata.json"]


def test_replay_uses_capture_seed_as_initial_url(monkeypatch):
    monkeypatch.setattr(
        app.job_store,
        "load_all",
        lambda: {
            "capture": {
                "collection": "docs",
                "archive_filename": "docs.wacz",
                "primary_seed": "https://example.com/docs",
            }
        },
    )
    assert app.replay_initial_url("docs", "docs.wacz") == "https://example.com/docs"
    assert app.replay_initial_url("docs", "legacy.wacz") == ""


def test_cookie_sessions_require_csrf_for_mutations(monkeypatch):
    monkeypatch.setattr(auth_routes, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_routes, "WEBVAULT_USERNAME", "admin")
    monkeypatch.setattr(auth_routes, "WEBVAULT_PASSWORD", "secret")
    with TestClient(app.app, follow_redirects=False) as client:
        assert 'name="csrf-token" content=""' in client.get("/login").text
        login = client.post(
            "/login",
            data={"username": "admin", "password": "secret", "next": "/"},
        )
        assert login.status_code == 303
        assert client.get("/static/app.css").status_code == 200
        home = client.get("/")
        csrf_match = re.search(r'name="csrf-token" content="([^"]+)"', home.text)
        assert csrf_match is not None
        csrf_token = csrf_match.group(1)
        assert csrf_token
        assert client.post("/logout").status_code == 403
        response = client.post(
            "/logout",
            headers={"x-csrf-token": csrf_token},
        )
        assert response.status_code == 303
        assert client.get("/").status_code == 303


def test_home_page_has_unique_ids_and_no_inline_click_handlers():
    html = app.render_home_page().body.decode()
    element_ids = re.findall(r'\sid="([^"]+)"', html)

    assert len(element_ids) == len(set(element_ids))
    assert "onclick=" not in html
    assert 'id="collectionSuggestions"' in html
    assert 'id="actionDialog"' in html


def test_collection_template_escapes_archive_metadata():
    html = app.render_collection_page(
        "research",
        [
            {
                "title": "Unsafe <script>alert(1)</script>",
                "description": "Preserved & portable",
                "name": "capture file.wacz",
                "size": "12 KB",
                "seed_url": "https://example.com/?a=1&b=2",
            }
        ],
    ).body.decode()

    assert "<script>alert(1)</script>" not in html
    assert "Unsafe &lt;script&gt;" in html
    assert "/static/collection.js" in html
    assert "capture%20file.wacz" in html


def test_crawl_budget_defaults_are_bounded():
    config = app.CrawlConfig.model_validate(
        {"seeds": ["https://example.com"], "pageLimit": 0, "timeLimit": 0}
    )
    assert config.pageLimit == 1_000
    assert config.timeLimit == 3_600
    with pytest.raises(ValueError, match="2048"):
        app.CrawlConfig.model_validate({"seeds": [f"https://example.com/{'x' * 2048}"]})


def test_crawl_validation_uses_http_error_status():
    response = TestClient(app.app).post(
        "/api/crawl",
        json={"seeds": ["file:///etc/passwd"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Invalid URL")


def test_queue_and_cancel_job_without_docker_access(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_routes, "CRAWL_DIR", tmp_path)
    monkeypatch.setattr(
        "webvault.services.crawls.validate_seed_target",
        lambda seed, _allow_private: app.normalize_seed_url(seed),
    )
    with TestClient(app.app) as client:
        queued = client.post(
            "/api/crawl",
            json={"seeds": ["https://example.com"], "collection": "research"},
        )
        assert queued.status_code == 202
        job_id = queued.json()["job_id"]
        persisted = app.job_store.load_all()[job_id]
        assert persisted["status"] == "queued"
        assert (tmp_path / f"{job_id}.yaml").is_file()

        cancelled = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert app.job_store.load_all()[job_id]["status"] == "cancelled"
    app.job_store.delete(job_id)


def test_crawl_queue_capacity_returns_429(monkeypatch):
    monkeypatch.setattr(app.settings, "max_queued_jobs", 0)
    monkeypatch.setattr(
        "webvault.services.crawls.validate_seed_target",
        lambda seed, _allow_private: app.normalize_seed_url(seed),
    )
    response = TestClient(app.app).post(
        "/api/crawl",
        json={"seeds": ["https://example.com"]},
    )
    assert response.status_code == 429


def test_typed_collection_request_rejects_invalid_body():
    response = TestClient(app.app).post("/api/collections/old", json={})
    assert response.status_code == 422


def test_archive_errors_do_not_expose_storage_details():
    missing = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "secret backend detail"}},
        "HeadObject",
    )
    unavailable = RuntimeError("secret backend detail")

    missing_response = app.archive_http_exception(missing)
    unavailable_response = app.archive_http_exception(unavailable)

    assert (missing_response.status_code, missing_response.detail) == (
        404,
        "Archive not found.",
    )
    assert (unavailable_response.status_code, unavailable_response.detail) == (
        503,
        "Archive storage is unavailable.",
    )
