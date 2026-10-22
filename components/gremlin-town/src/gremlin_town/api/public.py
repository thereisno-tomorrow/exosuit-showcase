"""Narrow public ASGI surface for the enrolled Grok client.

This app deliberately reuses the canonical client endpoints while registering
only the ten participant operations. It has no browser, operator, health,
documentation, or recovery routes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from gremlin_town.api.auth import ActorAuthenticator, AuthenticationError
from gremlin_town.api.commands import InvalidCommand
from gremlin_town.api.errors import ClientHttpError
from gremlin_town.api.routes import router as canonical_router
from gremlin_town.contracts import Actor, GremlinTownService


PUBLIC_OPERATION_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/participants"),
        ("POST", "/v1/requests"),
        ("GET", "/v1/requests"),
        ("GET", "/v1/requests/{request_id}"),
        ("POST", "/v1/requests/{request_id}/reports"),
        ("POST", "/v1/requests/{request_id}/replies"),
        ("POST", "/v1/requests/{request_id}/cancel"),
        ("POST", "/v1/requests/{request_id}/check"),
        ("GET", "/v1/operations/{operation_id}"),
        ("GET", "/v1/events"),
    }
)


def _public_router() -> APIRouter:
    router = APIRouter()
    for route in canonical_router.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = {method.upper() for method in route.methods or ()}
        selected = methods & {method for method, _ in PUBLIC_OPERATION_ROUTES if _ == route.path}
        if not selected:
            continue
        router.add_api_route(
            route.path,
            route.endpoint,
            methods=sorted(selected),
            name=f"public_{route.name}",
            tags=["public"],
        )
    return router


def create_public_app(
    service: GremlinTownService,
    actor_resolver: Callable[[str], Actor | None],
    allowed_actor_id: str,
) -> FastAPI:
    """Create the exact ten-operation public ASGI app.

    The resolver remains the credential authority. Even if it returns an actor
    for another participant, this listener rejects that actor before any
    service operation runs.
    """

    if not allowed_actor_id:
        raise ValueError("allowed_actor_id must be non-empty")

    def resolve_allowed(token: str) -> Actor | None:
        actor = actor_resolver(token)
        if actor is None or actor.actor_id != allowed_actor_id:
            return None
        return actor

    app = FastAPI(
        title="Gremlin Town public participant API",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    app.state.actor_authenticator = ActorAuthenticator(resolve_allowed)
    # get_actor is shared by the canonical endpoints and checks these only as
    # a fallback after bearer authentication fails. This public app has no
    # login route and therefore no valid session fallback.
    app.state.sessions: dict[str, Any] = {}
    app.state.session_cookie_name = "gremlin_town_public_session"
    # Register concrete routes directly. FastAPI 0.141 stores included
    # routers behind a lazy wrapper, which makes the public route table
    # opaque to the root listener's allowlist/denial audit.
    for route in _public_router().routes:
        if isinstance(route, APIRoute):
            app.add_api_route(
                route.path,
                route.endpoint,
                methods=route.methods,
                name=route.name,
                tags=["public"],
            )

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
