"""Application factory for authenticated API and read-only HTML views."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import secrets
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from urllib.parse import parse_qs

from gremlin_town.api.auth import ActorAuthenticator, AuthenticationError, new_session
from gremlin_town.api.commands import InvalidCommand
from gremlin_town.api.errors import ClientHttpError
from gremlin_town.api.routes import router
from gremlin_town.contracts import Actor, GremlinTownService
from gremlin_town.views.routes import install_view_routes


def create_app(
    service: GremlinTownService,
    *,
    actor_resolver: Callable[[str], Actor | None] | Mapping[str, Actor | str] | None = None,
    login_resolver: Callable[[str, str], Actor | None] | Mapping[str, tuple[str, Actor]] | None = None,
    session_ttl_seconds: int = 8 * 60 * 60,
    title: str = "Gremlin Town",
) -> FastAPI:
    """Build an app around a service port; no lifecycle logic lives here."""

    app = FastAPI(title=title, version="1", docs_url="/api/docs", redoc_url=None)
    app.state.service = service
    app.state.actor_authenticator = ActorAuthenticator(actor_resolver)
    app.state.login_resolver = login_resolver
    app.state.session_ttl_seconds = session_ttl_seconds
    app.state.sessions: dict[str, tuple[Actor, datetime]] = {}
    app.state.session_cookie_name = "gremlin_town_session"
    app.include_router(router)
    install_view_routes(app)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page() -> str:
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Gremlin Town login</title></head><body><main><h1>Gremlin Town login</h1>'
                '<form method="post" action="/login" autocomplete="on"><label for="username">Username</label>'
                '<input id="username" name="username" autocomplete="username" required>'
                '<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>'
                '<button type="submit">Sign in</button></form></main></body></html>')

    @app.post("/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        resolver = request.app.state.login_resolver
        try:
            fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
            username = fields.get("username", [""])[0]
            password = fields.get("password", [""])[0]
        except (UnicodeDecodeError, AttributeError):
            username = password = ""
        actor = None
        if callable(resolver):
            actor = resolver(username, password)
        elif isinstance(resolver, Mapping):
            entry = resolver.get(username)
            if entry and secrets.compare_digest(password, entry[0]):
                actor = entry[1]
        if actor is None or not actor.is_operator:
            return JSONResponse(status_code=401, content={"error": {"code": "UNAUTHORIZED", "message": "authentication required"}})
        session_id, expires_at = new_session(actor, ttl_seconds=request.app.state.session_ttl_seconds)
        request.app.state.sessions[session_id] = (actor, expires_at)
        response = RedirectResponse("/overview", status_code=303)
        response.set_cookie(request.app.state.session_cookie_name, session_id, max_age=request.app.state.session_ttl_seconds, httponly=True, secure=True, samesite="lax")
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> RedirectResponse:
        session_id = request.cookies.get(request.app.state.session_cookie_name)
        if session_id:
            request.app.state.sessions.pop(session_id, None)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            request.app.state.session_cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.exception_handler(AuthenticationError)
    async def authentication_error(_: Request, __: AuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "UNAUTHORIZED", "message": "authentication required"}},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(ClientHttpError)
    async def client_error(_: Request, exc: ClientHttpError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(InvalidCommand)
    async def invalid_command(_: Request, exc: InvalidCommand) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "INVALID_INPUT", "message": str(exc)}},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "INVALID_INPUT", "message": "invalid request"}},
        )

    return app
