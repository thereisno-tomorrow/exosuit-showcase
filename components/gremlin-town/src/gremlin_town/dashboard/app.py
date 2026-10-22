"""Standalone owner listener. No Brain runtime is created by this module."""
import logging
from pathlib import Path
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .auth import AuthService, SESSION_SECONDS, VerifiedOwner
from .settings import DashboardSettings
from .store import AuthRejected, AuthStore

logger = logging.getLogger(__name__)
COOKIE = "__Host-dashboard"
BROWSER_COOKIE = "__Host-dashboard-browser"
STATIC = Path(__file__).with_name("static")
ANONYMOUS_AUTH_PATHS = frozenset({
    "/auth/context", "/auth/sign-in/options", "/auth/sign-in/verify",
    "/auth/enrol/options", "/auth/enrol/verify",
})
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "img-src 'self'; connect-src 'self'; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "Permissions-Policy": "publickey-credentials-get=(self), publickey-credentials-create=(self)",
    "Strict-Transport-Security": "max-age=31536000",
}


def require_owner(request: Request) -> VerifiedOwner:
    auth: AuthService = request.app.state.dashboard_auth
    try:
        return auth.owner(request.cookies.get(COOKIE))
    except AuthRejected:
        raise HTTPException(401, "Sign in to open your day.") from None


def _origin(request: Request, auth: AuthService):
    if request.headers.get("origin") != auth.settings.origin:
        raise HTTPException(403, "Open this page again and try once more.")


def require_write_owner(request: Request) -> VerifiedOwner:
    """Authentication/CSRF only. Callers must also enforce their feature gate."""
    owner = require_owner(request)
    auth: AuthService = request.app.state.dashboard_auth
    _origin(request, auth)
    try:
        auth.verify_session_csrf(owner, request.headers.get("x-csrf-token", ""))
    except AuthRejected:
        raise HTTPException(403, "Open this page again and try once more.") from None
    return owner


def _browser(request: Request, auth: AuthService) -> str:
    _origin(request, auth)
    token = request.cookies.get(BROWSER_COOKIE, "")
    try:
        auth.verify_browser(token, request.headers.get("x-csrf-token", ""))
    except AuthRejected:
        raise HTTPException(403, "Open this page again and try once more.") from None
    return token


async def _json(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(400, "The sign-in request could not be read.")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "The sign-in request could not be read.") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "The sign-in request could not be read.")
    return body


def _cookie(response, name: str, value: str, age: int):
    response.set_cookie(name, value, max_age=age, path="/", secure=True, httponly=True, samesite="strict")


def create_app(settings: DashboardSettings, *, store: AuthStore | None = None, clock=time.time,
               router: APIRouter | None = None) -> FastAPI:
    settings.validate()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dashboard_settings = settings
    if not settings.enabled:
        return app
    auth = AuthService(settings, store=store, clock=clock)
    app.state.dashboard_auth = auth
    expected_host = urlsplit(settings.origin).netloc

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.headers.get("host") != expected_host:
            response = JSONResponse({"detail": "This address is not available."}, status_code=400)
        else:
            try:
                size = 0
                pieces = []
                async for piece in request.stream():
                    size += len(piece)
                    if size > 65_536:
                        raise HTTPException(413, "This request is too large.")
                    pieces.append(piece)
                request._body = b"".join(pieces)
                # Anonymous ceremony traffic must never prevent an authenticated
                # owner from revoking their session. Logout has its own auth,
                # exact-Origin and CSRF checks below, outside this shared bucket.
                if (request.url.path in ANONYMOUS_AUTH_PATHS
                        and not auth.store.rate_limit(auth.now())):
                    raise HTTPException(429, "Please wait a minute before trying again.")
                response = await call_next(request)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
            except Exception as error:
                # A failed database/file operation is a visible service failure,
                # never a successful login. Do not log attacker-controlled data.
                logger.error("Dashboard request failed (%s); operator inspection required", type(error).__name__)
                response = JSONResponse({"detail": "Couldn't open the dashboard. Please try later."}, status_code=503)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login():
        return FileResponse(STATIC / "login.html", media_type="text/html")

    @app.get("/enrol", response_class=HTMLResponse)
    def enrol_page():
        return FileResponse(STATIC / "login.html", media_type="text/html")

    @app.get("/auth-assets/login.js")
    def script():
        return FileResponse(STATIC / "login.js", media_type="application/javascript")

    @app.get("/auth-assets/login.css")
    def stylesheet():
        return FileResponse(STATIC / "login.css", media_type="text/css")

    @app.get("/auth/context")
    def context(request: Request):
        browser, csrf = auth.browser_context(request.cookies.get(BROWSER_COOKIE))
        response = JSONResponse({"csrf": csrf})
        _cookie(response, BROWSER_COOKIE, browser, 300)
        return response

    @app.post("/auth/{kind}/options")
    async def options(kind: str, request: Request):
        if kind not in ("enrol", "sign-in"):
            raise HTTPException(404)
        browser = _browser(request, auth)
        body = await _json(request)
        try:
            return auth.options(kind, browser, token=body.get("token"))
        except AuthRejected:
            auth.store.audit(auth.now(), "enrollment_failed" if kind == "enrol" else "sign_in_failed")
            raise HTTPException(401, "Couldn't sign in. Open a new sign-in or enrolment link.") from None

    @app.post("/auth/{kind}/verify")
    async def verify(kind: str, request: Request):
        if kind not in ("enrol", "sign-in"):
            raise HTTPException(404)
        browser = _browser(request, auth)
        body = await _json(request)
        try:
            session = auth.verify(kind, browser, body)
        except AuthRejected:
            raise HTTPException(401, "Couldn't sign in. Please try again.") from None
        # A new login always rotates the caller's previous session.
        old = request.cookies.get(COOKIE)
        if old:
            try:
                previous = auth.owner(old)
            except AuthRejected:
                previous = None
            if previous:
                auth.store.logout(previous.session_id)
        response = JSONResponse({"ok": True})
        _cookie(response, COOKIE, session, SESSION_SECONDS)
        return response

    @app.get("/auth/session")
    def session(request: Request):
        owner = require_owner(request)
        return {"authenticated": True, "csrf": auth.session_csrf(owner)}

    @app.post("/auth/logout")
    def logout(request: Request):
        owner = require_write_owner(request)
        auth.logout(owner)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    if router is not None:
        # Data/UI routers must apply require_owner/require_write_owner to every
        # route and their own default-off feature gates. They carry no auth code.
        app.include_router(router, dependencies=[Depends(require_owner)])

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request):
        try:
            require_owner(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        if settings.reads and router is not None:
            return RedirectResponse("/app", status_code=303)
        return "<!doctype html><title>My day</title><p>Your dashboard is being prepared.</p>"

    return app
