"""Compose the one dashboard listener with existing authorized hub adapters."""
from __future__ import annotations

from datetime import datetime, timezone

import anyio
from fastapi import APIRouter

from gremlin_town.mcp.brain_runtime import BrainCaller, BrainOperation, OwnerSelector
from gremlin_town.mcp.surface_door import DASHBOARD_CALLER
from .app import create_app, require_owner, require_write_owner
from .read_routes import create_read_router
from .reads import DashboardReader, DashboardSources, DashboardUnavailable
from .write_routes import create_write_router
from .writes import DashboardWriter


def sources_from_runtime(settings, runtime):
    """The existing copy and why-log remain the only source of record state.

    Each redraw rechecks the live owner policy before reading anything. Record
    ownership is filtered on raw source records, before inherited projections.
    """
    if runtime.brain_runtime is None or runtime.review_sources.brain_items is None:
        raise ValueError("Dashboard reads require the configured Brain Home runtime and reader")
    caller = BrainCaller(DASHBOARD_CALLER, frozenset({"bc:read"}))

    async def authorize():
        authorization = await runtime.brain_runtime._authorize(
            caller, BrainOperation.MEMORY_LIST, OwnerSelector("personal"), "read")
        if authorization.owner_id != settings.owner_id or authorization.owner_locator != "vault://brain-home":
            raise DashboardUnavailable("Couldn't open your work. Ask the operator to check access.")
        return authorization

    def owned(entry, authorization):
        record = getattr(entry, "record", entry)
        return (authorization.owner_id in record.owners
                and authorization.owner_locator in record.vaults)

    def items():
        authorization = anyio.run(authorize)
        return tuple(entry for entry in runtime.review_sources.brain_items() if owned(entry, authorization))

    preferences_reader = runtime.orientation_context.sources.preferences
    def preferences():
        authorization = anyio.run(authorize)
        return tuple(record for record in preferences_reader() if owned(record, authorization))

    schedule = None
    if runtime.flowsavvy.enabled:
        from gremlin_town.runtime import _schedule_reader
        schedule = _schedule_reader(runtime.flowsavvy)
    return DashboardSources(
        items=items, calendar=runtime.google_calendar_reader, schedule=schedule,
        history=runtime.review_why_log.history if runtime.review_why_log else None,
        preferences=preferences if preferences_reader else None,
        ranking=runtime.orientation.ranking,
    )


def create_dashboard_app(settings, runtime, *, reader=None, clock=None, auth_clock=None):
    """No worker or second API listener is started by dashboard composition."""
    settings.validate()
    options = {} if auth_clock is None else {"clock": auth_clock}
    if not settings.enabled:
        return create_app(settings, **options)
    router = APIRouter()
    if settings.reads:
        reader = reader or DashboardReader(sources_from_runtime(settings, runtime))
        router.include_router(create_read_router(reader, clock=clock))
    if settings.writes:
        if runtime.brain_runtime is None:
            raise ValueError("Dashboard writes require the configured Brain runtime")
        router.include_router(create_write_router(DashboardWriter(runtime.brain_runtime),
                             settings=settings, require_write_owner=require_write_owner))
    if settings.ask_diablo:
        from .ask import DashboardAsk
        from .ask_routes import create_ask_router

        async def authorize_item(owner_id, ref):
            if owner_id != settings.owner_id:
                raise DashboardUnavailable("Couldn't open that thing.")
            # This listener has its own policy instance. The primary hub's
            # refresh cannot revoke this process's cached participant access.
            await anyio.to_thread.run_sync(runtime.refresh_participants)
            result = await anyio.to_thread.run_sync(reader.read,
                clock() if clock else datetime.now(timezone.utc))
            if ref not in result["details"]:
                raise DashboardUnavailable("Couldn't find that thing.")
            return result["details"][ref]

        ask = DashboardAsk(runtime.service, owner_id=settings.owner_id,
            sender_participant=runtime.review.sender_participant,
            recipient_participant=runtime.review.delivery_participant,
            project=runtime.review.project, authorize_item=authorize_item)
        router.include_router(create_ask_router(ask, settings=settings,
            require_owner=require_owner, require_write_owner=require_write_owner))
    app = create_app(settings, router=router, **options)
    app.state.dashboard_reader = reader
    app.state.hub_runtime = runtime
    return app
