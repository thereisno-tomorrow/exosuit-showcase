"""Real card composition with counted synthetic providers; no network or personal data."""
from collections import Counter
from dataclasses import replace
from datetime import timedelta
import logging
import re

import pytest

from gremlin_town.mcp.brain_runtime import WorkRequest
from gremlin_town.review.run import current_review_part, current_review_parts, ReviewContext
from gremlin_town.review_card.models import CardError
from tests.dashboard.test_ask import hub as draft_hub
from tests.review_card.test_composition import composed
from tests.review_card.test_card import setup, tap, controls, content, WORK, NOW
from test_work_door import records


def counted_sources(engine):
    counts, windows = Counter(), []
    sources = engine.runtime.review_sources
    def wrap(name, reader):
        def read(*args):
            counts[name] += 1
            if name == "calendar":
                windows.append(args)
            return reader(*args)
        return read
    engine.runtime.review_sources = replace(sources,
        brain_items=wrap("items", sources.brain_items),
        brain_preferences=wrap("preferences", sources.brain_preferences),
        calendar_events=wrap("calendar", sources.calendar_events),
        open_requests=wrap("requests", sources.open_requests),
        important_emails=wrap("mail", lambda *_: ()),
        brain_home_size=wrap("size", lambda: ()))
    return counts, windows


@pytest.mark.anyio
async def test_next_card_one_fresh_snapshot_even_with_ask_enabled(tmp_path, draft_hub):
    engine, state, fake, log, store = composed(tmp_path, draft_hub)
    try:
        await engine.backend.change(WorkRequest("fixture-pair", "Two fictional chores 180 min each", "owner", "direct", (
            {"move": "new", "title": "Sort fictional envelopes"},
            {"move": "organise", "thing": "#1", "rough_minutes": 180, "length_is_nics": True},
            {"move": "new", "title": "Wash fictional cup"},
            {"move": "organise", "thing": "#2", "rough_minutes": 180, "length_is_nics": True})), owner_id=WORK.owner_id)
        counts, windows = counted_sources(engine)
        result = await engine.open()
        counts.clear()
        windows.clear()
        result = await tap(engine, result, "Start review")
        assert counts == dict(items=1, preferences=1)
        assert "Ask Diablo" in controls(result)
        first = store.session(NOW.date())["item"]
        counts.clear()
        windows.clear()
        result = await tap(engine, result, "Skip for now")
        assert counts == dict(items=1, preferences=1)
        assert not windows
        assert first != store.session(NOW.date())["item"]
        assert "Ask Diablo" in controls(result)
        raw = {record.id: record for record in records(fake)}
        for token in controls(result).values():
            proof = store.action(token)["spec"].get("expected")
            if proof:
                assert proof["sha256"] == raw[proof["record_id"]].sha256
        for _ in range(6):
            if "Skip for now" not in controls(result):
                break
            result = await tap(engine, result, "Skip for now")
        assert "Anything else to add?" in content(result)
        assert "Ask Diablo" not in controls(result)
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_reuse_ends_at_tap_boundary_and_saves_still_read_after_write(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        result = await tap(engine, await engine.open(), "Start review")
        counts, _ = counted_sources(engine)
        result = await tap(engine, result, "Add things", {"value": "Sort fictional receipt"})
        assert counts["items"] == 2
        assert "When will you do it?" in content(result)
        assert state["changes"] == 1
        counts.clear()
        result = await tap(engine, result, "Another day…")
        assert counts["items"] == 1
        state["allowed"] = False
        counts.clear()
        with pytest.raises(CardError, match="Access"):
            await tap(engine, result, "← Back")
        assert not counts
    finally:
        store.close()
        log.close()


def test_four_parts_match_separate_reads_and_preserve_questions(tmp_path):
    engine, state, fake, log, store = setup(tmp_path)
    try:
        context = ReviewContext(engine.runtime.review, log, engine.runtime.review_sources, engine.runtime.service)
        kwargs = dict(caller="system:review-card", code_callers=frozenset({"system:review-card"}),
                      review_day=NOW.date(), section_cap=2000)
        separate = {part: current_review_part(context, NOW, part=part, **kwargs) for part in (1, 2, 3, 4)}
        together = current_review_parts(context, NOW, **kwargs)
        assert together == separate
        assert current_review_parts(context, NOW, **kwargs) == together
        assert state["changes"] == 0
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_card_timing_log_contains_only_duration(caplog):
    from tests.review_card.test_host import Engine, Client, settings, event
    from gremlin_town.review_card.host import InteractionHost
    calls = []
    host = InteractionHost(Engine(calls), Client(calls), settings())
    try:
        with caplog.at_level(logging.INFO, logger="gremlin_town.review_card.host"):
            assert await host.interaction(event())
        messages = [entry.getMessage() for entry in caplog.records if entry.name == "gremlin_town.review_card.host"]
        assert len(messages) == 1
        assert re.fullmatch(r"Discord card updated in \d+\.\d{3} seconds", messages[0])
    finally:
        host.close()


def test_task_parts_do_not_use_opening_size_notice(tmp_path):
    """A failing capacity check changes Look only, never task questions or picks."""
    engine, state, fake, log, store = setup(tmp_path)
    try:
        calls = []
        def unavailable_size():
            calls.append(True)
            raise RuntimeError("synthetic unavailable capacity source")
        sources = replace(engine.runtime.review_sources, brain_home_size=unavailable_size)
        kwargs = dict(caller="system:review-card", code_callers=frozenset({"system:review-card"}),
                      review_day=NOW.date(), section_cap=2000)
        with_size = current_review_parts(ReviewContext(engine.runtime.review, log, sources, engine.runtime.service), NOW, **kwargs)
        without_size = current_review_parts(ReviewContext(engine.runtime.review, log,
            replace(sources, brain_home_size=None), engine.runtime.service), NOW, **kwargs)
        assert calls == [True]
        assert "synthetic unavailable capacity source" in with_size[1]["text"]
        assert {part: with_size[part] for part in (2, 3, 4)} == {part: without_size[part] for part in (2, 3, 4)}
        assert state["changes"] == 0
    finally:
        store.close()
        log.close()

@pytest.mark.anyio
async def test_enabled_schedule_provider_does_not_block_question_navigation_but_confirms_schedule(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from gremlin_town.flowsavvy.snapshot import ScheduleBlock, ScheduleSnapshot
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    calls = []
    try:
        await engine.backend.change(WorkRequest("slow-schedule-fixture", "Fictional scheduled task · 30 min · " + str(NOW.date()), "owner", "direct", ({"move": "new", "title": "Fictional scheduled task"}, {"move": "organise", "thing": "#1", "rough_minutes": 30, "length_is_nics": True}, {"move": "date", "thing": "#1", "when": str(NOW.date()), "kind": "planned"})), owner_id=WORK.owner_id)
        ref = next(iter(__import__('brain_core.work', fromlist=['index_things']).index_things(records(fake))))
        engine.runtime.flowsavvy = SimpleNamespace(enabled=True)
        def slow_schedule(now):
            calls.append(now)
            time.sleep(0.04)
            return ScheduleSnapshot(fetched_at=now, available=True, message=None, blocks=(
                ScheduleBlock(ref, 'Fictional scheduled task', NOW + timedelta(minutes=5), NOW + timedelta(minutes=35), None),))
        monkeypatch.setattr('gremlin_town.runtime._schedule_reader', lambda _: slow_schedule)
        result = await engine.open()
        assert len(calls) == 1 and 'Fictional scheduled task' in content(result)
        opening = await engine.backend.fresh(NOW, opening=True, full_day=True)
        assert any(slot.get('ref') == ref for slot in opening['day']['timeline'])
        calls.clear()
        result = await tap(engine, result, 'Start review')
        assert not calls, 'Question navigation waited on the enabled slow schedule provider'
        # Confirmation redraw genuinely displays the schedule and must retain its read.
        session = store.session(engine._day())
        session.update(screen='settled', stage='day', item=None)
        store.save(engine._day(), session)
        result = await engine.current()
        assert len(calls) == 1 and 'Fictional scheduled task' in content(result)
    finally:
        store.close()
        log.close()
