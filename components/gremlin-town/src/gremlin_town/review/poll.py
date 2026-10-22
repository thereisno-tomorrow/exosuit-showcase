"""The moments polling loop: decides which of the three signals fired, then
calls `run_moments` for each occurrence (step E spec §4).

This is the part `deploy/gremlin-town-moments.timer.example` runs on the hub:
on since 28 Sep 2026 and every 3 minutes since 29 Sep 2026 (see
`ops/audits/2026-09-28-review-01-install.md` and
`ops/audits/2026-09-29-google-install.md`). Nothing here creates or enables a
timer, scheduled task, or service unit. `poll_once` is plain, deterministic
Python: no model call, and no real Telegram or Diablo send (that is
`deliver`'s job, already proven separately). It is exercised only by tests
with a fake clock.

Each signal kind tracks its own cursor in the why-log's `runs` table
(`kind="moments_poll_<signal>"`), so a poll only ever looks at what is new
since the last one:

- `pc_on`: new rows in the why-log's own `signals` table (written by
  `POST /v1/moments/signals`). The reporting actor becomes `also_notify`
  (D24) - it is who told GT the PC came on, e.g. `local-codex`.
- `task_finished`: new GT events of type `result_returned` or
  `result_check_accepted`. The event's own actor becomes `also_notify` - in
  practice this only ever resolves for `result_check_accepted` (the real
  checking participant); `result_returned` is written by the outbox worker
  as `system:outbox`, which is never an enrolled, routable participant, so
  `run_moments` silently drops that second delivery and Diablo still gets it
  (`run_moments`'s own graceful `also_notify` handling, not special-cased
  here). "A continuity note closed" (the spec's third `task_finished` case)
  is not detected: continuity lives in Brain Home / the local project
  connector, not in GT's own event log, and building that reader is not
  part of this pass.
- `calendar_gap`: `brain_core.moments.find_gap` against `sources.calendar_events`,
  fired at most once per Singapore day (the smallest reversible reading of
  "each gap counts once" - a truer "once per contiguous gap" needs tracking
  when a gap period starts, not just that one existed at poll time).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import logging

from brain_core.moments import find_gap
from brain_core.timewords import today_in_singapore

from gremlin_town.contracts import Actor
from gremlin_town.review.run import ReviewContext, run_moments, close_hub_deliveries

logger = logging.getLogger(__name__)

TASK_FINISHED_EVENT_TYPES = ("result_returned", "result_check_accepted")
CALENDAR_GAP_LOOKAHEAD_HOURS = 12
CALENDAR_GAP_LOOKBEHIND_HOURS = 1


def _poll_pc_on(context: ReviewContext, now: datetime) -> tuple[dict[str, Any], ...]:
    why_log = context.why_log
    assert why_log is not None
    cursor_kind = "moments_poll_pc_on"
    after_id = int(why_log.latest_cursor(cursor_kind) or 0)
    signals = tuple(row for row in why_log.signals_since(after_id) if row.kind == "pc_on")
    if not signals:
        return ()
    results = []
    for row in signals:
        result = run_moments(context, now, "pc_on", also_notify=row.actor, signal_id=f"pc_on-{row.id}")
        results.append(result)
        if result.get("outcome") == "busy":
            break
        why_log.record_run(kind=cursor_kind, started_at=now, last_event_cursor=str(row.id), outcome="checked")
    return tuple(results)


def _poll_task_finished(context: ReviewContext, now: datetime) -> tuple[dict[str, Any], ...]:
    why_log = context.why_log
    assert why_log is not None
    cursor_kind = "moments_poll_task_finished"
    cursor = why_log.latest_cursor(cursor_kind)
    actor = Actor(actor_id=context.settings.sender_participant)

    events: list[dict[str, Any]] = []
    for event_type in TASK_FINISHED_EVENT_TYPES:
        page = context.service.events_list(actor, {"event_type": event_type}, cursor)  # type: ignore[attr-defined]
        events.extend(page.items)
    if not events:
        return ()
    events.sort(key=lambda event: event["sequence"])

    results = []
    for event in events:
        result = run_moments(
            context, now, "task_finished",
            also_notify=event.get("actor_id"),
            signal_id=f"task_finished-{event['sequence']}",
        )
        results.append(result)
        if result.get("outcome") == "busy":
            break
        why_log.record_run(kind=cursor_kind, started_at=now, last_event_cursor=str(event["sequence"]), outcome="checked")
    return tuple(results)


def _poll_calendar_gap(context: ReviewContext, now: datetime) -> tuple[dict[str, Any], ...]:
    calendar_events = context.sources.calendar_events
    if calendar_events is None:
        return ()
    why_log = context.why_log
    assert why_log is not None
    today = today_in_singapore(now)
    cursor_kind = "moments_poll_calendar_gap"
    if why_log.latest_cursor(cursor_kind) == today.isoformat():
        return ()  # already fired once today

    settings = context.settings
    window_start = now - timedelta(hours=CALENDAR_GAP_LOOKBEHIND_HOURS)
    window_end = now + timedelta(hours=CALENDAR_GAP_LOOKAHEAD_HOURS)
    try:
        events = calendar_events(window_start, window_end)
    except Exception:
        return ()  # a broken calendar source never blocks the other two signals
    if not find_gap(events, now, settings.gap_minutes, settings.gap_start_hour, settings.gap_end_hour):
        return ()

    result = run_moments(context, now, "calendar_gap", signal_id=f"gap-{today.isoformat()}")
    if result.get("outcome") != "busy":
        why_log.record_run(kind=cursor_kind, started_at=now, last_event_cursor=today.isoformat(), outcome="checked")
    return (result,)


def poll_once(context: ReviewContext, now: datetime) -> tuple[dict[str, Any], ...]:
    """Check all three signals once; returns every `run_moments` result produced.

    With `[review] handoff_notes` on, the same loop then keeps the notes for handed-off
    work (N12a, `review/handoff_notes.py`) and adds that pass's own result last.

    Disabled `[review]` (the default) checks nothing and returns immediately."""

    if not context.settings.enabled or context.why_log is None:
        return ()
    close_hub_deliveries(context, now)
    results = _poll_pc_on(context, now) + _poll_task_finished(context, now) + _poll_calendar_gap(context, now)
    if context.settings.handoff_notes:
        from gremlin_town.review.handoff_notes import keep_handoff_notes

        results += (keep_handoff_notes(context, now),)
    if context.placement is not None and context.placement.settings.enabled:
        # Step I (build plan slice 4): last, after the three signals, so it never holds them up.
        from gremlin_town.review.placement import run_placement

        try:
            placed = run_placement(context.placement, now)
        except Exception as exc:  # reported, never allowed to stop the poll; the next poll tries again
            from gremlin_town.review.brain_source import describe_failure

            logger.exception("placement run failed")
            placed = {"outcome": "error", "error": describe_failure(exc)}
        results += ({"placement": placed},)
    return results
