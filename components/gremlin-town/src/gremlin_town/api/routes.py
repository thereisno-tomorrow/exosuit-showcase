"""Versioned HTTP routes; lifecycle decisions remain in GremlinTownService."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from gremlin_town.api.auth import get_actor
from gremlin_town.api.commands import (
    InvalidCommand,
    cancel_from_json,
    check_from_json,
    create_from_json,
    recovery_from_json,
    report_from_json,
    reply_from_json,
)
from gremlin_town.api.errors import ClientHttpError, call_service
from gremlin_town.api.serialization import to_jsonable
from gremlin_town.contracts import Actor


router = APIRouter(prefix="/v1", tags=["gremlin-town"])


def _body_error(exc: InvalidCommand) -> None:
    raise ClientHttpError(422, "INVALID_INPUT", str(exc)) from exc


def _json(value: Any) -> JSONResponse:
    return JSONResponse(content=to_jsonable(value))


@router.get("/participants")
async def participants_list(request: Request, actor: Actor = Depends(get_actor), project: str | None = Query(default=None)) -> JSONResponse:
    return _json(await call_service(request, "participants_list", actor, project))


@router.post("/requests")
async def requests_create(request: Request, actor: Actor = Depends(get_actor), body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        command = create_from_json(body)
    except InvalidCommand as exc:
        _body_error(exc)
    return _json(await call_service(request, "requests_create", actor, command))


@router.get("/requests")
async def requests_list(
    request: Request,
    actor: Actor = Depends(get_actor),
    project: str | None = Query(default=None),
    recipient: str | None = Query(default=None),
    work_state: str | None = Query(default=None),
    attention: bool | None = Query(default=None),
    open: bool | None = Query(default=None),
    stale: bool | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    filters = {
        key: value
        for key, value in {
            "project": project,
            "recipient": recipient,
            "work_state": work_state,
            "attention": attention,
            "open": open,
            "stale": stale,
            "limit": limit,
        }.items()
        if value is not None
    }
    return _json(await call_service(request, "requests_list", actor, filters, cursor))


@router.get("/requests/{request_id}")
async def requests_read(
    request: Request,
    request_id: str,
    actor: Actor = Depends(get_actor),
    message_cursor: str | None = Query(default=None),
) -> JSONResponse:
    return _json(await call_service(request, "requests_read", actor, request_id, message_cursor, resource=True))


@router.post("/requests/{request_id}/reports")
async def requests_report(request: Request, request_id: str, actor: Actor = Depends(get_actor), body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        command = report_from_json(request_id, body)
    except InvalidCommand as exc:
        _body_error(exc)
    return _json(await call_service(request, "requests_report", actor, command, resource=True))


@router.post("/requests/{request_id}/replies")
async def requests_reply(request: Request, request_id: str, actor: Actor = Depends(get_actor), body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        command = reply_from_json(request_id, body)
    except InvalidCommand as exc:
        _body_error(exc)
    return _json(await call_service(request, "requests_reply", actor, command, resource=True))


@router.post("/requests/{request_id}/cancel")
async def requests_cancel(request: Request, request_id: str, actor: Actor = Depends(get_actor), body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        command = cancel_from_json(request_id, body)
    except InvalidCommand as exc:
        _body_error(exc)
    return _json(await call_service(request, "requests_cancel", actor, command, resource=True))


@router.post("/requests/{request_id}/check")
async def requests_check(request: Request, request_id: str, actor: Actor = Depends(get_actor), body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        command = check_from_json(request_id, body)
    except InvalidCommand as exc:
        _body_error(exc)
    return _json(await call_service(request, "requests_check", actor, command, resource=True))


@router.post("/requests/{request_id}/recovery-disposition")
async def recovery_dispose(request: Request, request_id: str, actor: Actor = Depends(get_actor), body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        command = recovery_from_json(request_id, body)
    except InvalidCommand as exc:
        _body_error(exc)
    return _json(await call_service(request, "recovery_dispose", actor, command, resource=True))


@router.get("/operations/{operation_id}")
async def operations_get(request: Request, operation_id: str, actor: Actor = Depends(get_actor)) -> JSONResponse:
    return _json(await call_service(request, "operations_get", actor, operation_id, resource=True))


@router.get("/events")
async def events_list(
    request: Request,
    actor: Actor = Depends(get_actor),
    request_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    filters = {
        key: value
        for key, value in {"request_id": request_id, "event_type": event_type, "limit": limit}.items()
        if value is not None
    }
    return _json(await call_service(request, "events_list", actor, filters, cursor))
