from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import PACKAGE_DIR, settings
from .utils import friendly_title

TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATES = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(("html", "xml")),
)


def render_template(name: str, status_code: int = 200, **context):
    template = TEMPLATES.get_template(name)
    context.setdefault("csrf_token", "")
    return HTMLResponse(template.render(**context), status_code=status_code)


def render_home_page(csrf_token: str = ""):
    return render_template(
        "home.html",
        title="WebVault · Archive the living web",
        body_class="home-page",
        auth_enabled=settings.auth_enabled,
        csrf_token=csrf_token,
    )


def render_collection_page(
    collection: str,
    files: list[dict],
    unique_files: list[dict] | None = None,
    can_rerun: bool = False,
    csrf_token: str = "",
):
    collection_title = friendly_title(collection)
    return render_template(
        "collection.html",
        title=f"{collection_title} · WebVault",
        body_class="collection-page",
        collection=collection,
        collection_title=collection_title,
        files=files,
        unique_files=unique_files if unique_files is not None else files,
        can_rerun=can_rerun,
        csrf_token=csrf_token,
    )


def render_live_page(csrf_token: str = ""):
    return render_template(
        "live.html",
        title="Live capture · WebVault",
        body_class="live-page",
        csrf_token=csrf_token,
    )


def render_live_control_page(csrf_token: str = ""):
    return render_template(
        "live_control.html",
        title="Interactive crawl · WebVault",
        body_class="live-control-page",
        csrf_token=csrf_token,
    )


def render_profiles_page(csrf_token: str = ""):
    return render_template(
        "profiles.html",
        title="Browser login profiles · WebVault",
        body_class="profiles-page",
        csrf_token=csrf_token,
    )


def render_capture_page(capture: dict, history: list[dict], csrf_token: str = ""):
    metadata = capture.get("archive_metadata") or {}
    page_title = metadata.get("title") or capture.get("seed_url") or "Capture"
    return render_template(
        "capture.html",
        title=f"{page_title} · WebVault",
        body_class="capture-detail-page",
        capture=capture,
        history=history,
        page_title=page_title,
        csrf_token=csrf_token,
    )


def render_stats_page(csrf_token: str = ""):
    return render_template(
        "stats.html",
        title="Server statistics · WebVault",
        body_class="stats-page",
        csrf_token=csrf_token,
    )


def render_login_page(error: str = "", next_url: str = "/", username: str = ""):
    return render_template(
        "login.html",
        title="Sign in · WebVault",
        body_class="login-page",
        error=error,
        next_url=next_url,
        username=username,
        status_code=401 if error else 200,
    )


def render_replay_page(source: str, title: str, initial_url: str = ""):
    return render_template(
        "replay.html",
        title=title,
        source=source,
        initial_url=initial_url,
    )


def render_replay_wrapper_page(
    source: str,
    title: str,
    back_href: str,
    back_label: str,
    download_href: str,
    initial_url: str = "",
):
    return render_template(
        "replay_wrapper.html",
        title=f"{title} · Replay",
        body_class="replay-wrapper-page",
        replay_title=title,
        back_href=back_href,
        back_label=back_label,
        download_href=download_href,
        source=source,
        initial_url=initial_url,
    )


def mark_uncached(response: HTMLResponse):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def uncached_headers(extra: dict[str, str] | None = None):
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if extra:
        headers.update(extra)
    return headers
