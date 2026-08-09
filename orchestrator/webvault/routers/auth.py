import base64
import secrets
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..config import settings
from ..sessions import SESSION_TTL_SECONDS
from ..state import session_store
from ..views import render_login_page

router = APIRouter()
AUTH_COOKIE = "webvault_session"
AUTH_ENABLED = settings.auth_enabled
WEBVAULT_USERNAME = settings.webvault_username
WEBVAULT_PASSWORD = settings.webvault_password


async def session_auth(request: Request, call_next):
    """Authenticate browser sessions and enforce per-session CSRF tokens."""
    public_asset = request.url.path.startswith(("/img/", "/static/"))
    if not AUTH_ENABLED or request.url.path == "/login" or public_asset:
        return await call_next(request)

    token = request.cookies.get(AUTH_COOKIE, "")
    expected_csrf_token = session_store.validate(token)
    if expected_csrf_token is not None:
        request.state.csrf_token = expected_csrf_token
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf_token = request.headers.get("x-csrf-token", "")
            if not csrf_token and request.headers.get("content-type", "").startswith(
                "application/x-www-form-urlencoded"
            ):
                fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))
                csrf_token = fields.get("csrf_token", [""])[0]
            if not secrets.compare_digest(csrf_token, expected_csrf_token):
                if request.url.path.startswith("/api/"):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF validation failed."},
                    )
                return Response(status_code=403, content="CSRF validation failed.")
        return await call_next(request)

    # Basic Auth remains supported for scripts and API clients.
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            username, password = base64.b64decode(header[6:]).decode().split(":", 1)
            if secrets.compare_digest(username, WEBVAULT_USERNAME) and secrets.compare_digest(
                password, WEBVAULT_PASSWORD
            ):
                return await call_next(request)
        except (ValueError, UnicodeDecodeError):
            pass

    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Authentication required."})
    target = request.url.path
    if request.url.query:
        target += f"?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    if session_store.validate(request.cookies.get(AUTH_COOKIE, "")) is not None:
        return RedirectResponse("/", status_code=303)
    return render_login_page(next_url=request.query_params.get("next", "/"))


@router.post("/login")
async def login(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    username = fields.get("username", [""])[0]
    password = fields.get("password", [""])[0]
    next_url = fields.get("next", ["/"])[0]
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    if not (
        secrets.compare_digest(username, WEBVAULT_USERNAME)
        and secrets.compare_digest(password, WEBVAULT_PASSWORD)
    ):
        return render_login_page("Incorrect username or password.", next_url, username=username)

    session_token, _csrf_token = session_store.create()
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        AUTH_COOKIE,
        session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    session_store.revoke(request.cookies.get(AUTH_COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE, httponly=True, samesite="lax")
    return response
