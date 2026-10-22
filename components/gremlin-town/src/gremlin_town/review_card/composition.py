"""Use the existing hub runtime and its one GT outbox for every card operation."""
from dataclasses import replace
import json
import re

import anyio

from gremlin_town.review.run import ReviewContext


def context_from_runtime(runtime, *, grant=None):
    sources = runtime.review_sources
    if grant is not None:
        def owned(reader):
            def read():
                return tuple(entry for entry in reader() if
                    grant.owner_id in getattr(entry, "record", entry).owners and
                    grant.owner_locator in getattr(entry, "record", entry).vaults)
            return read
        sources = replace(sources, brain_items=owned(sources.brain_items),
                          brain_preferences=owned(sources.brain_preferences) if sources.brain_preferences else None)
    return ReviewContext(runtime.review, runtime.review_why_log, sources, runtime.service,
        handoff_notes=runtime.review_handoff_notes, ranking=runtime.orientation.ranking,
        placement=runtime.placement_context, schedule_enabled=runtime.orientation_context.schedule_enabled,
        flowsavvy_enabled=runtime.flowsavvy.enabled, schedule=runtime.orientation_context.sources.schedule,
        auto_tidy_enabled=runtime.discord.enabled and runtime.discord.auto_tidy)


def build_engine(runtime, settings, store, clock):
    from .engine import ReviewCardEngine
    from .diablo import ReviewDiablo
    from gremlin_town.review.tidy import run_added_tidy, added_tidy_revision
    emojis = {}
    if settings.emoji_file:
        emojis = json.loads(settings.emoji_file.read_text(encoding="utf-8"))
        if (not isinstance(emojis, dict) or len(emojis) > 200 or any(
                not isinstance(key, str) or not key.startswith("exo_") or
                not isinstance(value, str) or not re.fullmatch(r"<a?:exo_[a-zA-Z0-9_]+:[0-9]{1,20}>", value)
                for key, value in emojis.items())):
            raise ValueError("The review emoji map is invalid")
    engine = ReviewCardEngine(runtime, settings, store, clock, emojis=emojis)
    if settings.ask_diablo:
        engine.diablo = ReviewDiablo(runtime.service, owner_id=settings.owner_id,
            sender_participant=runtime.review.sender_participant,
            recipient_participant=runtime.review.delivery_participant, project=runtime.review.project,
            authorize_item=engine.authorize_item, saver=engine.backend.change, enabled=True)
    if settings.auto_tidy:
        async def added(operation, refs):
            grant = await engine.backend.authorize()
            context = context_from_runtime(runtime, grant=grant)
            return await anyio.to_thread.run_sync(lambda: run_added_tidy(context, clock(),
                                                  batch_id=operation, item_refs=refs, enabled=True))
        engine.auto_tidy = added
        engine.tidy_revision = lambda now: added_tidy_revision(context_from_runtime(runtime), now, enabled=True)
    return engine
