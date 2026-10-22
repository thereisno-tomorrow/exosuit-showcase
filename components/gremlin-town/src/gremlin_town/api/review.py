"""HTTP surface for `[review]`. Install only when `[review] enabled = true`;
otherwise leave these routes unregistered - a true 404, not a handler-returned
one (step E spec §8: "Both absent (404) when disabled").
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from gremlin_town.api.auth import get_actor
from gremlin_town.api.errors import ClientHttpError
from gremlin_town.contracts import Actor
from gremlin_town.review.why_log import WhyLogError
from gremlin_town.mcp.brain_runtime import work_caller_allowed
from gremlin_town.mcp.brain_runtime import BrainRuntimeError

logger = logging.getLogger(__name__)

# pc_on (step E); day_settled and day_change (step I, build plan slice 4): Nic has settled his day
# in the review, or changed it in one line.
ALLOWED_SIGNAL_KINDS = frozenset({"pc_on", "day_settled", "day_change"})
ALLOWED_RESPONSES = frozenset(
    {"picked_today", "accepted", "not_now", "parked", "dropped", "corrected", "no_answer", "answered"}
)

# How long the reply to day_settled or day_change waits for the plan (step I section 5a). Diablo's
# client gives this call 20 s (adapters/hermes/hub_client.py: moments.signal is not a Brain write,
# and GREMLIN_TOWN_HUB_TIMEOUT is unset on his container), and on 30 Sep 2026 the run took 19.2 s.
# Past this limit the reply says plan_pending and the run carries on by itself. The signal is
# stored before the run starts, so the day counts as settled either way, and if the run stops
# short, the 3-minute moments poll tries again. 12 s leaves room under 20 s for the why-log write
# and the network. Nic approved the figure and the "your plan is coming" reply on 30 Sep
# (docs/product/2026-09-30-diablo-speed-and-token-fixes-plan.md, finding 6).
INLINE_PLACEMENT_SECONDS = 12.0

# A run that ended without a plan gets its own reply, never plan_pending, which now means only that the
# run is still going (the storage-limit plan, layer 1; the independent review of fix/diablo-speed-tokens).
# cannot_read_brain_home: Brain Home is past a size limit, and every run will fail the same way until
# it is tidied up. plan_failed: anything else; the signal is stored, so the 3-minute poll tries again.
# Both carry `error`, in plain words, for Diablo to tell Nic.
PLAN_FAILED = "plan_failed"
PLAN_FAILED_WORDS = (
    "Your day couldn't be planned just now. The hub tries again every 3 minutes, and "
    "\"where am I?\" shows the plan once it's drawn."
)

# On shutdown, how long the service waits for runs still going after their reply, before it closes
# the why-log under them. A run cut off there may already have written calendar entries it never
# records, and it leaves its lock held for LOCK_STALE_SECONDS (300 s), so waiting is worth a lot.
# But the unit's TimeoutStopSec is 30 s (deploy/gremlin-town.service): past that systemd kills the
# process with no cleanup at all. 20 s leaves 10 s for the rest of shutdown. A run already past the
# 12 s cap and still going 20 s later is stuck, not slow.
LATE_RUN_SHUTDOWN_SECONDS = 20.0


def failed_run_reply(error: BaseException | None) -> dict[str, str]:
    """The reply's `placement` for a run that raised (or was cancelled): `cannot_read_brain_home`
    for a Brain Home size limit, which won't pass by itself, else `plan_failed`. Either way `error`
    says why in plain words."""

    from gremlin_town.review.brain_source import brain_problem
    from gremlin_town.review.placement import CANNOT_READ_BRAIN_HOME

    problem = None if error is None else brain_problem(error)
    if problem is not None and problem[1]:
        return {"outcome": CANNOT_READ_BRAIN_HOME, "error": problem[0]}
    return {"outcome": PLAN_FAILED, "error": PLAN_FAILED_WORDS if problem is None else problem[0]}


def _late_run_finished(runtime: Any, kind: str, task: asyncio.Task[Any]) -> None:
    runtime.placement_late_runs.discard(task)
    if task.cancelled():
        logger.warning("placement run after a %s signal was cancelled after its reply; the poll tries again", kind)
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "placement run after a %s signal failed after its reply (%s); the poll tries again",
            kind, type(error).__name__, exc_info=error,
        )
        return
    placed = task.result()
    logger.warning(
        "placement run after a %s signal finished after its reply: %s, %s",
        kind, placed.get("outcome"), placed.get("seconds"),
    )


async def finish_late_placement_runs(runtime: Any, timeout: float | None = None) -> None:
    """Shutdown: let runs still going after their reply record their plan and release the placement
    lock before the why-log closes, for at most ``timeout`` (``LATE_RUN_SHUTDOWN_SECONDS``)."""

    loop = asyncio.get_running_loop()
    pending = {task for task in runtime.placement_late_runs if not task.done() and task.get_loop() is loop}
    if not pending:
        return
    limit = LATE_RUN_SHUTDOWN_SECONDS if timeout is None else timeout
    logger.warning("shutdown waits up to %.0f s for %d placement run(s) still going", limit, len(pending))
    _done, still_going = await asyncio.wait(pending, timeout=limit)
    if still_going:
        logger.error(
            "%d placement run(s) still going after %.0f s at shutdown: the why-log closes under them, their lock "
            "is taken over after %d s, and entries they wrote but didn't record may be treated as moved by hand",
            len(still_going), limit, _lock_stale_seconds(),
        )


def _lock_stale_seconds() -> int:
    from gremlin_town.review.placement import LOCK_STALE_SECONDS

    return LOCK_STALE_SECONDS


def record_review_response(why_log, *, caller: str, door_participant: str = "hermes-wa",
                           relayer: str | None = None, check_only: bool = False, item_ref: str | None = None,
                           code_callers: frozenset[str] = frozenset(),
                           item_refs: list[str] | None = None,
                           response: str, response_words: str | None, now: datetime) -> dict[str, Any]:
    """Relay Nic's answer; no named hub relayer is registered in this build."""
    identity = relayer if relayer is not None else caller
    if not work_caller_allowed(identity, door_participant=door_participant, code_callers=code_callers):
        raise BrainRuntimeError("ACCESS_DENIED", "Only Diablo relays Nic's review answers", field="caller",
            fix="Nic settles his day with Diablo")
    references = item_refs if item_refs is not None else [item_ref]
    if not references or any(not isinstance(ref, str) or not ref for ref in references):
        raise WhyLogError("no surfacing found for the supplied references to respond to")
    things = []
    for reference in references:
        line = why_log.answer_context(reference)
        thing = line.thing if line is not None else reference
        if not why_log.full_reason(thing):
            raise WhyLogError(f"no surfacing found for {reference!r} to respond to")
        if thing not in things:
            things.append(thing)
    target = {"item_refs": things} if item_refs is not None else {"item_ref": things[0]}
    if check_only:
        return {"status": "validated", "recorded": False, **target,
            "say": "Checked only; nothing was saved."}
    if response == "answered":
        why_log.mark_today_answered(things, message=response_words or "", now=now)
    else:
        for thing in things:
            why_log.record_response(item_ref=thing, response=response, response_words=response_words, responded_at=now,
                                    channel="review" if item_refs is not None else None)
    return {"recorded": True, **target, "response": response, "relayed_by": identity}


def record_moments_signal(why_log, *, caller: str, door_participant: str = "hermes-wa",
                          relayer: str | None = None, check_only: bool = False, kind: str,
                          code_callers: frozenset[str] = frozenset(),
                          detail: str | None, now: datetime) -> dict[str, Any]:
    """Relay day answers through the door gate; pc_on remains open to participants."""
    identity = relayer if relayer is not None else caller
    if (relayer is not None or kind in {"day_settled", "day_change"}) and not work_caller_allowed(
            identity, door_participant=door_participant, code_callers=code_callers):
        raise BrainRuntimeError("ACCESS_DENIED", "Only Diablo relays Nic's day answers", field="caller",
            fix="Nic settles his day with Diablo")
    if check_only:
        return {"status": "validated", "recorded": False, "kind": kind,
            "say": "Checked only; nothing was saved."}
    why_log.record_signal(kind=kind, actor=identity, at=now, detail=detail)
    return {"recorded": True, "kind": kind, "actor": identity, "at": now.isoformat()}


def install_review_routes(app: FastAPI, runtime: Any) -> None:
    why_log = runtime.review_why_log
    router = APIRouter(prefix="/v1", tags=["review"])

    def refused():
        return JSONResponse({"error": {"code": "ACCESS_DENIED", "message": "Only Diablo relays Nic's review answers",
            "field": "caller", "fix": "Nic settles his day with Diablo"}}, status_code=403)

    def replaying(actor):
        provider = getattr(getattr(runtime, "brain_runtime", None), "_memory", None)
        marker = getattr(provider, "replay_marker", None)
        return actor.actor_id == getattr(runtime, "door_participant", "hermes-wa") and marker is not None and marker.is_file()

    @router.post("/moments/signals")
    async def moments_signal(
        request: Request,
        actor: Actor = Depends(get_actor),
        body: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        runtime.policy.authenticate(actor)
        kind = body.get("kind")
        if kind not in ALLOWED_SIGNAL_KINDS:
            raise ClientHttpError(422, "INVALID_INPUT", f"kind must be one of {sorted(ALLOWED_SIGNAL_KINDS)}")
        if kind in ("day_settled", "day_change") and not work_caller_allowed(
            actor.actor_id, door_participant=getattr(runtime, "door_participant", "hermes-wa")):
            return refused()
        detail = body.get("detail")
        if kind in ("day_settled", "day_change") and detail is not None and not isinstance(detail, str):
            raise ClientHttpError(422, "INVALID_INPUT", "detail must be a string")
        now = datetime.now(UTC)
        flow_mode = bool(getattr(getattr(runtime, "flowsavvy", None), "enabled", False))
        busy_change = None
        if kind == "day_change":
            from gremlin_town.review.placement import DayChangeError, parse_day_change
            from gremlin_town.review.busy_calendar import BusyChangeError, parse_busy_change

            try:
                if flow_mode:
                    busy_change = parse_busy_change(detail, now, why_log=why_log, caller=actor.actor_id)
                    if not runtime.flowsavvy.busy_calendar:
                        raise ClientHttpError(409, "FEATURE_DISABLED", "Calendar busy controls are not switched on.")
                    if runtime.calendar_writer is None:
                        raise ClientHttpError(409, "FEATURE_UNAVAILABLE", "Calendar busy controls need calendar write access.")
                else:
                    parse_day_change(detail)
            except (DayChangeError, BusyChangeError) as exc:
                raise ClientHttpError(422, "INVALID_INPUT", str(exc)) from exc
            except WhyLogError as exc:
                raise ClientHttpError(500, "WHY_LOG_READ_FAILED", "Could not check the saved calendar instruction.") from exc
        try:
            answer = record_moments_signal(why_log, caller=actor.actor_id,
                door_participant=getattr(runtime, "door_participant", "hermes-wa"), check_only=replaying(actor),
                kind=kind, detail=detail, now=now)
        except WhyLogError as exc:
            raise ClientHttpError(500, "WHY_LOG_WRITE_FAILED", str(exc)) from exc
        if answer.get("status") == "validated":
            return JSONResponse(answer)
        if busy_change is not None:
            from starlette.concurrency import run_in_threadpool
            from gremlin_town.review.busy_calendar import apply_busy_change

            try:
                answer["calendar"] = await run_in_threadpool(apply_busy_change, why_log, runtime.calendar_writer,
                    busy_change, caller=actor.actor_id, now=now)
            except WhyLogError as exc:
                raise ClientHttpError(500, "WHY_LOG_WRITE_FAILED", "Could not track the calendar change. It needs checking.") from exc
            return JSONResponse(answer)
        placement_context = getattr(runtime, "placement_context", None)
        if kind in ("day_settled", "day_change") and placement_context is not None and not flow_mode:
            # Step I section 5a: the reply after Nic settles or changes his day shows the plan at once.
            from starlette.concurrency import run_in_threadpool

            from gremlin_town.review.placement import run_placement

            # Google calls block, so the run goes to a worker thread, off the event loop.
            run = asyncio.ensure_future(run_in_threadpool(run_placement, placement_context, now))
            try:
                placed = await asyncio.wait_for(asyncio.shield(run), INLINE_PLACEMENT_SECONDS)
            except TimeoutError:
                if not run.done():
                    # Over the limit: answer now and let the run finish (or the poll try again).
                    runtime.placement_late_runs.add(run)
                    run.add_done_callback(lambda task, kind=kind: _late_run_finished(runtime, kind, task))
                    logger.warning(
                        "placement run after a %s signal passed %.0f s; replied plan_pending, the run goes on",
                        kind, INLINE_PLACEMENT_SECONDS,
                    )
                    placed = {"outcome": "plan_pending"}
                elif not run.cancelled() and run.exception() is None:
                    placed = run.result()  # it finished just as the wait gave up
                else:
                    # A TimeoutError raised inside the run itself (a GitHub or Google socket), not the cap.
                    error = None if run.cancelled() else run.exception()
                    logger.error("placement run after a %s signal failed", kind, exc_info=error)
                    placed = failed_run_reply(error)
            except Exception as exc:
                # The signal is stored, so the 3-minute poll tries again; the reply says the run failed,
                # not that a plan is coming, and not a 500.
                logger.exception("placement run after a %s signal failed", kind)
                placed = failed_run_reply(exc)
            else:
                seconds = placed.get("seconds") or {}
                if seconds.get("total", 0) >= INLINE_PLACEMENT_SECONDS / 2:
                    # Slow but in time: the split shows where the time went (service INFO lines don't
                    # reach the journal, so this is a warning).
                    logger.warning("placement run after a %s signal was slow: %s", kind, seconds)
            answer["placement"] = {key: placed[key] for key in ("outcome", "summary", "calendar", "error") if key in placed}
        return JSONResponse(answer)

    @router.post("/review/responses")
    async def review_response(
        request: Request,
        actor: Actor = Depends(get_actor),
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        runtime.policy.authenticate(actor)
        if not work_caller_allowed(actor.actor_id, door_participant=getattr(runtime, "door_participant", "hermes-wa")):
            return refused()
        item_ref = body.get("item_ref")
        item_refs = body.get("item_refs")
        response = body.get("response")
        current_day = body.get("current_day", False)
        if not isinstance(current_day, bool):
            raise ClientHttpError(422, "INVALID_INPUT", "current_day must be a boolean")
        async def rebuilt_day():
            from starlette.concurrency import run_in_threadpool
            from gremlin_town.review.run import ReviewContext, current_review_part
            context = ReviewContext(runtime.review, why_log, runtime.review_sources, runtime.service,
                                    ranking=getattr(runtime.orientation, "ranking", "points"))
            try:
                part = await run_in_threadpool(current_review_part, context, datetime.now(UTC),
                    caller=actor.actor_id, door_participant=getattr(runtime, "door_participant", "hermes-wa"))
            except WhyLogError as exc:
                raise ClientHttpError(500, "WHY_LOG_WRITE_FAILED", str(exc)) from exc
            return part
        if current_day and item_ref is None and item_refs is None and response is None:
            if replaying(actor):
                return JSONResponse({"status": "validated", "recorded": False, "say": "Checked only; nothing was saved."})
            return JSONResponse({"recorded": False, "current_day": await rebuilt_day()})
        if item_refs is not None:
            if not isinstance(item_refs, list) or not item_refs or any(not isinstance(ref, str) or not ref for ref in item_refs):
                raise ClientHttpError(422, "INVALID_INPUT", "item_refs must be a non-empty list of references")
            if item_ref is not None:
                raise ClientHttpError(422, "INVALID_INPUT", "use item_ref or item_refs, not both")
        elif not isinstance(item_ref, str) or not item_ref:
            raise ClientHttpError(422, "INVALID_INPUT", "item_ref or item_refs is required")
        if response not in ALLOWED_RESPONSES:
            raise ClientHttpError(422, "INVALID_INPUT", f"response must be one of {sorted(ALLOWED_RESPONSES)}")
        response_words = body.get("response_words")
        if response_words is not None and not isinstance(response_words, str):
            raise ClientHttpError(422, "INVALID_INPUT", "response_words must be a string")
        now = datetime.now(UTC)
        try:
            answer = record_review_response(why_log, caller=actor.actor_id,
                door_participant=getattr(runtime, "door_participant", "hermes-wa"), check_only=replaying(actor),
                item_ref=item_ref, item_refs=item_refs, response=response, response_words=response_words, now=now)
        except WhyLogError as exc:
            status = 404 if "no surfacing found" in str(exc) else 500
            raise ClientHttpError(status, "WHY_LOG_WRITE_FAILED", str(exc)) from exc
        if current_day and answer.get("status") != "validated":
            answer["current_day"] = await rebuilt_day()
        return JSONResponse(answer)

    app.include_router(router)
