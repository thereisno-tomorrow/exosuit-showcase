"""Fresh policy-authorized views and the existing work/review doors."""
from dataclasses import replace

import anyio

from brain_core import work
from gremlin_town.dashboard.reads import DashboardReader, DashboardSources
from gremlin_town.mcp.brain_runtime import BrainCaller, BrainOperation, OwnerSelector
from gremlin_town.mcp.surface_door import REVIEW_CARD_CALLER
from gremlin_town.review.run import ReviewContext, current_review_parts
from .models import CardError, review_day
from .latency import phase


class ReviewCardBackend:
    def __init__(self, runtime, settings):
        self.runtime, self.settings = runtime, settings
        self.caller = BrainCaller(REVIEW_CARD_CALLER, frozenset({"bc:read", "bc:write"}))

    async def authorize(self):
        refresh = getattr(self.runtime, "refresh_participants", None)
        if refresh:
            await anyio.to_thread.run_sync(refresh)
        grant = await self.runtime.brain_runtime._authorize(
            self.caller, BrainOperation.MEMORY_LIST, OwnerSelector("personal"), "read")
        if grant.owner_id != self.settings.owner_id or grant.owner_locator != "vault://brain-home":
            raise CardError("Couldn't open your review because access was denied. Nothing changed.")
        return grant

    async def fresh(self, now, *, opening=False, full_day=True, weekly=False):
        with phase("access"):
            grant = await self.authorize()
        def read():
            from .reads import ReviewReadSession
            sources = self.runtime.review_sources
            def owned(entry):
                record = getattr(entry, "record", entry)
                return grant.owner_id in record.owners and grant.owner_locator in record.vaults
            pinned = getattr(self.runtime, "review_read_snapshot", None)
            revision = None
            if pinned is not None:
                with phase("read_items"):
                    snapshot = pinned()
                    entries = tuple(entry for entry in snapshot.items if owned(entry))
                    preferences = tuple(entry for entry in snapshot.preferences if owned(entry))
                    revision = snapshot.revision
            else:
                with phase("read_items"):
                    entries = tuple(entry for entry in sources.brain_items() if owned(entry))
                with phase("read_preferences"):
                    preferences = tuple(entry for entry in sources.brain_preferences() if owned(entry)) if sources.brain_preferences else ()
            sources = replace(sources, brain_items=lambda: entries, brain_preferences=lambda: preferences)
            with phase("review_history"):
                log = self.runtime.review_why_log.read_view(review_day(now, now.tzinfo))
            session = ReviewReadSession(now, entries, preferences, sources, log,
                                        self.runtime.orientation.ranking, revision)
            return self._compose(session, opening=opening, full_day=full_day, weekly=weekly)
        try:
            return await anyio.to_thread.run_sync(read)
        except Exception as exc:
            if isinstance(exc, CardError):
                raise
            raise CardError("Couldn't see your review. Try again; nothing was changed.") from None

    def _compose(self, session, *, opening=False, full_day=False, weekly=False):
        from .reads import ReviewDetails, ReviewTaskProjection, enrich_detail
        sources, index, log, now = session.sources, session.index, session.log, session.now
        view = {"day": {}, "work": {}, "details": ReviewDetails(session)}
        if full_day:
            if session.day_projection is None:
                schedule = None
                if self.runtime.flowsavvy.enabled:
                    from gremlin_town.runtime import _schedule_reader
                    schedule_reader = _schedule_reader(self.runtime.flowsavvy)
                    def schedule(*args):
                        with phase("read_schedule"):
                            return schedule_reader(*args)
                with phase("day_projection"):
                    session.day_projection = DashboardReader(DashboardSources(sources.brain_items,
                        sources.calendar_events, schedule, log.history, sources.brain_preferences,
                        session.ranking)).read(now, day=session.day, zone=now.tzinfo, calendar_day=True,
                                               prepared_index=index)
                raw = {entry.record.id: entry.record for entry in session.entries}
                for ref, detail in session.day_projection["details"].items():
                    enrich_detail(session, ref, detail, raw)
            view = dict(session.day_projection)
        if weekly:
            if session.weekly_projection is None:
                from brain_core.weekly import weekly_review, TITLES
                with phase("weekly_projection"):
                    summary = weekly_review(session.entries, now, preferences=session.preferences,
                                            history=log.history(), prepared_index=index)
                session.weekly_projection = [{"title": TITLES[part],
                    "lines": [line.text for line in summary.lines if line.part == part]} for part in summary.parts]
            view["weekly"] = session.weekly_projection
        if session.task_projection is None or (not opening and session.task_projection.opening_only):
            parts = {}
            if not opening:
                sources = replace(sources, calendar_events=lambda *_: (), important_emails=None,
                    open_requests=lambda: (), brain_home_size=None)
                context = ReviewContext(self.runtime.review, log, sources, self.runtime.service,
                                        ranking=session.ranking, auto_tidy_enabled=self.settings.auto_tidy)
                with phase("review_questions"):
                    parts = {part: result["lines"] for part, result in current_review_parts(context, now,
                        caller=REVIEW_CARD_CALLER, code_callers=frozenset({REVIEW_CARD_CALLER}),
                        parts=(2, 3, 4), review_day=session.day, section_cap=2000, card_local_day=True,
                        prepared_index=index).items()}
            from .stages import stage_queues
            stages = stage_queues(index, parts, log, session.day)
            real_questions = {line["item_ref"] for lines in stages.values() for line in lines
                              if line.get("kind") not in {"outcome", "carried_over"}}
            offered = {line.get("item_ref") for line in parts.get(4, ()) if line.get("kind") == "your_day"}
            recovered = [dict(reference=f"choice:{session.day}:{ref}", item_ref=ref,
                              kind="your_day", answered=False, already_picked=False)
                         for ref, record in index.items() if work.is_up(record, index, session.day)
                         and ref not in offered and ref not in real_questions]
            if recovered:
                parts[4] = [*parts.get(4, ()), *recovered]
            session.task_projection = ReviewTaskProjection(parts, stages, opening)
        else:
            parts, stages = session.task_projection.parts, session.task_projection.stages
        return {**view, "parts": parts, "stages": stages, "index": index,
                "full_day": full_day, "_read_session": session}

    async def extend(self, view, *, full_day=False, weekly=False):
        """Destination-only inputs, sharing this tap's pinned records and history."""
        try:
            session = view["_read_session"]
            return await anyio.to_thread.run_sync(lambda: self._compose(session, full_day=full_day, weekly=weekly))
        except Exception as exc:
            if isinstance(exc, CardError):
                raise
            raise CardError("Couldn't see your review. Try again; nothing was changed.") from None

    async def change(self, request, *, owner_id):
        if owner_id != self.settings.owner_id:
            raise CardError("This review belongs to its configured owner.")
        await self.authorize()
        with phase("apply_change"):
            return await self.runtime.brain_runtime.change_work_from_tap(self.caller, OwnerSelector("personal"), request,
                                                                    authenticated_owner_id=owner_id)

    async def settle(self, refs, eligible, *, operation_id, now, day=None):
        """Record picks before the yes; an exact retry records no second signal."""
        await self.authorize()
        grant = await self.runtime.brain_runtime._authorize(
            self.caller, BrainOperation.WORK, OwnerSelector("personal"), "write")
        if grant.owner_id != self.settings.owner_id:
            raise CardError("This review belongs to its configured owner.")
        def save():
            log = self.runtime.review_why_log
            log.set_review_picks(refs, eligible, now=now, operation_id=operation_id, review_day=day or review_day(now, now.tzinfo))
            from gremlin_town.flowsavvy.state import notify_change
            notify_change(self.runtime.flowsavvy)
        with phase("confirm_choices"):
            await anyio.to_thread.run_sync(save)
        return {"status": "accepted", "remote_observed": True}
