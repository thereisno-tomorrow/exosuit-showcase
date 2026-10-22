"""Authenticated, default-off dashboard reads. No public data or write routes."""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from gremlin_town.dashboard.page import page_html
from gremlin_town.dashboard.reads import DashboardReader, DashboardUnavailable

ASSETS = Path(__file__).with_name("static")


def create_read_router(reader: DashboardReader, *, owner_dependency=None, clock=None):
    """Install into the passkey app. A test may inject an equally strict owner gate."""
    if owner_dependency is None:
        from gremlin_town.dashboard.app import require_owner
        owner_dependency = require_owner
    clock = clock or (lambda: datetime.now(timezone.utc))
    router = APIRouter()

    def enabled(request: Request, owner=Depends(owner_dependency)):
        settings = request.app.state.dashboard_settings
        if not settings.enabled or not settings.reads:
            raise HTTPException(404, "Not available")
        return owner

    def payload(request, part, ref=None):
        try:
            result = reader.read(clock())
        except DashboardUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        if ref is not None:
            if ref not in result["details"]:
                raise HTTPException(404, "Couldn't find that thing")
            value = result["details"][ref]
        else:
            value = result[part]
        settings = request.app.state.dashboard_settings
        value["capabilities"] = {"writes": settings.writes, "ask_diablo": settings.ask_diablo}
        return JSONResponse(value, headers={"Cache-Control": "no-store", "Vary": "Cookie"})

    @router.get("/api/day", dependencies=[Depends(enabled)])
    def day(request: Request):
        return payload(request, "day")

    @router.get("/api/work", dependencies=[Depends(enabled)])
    def work(request: Request):
        return payload(request, "work")

    @router.get("/api/work/{ref}", dependencies=[Depends(enabled)])
    def detail(ref: str, request: Request):
        return payload(request, "details", ref)

    @router.get("/app", dependencies=[Depends(enabled)], response_class=HTMLResponse)
    def page(request: Request):
        settings = request.app.state.dashboard_settings
        return HTMLResponse(page_html(actions=settings.writes, ask=settings.ask_diablo),
                            headers={"Cache-Control": "no-store", "Vary": "Cookie"})

    @router.get("/assets/{name}", dependencies=[Depends(enabled)])
    def asset(name: str, request: Request):
        settings = request.app.state.dashboard_settings
        if ((name == "actions.js" and not settings.writes)
                or (name == "ask.js" and not settings.ask_diablo)):
            raise HTTPException(404, "Not available")
        types = {"dashboard.css": "text/css", "dashboard.js": "text/javascript", "actions.js": "text/javascript", "ask.js": "text/javascript"}
        if name not in types or not (ASSETS / name).is_file():
            raise HTTPException(404, "Not available")
        return Response((ASSETS / name).read_bytes(), media_type=types[name],
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router
