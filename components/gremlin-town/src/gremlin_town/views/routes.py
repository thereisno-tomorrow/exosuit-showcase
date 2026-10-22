"""Read-only authenticated HTML routes."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse

from gremlin_town.api.auth import get_actor
from gremlin_town.api.errors import ClientHttpError, call_service
from gremlin_town.api.serialization import to_jsonable
from gremlin_town.contracts import Actor
from gremlin_town.views.render import render_overview


def _filters(project: str | None, recipient: str | None, work_state: str | None, attention: bool | None) -> list[tuple[str, str, bool]]:
    links = [("All", "/overview", not project and not recipient and not work_state and attention is None)]
    if project:
        links.append((f"Project: {project}", f"/overview?project={project}", True))
    if recipient:
        links.append((f"Recipient: {recipient}", f"/overview?recipient={recipient}", True))
    if work_state:
        links.append((f"State: {work_state}", f"/overview?work_state={work_state}", True))
    if attention is not None:
        links.append(("Attention", f"/overview?attention={'true' if attention else 'false'}", True))
    return links


async def _list_page(request: Request, actor: Actor, *, attention: bool | None = None) -> HTMLResponse:
    project = request.query_params.get("project")
    recipient = request.query_params.get("recipient")
    work_state = request.query_params.get("work_state")
    cursor = request.query_params.get("cursor")
    limit = request.query_params.get("limit")
    if attention is None and request.query_params.get("attention") is not None:
        attention = request.query_params.get("attention", "").lower() == "true"
    filters = {key: value for key, value in {"project": project, "recipient": recipient, "work_state": work_state, "attention": attention}.items() if value is not None}
    heading = "Attention" if attention else "Inbox"
    status_code = 200
    try:
        page = await call_service(request, "requests_list", actor, filters, cursor)
        data = to_jsonable(page)
        html = render_overview(
            items=tuple(data.get("items", ())),
            next_cursor=data.get("next_cursor"),
            as_of=data.get("as_of"),
            sync_time=data.get("sync_time"),
            actor_id=actor.actor_id,
            heading=heading,
            filter_values={"project": project, "recipient": recipient, "work_state": work_state, "attention": attention},
            filters=_filters(project, recipient, work_state, attention),
            filter_action="/overview",
            next_path="/attention" if attention else "/overview",
            next_params={"project": project, "recipient": recipient, "work_state": work_state, "attention": "true" if attention else ("false" if attention is not None else None), "limit": limit},
        )
    except ClientHttpError as exc:
        html = render_overview(heading=heading, actor_id=actor.actor_id, filter_values={"project": project, "recipient": recipient, "work_state": work_state, "attention": attention}, error=exc.message)
        status_code = exc.status_code
    return HTMLResponse(html, status_code=status_code)


def install_view_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root(request: Request, actor: Actor = Depends(get_actor)) -> HTMLResponse:
        return await _list_page(request, actor)

    @app.get("/overview", response_class=HTMLResponse, include_in_schema=False)
    async def overview(request: Request, actor: Actor = Depends(get_actor)) -> HTMLResponse:
        return await _list_page(request, actor)

    @app.get("/inbox", response_class=HTMLResponse, include_in_schema=False)
    async def inbox(request: Request, actor: Actor = Depends(get_actor)) -> HTMLResponse:
        return await _list_page(request, actor)

    @app.get("/attention", response_class=HTMLResponse, include_in_schema=False)
    async def attention(request: Request, actor: Actor = Depends(get_actor)) -> HTMLResponse:
        return await _list_page(request, actor, attention=True)

    @app.get("/requests/{request_id}", response_class=HTMLResponse, include_in_schema=False)
    async def request_detail(request: Request, request_id: str, actor: Actor = Depends(get_actor)) -> HTMLResponse:
        status_code = 200
        try:
            snapshot = await call_service(request, "requests_read", actor, request_id, request.query_params.get("message_cursor"), resource=True)
            html = render_overview(
                heading="Request and delivery",
                snapshot=snapshot,
                as_of=getattr(snapshot, "as_of", None),
                actor_id=actor.actor_id,
            )
        except ClientHttpError as exc:
            html = render_overview(heading="Request and delivery", actor_id=actor.actor_id, error=exc.message)
            status_code = exc.status_code
        return HTMLResponse(html, status_code=status_code)

    @app.get("/delivery/{request_id}", response_class=HTMLResponse, include_in_schema=False)
    async def delivery(request: Request, request_id: str, actor: Actor = Depends(get_actor)) -> HTMLResponse:
        return await request_detail(request, request_id, actor)

    @app.get("/provenance", response_class=HTMLResponse, include_in_schema=False)
    async def provenance(
        request: Request,
        actor: Actor = Depends(get_actor),
        request_id: str | None = Query(default=None),
        event_type: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
    ) -> HTMLResponse:
        filters = {key: value for key, value in {"request_id": request_id, "event_type": event_type}.items() if value is not None}
        status_code = 200
        try:
            page = await call_service(request, "events_list", actor, filters, cursor)
            data = to_jsonable(page)
            html = render_overview(
                heading="Provenance",
                events=tuple(data.get("items", ())),
                as_of=data.get("as_of"),
                sync_time=data.get("sync_time"),
                actor_id=actor.actor_id,
                next_cursor=data.get("next_cursor"),
                filters=[("All events", "/provenance", not filters)],
                next_path="/provenance",
                next_params={"request_id": request_id, "event_type": event_type, "limit": request.query_params.get("limit")},
            )
        except ClientHttpError as exc:
            html = render_overview(heading="Provenance", actor_id=actor.actor_id, error=exc.message)
            status_code = exc.status_code
        return HTMLResponse(html, status_code=status_code)
