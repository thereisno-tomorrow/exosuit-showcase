"""Real checked work door, synthetic GitHub, no Discord or other network."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import anyio
import pytest

from brain_core import work
from brain_core.review import ItemRecord
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.mcp.surface_door import bind_owner_tap
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.run import ReviewContext, run_review
from gremlin_town.review.settings import ReviewSettings
from gremlin_town.review_card.engine import ReviewCardEngine
from gremlin_town.review_card.models import CardError
from gremlin_town.review_card.render import walk
from gremlin_town.review_card.settings import DiscordSettings
from gremlin_town.review_card.store import CardStore
from test_work_door import WORK, NOW, records, setup_door, call, request
from tests.dashboard.test_ask import hub as draft_hub


def setup(tmp_path, *, initial=True):
    provider, fake, pins, log = setup_door(tmp_path)
    provider.code_callers = frozenset({"system:review-card"})
    provider.schedule_lengths = True
    if initial:
        call(provider, request(operation="before-review"))
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=(record.id for record in records(fake)))
    review = ReviewSettings.from_table({"enabled": True, "sender_participant": "review-test",
        "delivery_participant": "hermes-wa", "project": "test_project", "why_log_file": str(tmp_path / "why.sqlite3"),
        "opening_deadlines": True, "new_lengths": True})
    class Sender:
        def requests_create(self, actor, command):
            return SimpleNamespace(request_id="fixture-request")
    sources = ReviewSources(brain_items=lambda: tuple(ItemRecord(record, f"records/{record.id}.md") for record in records(fake)),
        calendar_events=lambda *_: (), brain_preferences=lambda: (), open_requests=lambda: ())
    run_review(ReviewContext(review, log, sources, Sender()), NOW - timedelta(minutes=1))
    state = {"allowed": True, "lose_reply": False, "changes": 0, "reads": 0}
    class Brain:
        async def _authorize(self, caller, operation, selector, access):
            assert caller.subject == "system:review-card"
            if not state["allowed"]:
                raise CardError("Access was removed.")
            state["reads"] += 1
            return SimpleNamespace(owner_id=WORK.owner_id, owner_locator="vault://brain-home")
        async def change_work_from_tap(self, caller, selector, change, *, authenticated_owner_id):
            with bind_owner_tap(caller.subject, authenticated_owner_id, change), bind_write_attribution(caller.subject):
                result = await provider.work(WORK, change)
            state["changes"] += 1
            if state["lose_reply"]:
                state["lose_reply"] = False
                raise TimeoutError("synthetic lost reply")
            return result
    runtime = SimpleNamespace(brain_runtime=Brain(), review_sources=sources, review=review, service=Sender(),
        review_why_log=log, flowsavvy=SimpleNamespace(enabled=False), orientation=SimpleNamespace(ranking="points"))
    settings = DiscordSettings(enabled=True, card=True, owner_id=WORK.owner_id, owner_user_id="123",
        dashboard_url="https://dashboard.example.test")
    store = CardStore(tmp_path / "card.sqlite")
    engine = ReviewCardEngine(runtime, settings, store, lambda: NOW)
    engine.test_provider = provider
    return engine, state, fake, log, store


def controls(result):
    return {value["label"]: value["custom_id"] for value in walk(result.message["components"])
            if value.get("type") == 2 and value.get("custom_id")}


def content(result):
    return "\n".join(value.get("content", "") for value in walk(result.message["components"]))


async def review_choices(engine, result):
    label = next(name for name in controls(result) if name.startswith("Review choices"))
    result = await tap(engine, result, label)
    return await final_summary_page(engine, result)


async def final_summary_page(engine, result):
    while "Next summary page" in controls(result):
        result = await tap(engine, result, "Next summary page")
    return result


async def tap(engine, result, label, fields=None):
    """Follow actual offered navigation and opaque choices in the current UI."""
    offered = controls(result)
    if label == 'Add something urgent…':
        label = 'Add things…'
    if label == '← Back' and label not in offered:
        label = next(name for name in offered if name.startswith('← Back to '))
    for _ in range(4):
        if label in offered:
            break
        destination = 'More options…'
        if destination not in offered:
            raise AssertionError(f'{label!r} is unavailable: {list(offered)}')
        result = await engine.handle(offered[destination], 'fictional-more', owner_user_id='123')
        offered = controls(result)
    token = offered[label]
    spec = engine.store.action(token)['spec']
    if spec.get('modal') and fields is not None:
        await engine.prepare_modal(token, owner_user_id='123')
    interaction = 'fictional-' + str(result.revision) + '-' + token[3:11]
    result = await engine.handle(token, interaction, None if spec['action'] == 'begin_selector' else fields, owner_user_id='123')
    if spec['action'] != 'begin_selector' or fields is None:
        return result
    requested = dict(fields)
    for _ in range(20):
        state = engine.store.session(result.day)
        if state['screen'] != 'selector':
            break
        picker = state['selector']
        key = picker['fields'][picker['field_index']]
        wanted = requested.get(key, requested.get('value') if key == 'item' and picker['action'] in {'inspect', 'duplicate_choose'} else None)
        if key == 'add' and not wanted:
            result = await engine.handle(controls(result)['No replacement'], 'fictional-none', owner_user_id='123')
            continue
        values = wanted if isinstance(wanted, list) else ([value.strip() for value in wanted.split(',')] if key in {'tasks','ids'} and isinstance(wanted,str) else [wanted])
        menu = next((node for node in walk(result.message['components']) if node['type'] == 3), None)
        if menu is None:
            return result
        selection = engine.store.action(menu['custom_id'])['spec']
        matches = []
        for value,item in selection['allowed'].items():
            name = item['label'].split('. ', 1)[-1]
            if item['ref'] in values or name in values or item['label'] in values:
                matches.append(value)
        if not matches:
            if 'More choices' in controls(result):
                result = await engine.handle(controls(result)['More choices'], 'fictional-next-page', owner_user_id='123')
                continue
            return result
        result = await engine.handle(menu['custom_id'], 'fictional-select', {'values': matches}, owner_user_id='123')
        if key in {'tasks','ids'}:
            result = await engine.handle(controls(result)['Use these choices'], 'fictional-use-choices', owner_user_id='123')
    if engine.store.session(result.day)['screen'] == 'form_ready':
        token = controls(result)['Continue with these choices…']
        form = await engine.prepare_modal(token, owner_user_id='123')
        shown_fields = {part['component']['custom_id'] for part in form['data']['components']}
        submitted = {key:value for key,value in requested.items() if key in shown_fields}
        result = await engine.handle(token, 'fictional-bound-form', submitted or {'note':''}, owner_user_id='123')
    return result


def test_add_then_process_immediately_date_length_and_settle(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await engine.open()
        assert "Today at a glance" in content(result)
        result = await tap(engine, result, "Start review")
        assert "Anything else to add?" in content(result)
        token = controls(result)["Add things"]
        form = await engine.prepare_modal(token, owner_user_id="123")
        assert form["type"] == 9 and "One item per line" in form["data"]["components"][0]["description"]
        result = await tap(engine, result, "Add things", {"value": "Wash cup\nfind cloth\nwipe table", "order":"ordered"})
        assert "When will you do it?" in content(result), content(result)
        index = work.index_things(records(fake))
        titles = {record.title: record for record in index.values()}
        assert titles["find cloth"].data["after"] == [work.first_id(titles["Wash cup"])]
        assert titles["wipe table"].data["after"] == [work.first_id(titles["find cloth"])]
        assert len(store.session(NOW.date())["added"]) == 3
        result = await tap(engine, result, "Today")
        assert 'How much time does this need?' in content(result), content(result)
        result = await tap(engine, result, "30 min")
        assert all(record.data.get("timing", {}).get("duration_minutes") != 30 or record.data["timing"]["estimated_by"] == "nic"
                   for record in work.index_things(records(fake)).values())
        while "Skip for now" in controls(result):
            result = await tap(engine, result, "Skip for now")
        result = await tap(engine, result, "Choose today's work")
        assert "Most important first" in content(result)
        result = await review_choices(engine, result)
        result = await tap(engine, result, "Save today's choices")
        assert 'Choices saved' in content(result), content(result)
        assert len(log.signals_on("day_settled", NOW.date())) == 1
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_lost_save_reply_restart_retries_exact_intent_without_duplicate(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await tap(engine, await engine.open(), "Start review")
        state["lose_reply"] = True
        result = await tap(engine, result, "Add things", {"value": "File blue receipt"})
        assert "Check save again" in controls(result)
        assert len(work.index_things(records(fake))) == 1
        branch = fake.branch
        restarted = ReviewCardEngine(engine.runtime, engine.settings, store, lambda: NOW)
        result = await tap(restarted, await restarted.current(), "Check save again")
        assert "When will you do it?" in content(result)
        assert fake.branch == branch and len(work.index_things(records(fake))) == 1
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_owner_expiry_revocation_and_stale_navigation_are_fail_closed(tmp_path):
    engine, state, fake, log, store = setup(tmp_path)
    async def run():
        result = await engine.open()
        old = controls(result)["Start review"]
        with pytest.raises(CardError, match="owner"):
            await engine.handle(old, "fixture", owner_user_id="999")
        state["allowed"] = False
        with pytest.raises(CardError, match="Access"):
            await engine.current()
        state["allowed"] = True
        result = await engine.handle(old, "fixture", owner_user_id="123")
        assert state["changes"] == 0
        engine.clock = lambda: NOW + timedelta(days=1)
        with pytest.raises(CardError, match="Replaced"):
            await engine.handle(old, "fixture", owner_user_id="123")
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_calendar_modal_all_weeks_and_past_day_refusal(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await tap(engine, await engine.open(), "Start review")
        result = await tap(engine, result, "Add things", {"value": "Sort envelope"})
        result = await tap(engine, result, "Another day…")
        token = controls(result)["Pick a date…"]
        form = await engine.prepare_modal(token, owner_user_id="123")
        weeks = form["data"]["components"][0]["component"]["options"]
        assert len(weeks) == 25
        assert (date_value(weeks[1]["value"]) - date_value(weeks[0]["value"])).days == 7
        branch = fake.branch
        result = await engine.handle(token, "fictional", {"week": weeks[0]["value"], "day": "0"}, owner_user_id="123")
        assert "That day has passed" in content(result)
        assert fake.branch == branch and any(label.startswith("← Back to ") for label in controls(result))
    from datetime import date
    date_value = date.fromisoformat
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_settlement_replaces_current_picks_without_rewriting_audit(tmp_path):
    engine, state, fake, log, store = setup(tmp_path)
    try:
        ref = next(iter(work.index_things(records(fake))))
        log.set_review_picks([ref], [ref], now=NOW, operation_id="first-yes")
        saved = log.full_reason(ref)
        assert any(row.response == "picked_today" for row in log.history())
        log.set_review_picks([], [ref], now=NOW + timedelta(minutes=2), operation_id="changed-day")
        assert not any(row.response == "picked_today" for row in log.history())
        assert log.full_reason(ref)[:len(saved)] == saved
        log.set_review_picks([ref], [ref], now=NOW, operation_id="first-yes")
        assert not any(row.response == "picked_today" for row in log.history())
        assert len(log.signals_on("day_settled", NOW.date())) == 2
    finally:
        store.close()
        log.close()


def test_estimates_are_saved_before_explicit_day_approval(tmp_path):
    engine, state, fake, log, store = setup(tmp_path)
    async def run():
        result = await tap(engine, await engine.open(), "Today's choices")
        result = await tap(engine, result, "○ Add")
        result = await review_choices(engine, result)
        result = await tap(engine, result, "Save today's choices")
        assert 'How much time does this need?' in content(result)
        assert not log.signals_on("day_settled", NOW.date())
        result = await tap(engine, result, "1 hour")
        assert not log.signals_on("day_settled", NOW.date())
        result = await tap(engine, await final_summary_page(engine, result), "Save today's choices")
        assert 'Choices saved' in content(result), content(result)
        assert len(log.signals_on("day_settled", NOW.date())) == 1
        record = next(iter(work.index_things(records(fake)).values()))
        assert record.data["timing"]["duration_minutes"] == 60
        assert record.data["dates"]["planned"]["day"] == NOW.date().isoformat()
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_private_created_refs_cover_entire_batch_and_exact_retry(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        titles = [f"Synthetic numbered task {number}" for number in range(12)]
        change = WorkRequest("batch-refs", ", ".join(titles), "owner", "direct", tuple({"move": "new", "title": title} for title in titles))
        first = await engine.backend.change(change, owner_id=WORK.owner_id)
        assert first["remote_observed"] is True and len(first["created_refs"]) == 12
        assert len(first["things"]) < 12
        branch = fake.branch
        second = await engine.backend.change(change, owner_id=WORK.owner_id)
        assert second["created_refs"] == first["created_refs"] and second["status"] == "duplicate"
        assert fake.branch == branch
        assert set(first["created_refs"]) == set(work.index_things(records(fake)))
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_modal_prepare_has_no_source_io_and_pending_poll_preserves_buttons(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await tap(engine, await engine.open(), "Start review")
        before = state["reads"]
        await engine.prepare_modal(controls(result)["Add things"], owner_user_id="123")
        assert state["reads"] == before
        async with engine.lock:
            # Modal acknowledgment never waits behind an in-flight save.
            await engine.prepare_modal(controls(result)["Add things"], owner_user_id="123")
        assert await engine.refresh_pending() is None
        assert await engine.refresh_pending() is None
        assert state["reads"] == before
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_drop_is_one_exact_owner_tap_but_ordinary_tool_still_needs_reason(tmp_path):
    from gremlin_town.mcp.brain_runtime import BrainRuntimeError, WorkRequest
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await tap(engine, await engine.open(), "Start review")
        result = await tap(engine, result, "Add things", {"value": "Discard fixture task"})
        ref = next(iter(work.index_things(records(fake))))
        change = WorkRequest("ordinary-drop", "Drop", "owner", "direct", ({"move": "end", "thing": ref, "ending": "dropped"},))
        with bind_write_attribution("hermes-wa"):
            ordinary = await engine.test_provider.work(WORK, change)
        assert work.index_things(records(fake))[ref].state == "open"
        result = await engine.current()
        assert await engine.prepare_modal(controls(result)['Drop this task'], owner_user_id="123") is None
        result = await tap(engine, result, 'Drop this task')
        dropped = work.index_things(records(fake))[ref]
        assert dropped.state == "dropped", content(result)
        assert dropped.data["reason"] == "Owner selected Drop."
        assert dropped.source["words"] == "Drop"
        assert "Why drop" not in content(result)
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_composed_draft_save_lost_reply_replays_frozen_request_and_thread_return(tmp_path, draft_hub):
    import json
    from gremlin_town.contracts import Actor, MessageKind, Packet, ReplyRequest
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.review_card.diablo import ReviewDiablo
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    engine.settings = replace(engine.settings, ask_diablo=True)
    engine.diablo = ReviewDiablo(draft_hub.service, owner_id=WORK.owner_id,
        sender_participant="fixture-sender", recipient_participant="fixture-recipient", project="fixture-project",
        authorize_item=engine.authorize_item, saver=engine.backend.change, enabled=True)
    async def run():
        await engine.backend.change(WorkRequest("long-fixture", "Build fixture project 180 min", "owner", "direct", (
            {"move": "new", "title": "Build fixture project"},
            {"move": "organise", "thing": "#1", "rough_minutes": 180, "length_is_nics": True})), owner_id=WORK.owner_id)
        result = await tap(engine, await engine.open(), "Start review")
        result = await tap(engine, result, "Ask Diablo")
        result = await tap(engine, result, "Research and break it down")
        binding = engine.current_diablo_binding()
        draft_hub.worker.dispatch_once()
        operation = draft_hub.store.operation_by_client("fixture-sender", engine.diablo.prefix + binding["operation_id"])
        requested = draft_hub.store.request(operation.request_id)
        packet = Packet(1, MessageKind.RESULT, None, "Fixture steps", json.dumps({"steps": [
            {"title": "Inspect fixture options", "detail": "Read the fixture list"},
            {"title": "Choose fixture option", "detail": "Write the fixture choice"}]}), {}, (), "", "", "private")
        draft_hub.service.requests_reply(Actor("fixture-recipient"), ReplyRequest("fixture-reply", requested.request_id, requested.revision, MessageKind.RESULT, packet))
        draft_hub.worker.dispatch_once()
        result = await tap(engine, result, "Skip for now")
        before_screen = store.session(NOW.date())["screen"]
        control = await engine.draft_control(binding)
        assert store.session(NOW.date())["screen"] == before_screen
        result = await engine.handle(control["custom_id"], "thread-return", owner_user_id="123")
        assert "Save these steps" in controls(result), content(result)
        state["lose_reply"] = True
        result = await tap(engine, result, "Save these steps")
        assert "Check save again" in controls(result), content(result)
        pending = store.pending(NOW.date())
        assert pending["intent"]["request"]["expected_records"]
        branch = fake.branch
        assert len(work.index_things(records(fake))) == 3
        restarted = ReviewCardEngine(engine.runtime, engine.settings, store, lambda: NOW, diablo=engine.diablo)
        result = await tap(restarted, await restarted.current(), "Check save again")
        assert "Check save again" not in controls(result), content(result)
        assert fake.branch == branch and len(work.index_things(records(fake))) == 3
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_card_uses_local_calendar_day_before_delivery_and_can_show_more_than_ten(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        titles = [f"Fictional batch thing {number}" for number in range(12)]
        await engine.backend.change(WorkRequest("twelve-items", ", ".join(titles), "owner", "direct",
            tuple({"move": "new", "title": title} for title in titles)), owner_id=WORK.owner_id)
        view = await engine.backend.fresh(NOW)
        assert len([line for line in view["parts"][3] if line["item_ref"] in view["details"]]) == 12
        from brain_core.review import MorningInputs, build_morning
        inputs = MorningInputs(tuple(engine.runtime.review_sources.brain_items()), (), (), (), {}, NOW)
        default = build_morning(inputs)
        assert len([line for line in default.parts[2].lines if line.record_sha256]) == 10
        for invalid in (True, 0, 2001):
            with pytest.raises(ValueError, match="section_cap"):
                build_morning(inputs, section_cap=invalid)
        engine.clock = lambda: (NOW + timedelta(days=1)).replace(hour=5)
        result = await engine.current()
        result = await tap(engine, result, "Start review")
        assert "1 of 12" in content(result), content(result)
        again = await engine.current()
        assert store.session(engine.clock().date())["item"] == store.action(next(iter(controls(again).values())))["spec"].get("line")
        fresh = await engine.backend.fresh(engine.clock())
        assert fresh["day"]["date"] == engine.clock().date().isoformat()
        assert fresh["day"]["updated_at"] == engine.clock().isoformat()
        assert fresh["day"]["timeline_start"].startswith(engine.clock().date().isoformat())
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_exact_validated_answer_reference_closes_pre_midnight_line_only(tmp_path):
    from gremlin_town.review.why_log import Surfacing
    engine, state, fake, log, store = setup(tmp_path)
    try:
        ref = next(iter(work.index_things(records(fake))))
        old = NOW - timedelta(days=1)
        log.record_surfacings([Surfacing(old, "review", ref, "a" * 64, "stalled", {}, question='Put aside…?', offered=("park",))])
        rows = log._connection.execute("SELECT id FROM surfacings WHERE item_ref=? AND rule='stalled'", (ref,)).fetchall()
        exact = str(rows[-1]["id"])
        log.mark_today_answered([ref], message='Put aside…', now=NOW)
        assert log.answer_context(exact).open
        log.mark_today_answered([ref], message='Put aside…', now=NOW, validated_references=(exact,))
        assert not log.answer_context(exact).open
    finally:
        store.close()
        log.close()


def test_twenty_ordered_additions_are_atomic_and_retry_identical(tmp_path):
    from gremlin_town.mcp.brain_runtime import BrainRuntimeError, WorkRequest
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await tap(engine, await engine.open(), "Start review")
        token = controls(result)["Add things"]
        words = "\n".join(f"Do fixture step {number}" for number in range(20))
        state["lose_reply"] = True
        result = await tap(engine, result, "Add things", {"value": words, "order":"ordered"})
        assert "Check save again" in controls(result)
        intent = store.action(token)["intent"]
        assert len(intent["request"]["moves"]) == 20
        index = work.index_things(records(fake))
        assert len(index) == 20 and len(records(fake)) == 20
        refs = {record.title: ref for ref, record in index.items()}
        for number in range(20):
            assert index[refs[f"Do fixture step {number}"]].data.get("after", []) == ([refs[f"Do fixture step {number - 1}"]] if number else [])
        branch = fake.branch
        result = await tap(engine, result, "Check save again")
        assert "Check save again" not in controls(result) and fake.branch == branch
        ordinary = WorkRequest("ordinary-after", "Add another fixture", "owner", "direct", (
            {"move": "new", "title": "Add another fixture", "after": [next(iter(index))]},))
        with bind_write_attribution("hermes-wa"), pytest.raises(BrainRuntimeError):
            await engine.test_provider.work(WORK, ordinary)
        assert fake.branch == branch
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_deferred_yes_keeps_original_eligible_and_picks_after_restart(tmp_path):
    import json
    from gremlin_town.mcp.brain_runtime import WorkRequest
    engine, state, fake, log, store = setup(tmp_path)
    async def run():
        result = await tap(engine, await engine.open(), "Today's choices")
        result = await tap(engine, result, "○ Add")
        original = next(iter(work.index_things(records(fake))))
        result = await review_choices(engine, result)
        result = await tap(engine, result, "Save today's choices")
        added = await engine.backend.change(WorkRequest("concurrent-fixture", "Arrange fixture notes 30 min", "owner", "direct", (
            {"move": "new", "title": "Arrange fixture notes"},
            {"move": "organise", "thing": "#1", "rough_minutes": 30, "length_is_nics": True})), owner_id=WORK.owner_id)
        ref = added["created_refs"][0]
        await engine.backend.fresh(NOW)
        log.set_review_picks([ref], [ref], now=NOW, operation_id="other-picks")
        restarted = ReviewCardEngine(engine.runtime, engine.settings, store, lambda: NOW)
        before = len(log.signals_on("day_settled", NOW.date()))
        result = await tap(restarted, await restarted.current(), "1 hour")
        assert len(log.signals_on("day_settled", NOW.date())) == before
        result = await tap(restarted, await final_summary_page(restarted, result), "Save today's choices")
        assert 'Choices saved' in content(result)
        final = json.loads(log.signals_on("day_settled", NOW.date())[-1].detail)
        assert final["picks"] == final["eligible"] == [original]
        assert any(row.item_ref == ref and row.response == "picked_today" for row in log.history())
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_deferred_yes_refuses_if_an_original_selected_item_becomes_ineligible(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    engine, state, fake, log, store = setup(tmp_path)
    async def run():
        original = next(iter(work.index_things(records(fake))))
        added = await engine.backend.change(WorkRequest("second-fixture", "Arrange fixture notes 30 min", "owner", "direct", (
            {"move": "new", "title": "Arrange fixture notes"},
            {"move": "organise", "thing": "#1", "rough_minutes": 30, "length_is_nics": True})), owner_id=WORK.owner_id)
        ref = added["created_refs"][0]
        await engine.backend.fresh(NOW)
        log.set_review_picks([ref], [ref], now=NOW, operation_id="preselected-second")
        result = await tap(engine, await engine.open(), "Today's choices")
        result = await tap(engine, result, "○ Add")
        result = await review_choices(engine, result)
        result = await tap(engine, result, "Save today's choices")
        assert set(store.session(NOW.date())["settle_picks"]) == {original, ref}
        await engine.backend.change(WorkRequest("external-complete", "Done", "owner", "direct",
            ({"move": "tick", "thing": ref},)), owner_id=WORK.owner_id)
        before = len(log.signals_on("day_settled", NOW.date()))
        result = await tap(engine, result, "1 hour")
        assert "Today's choices saved" not in content(result)
        assert "choices changed" in content(result)
        assert len(log.signals_on("day_settled", NOW.date())) == before
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_card_pick_day_survives_midnight_without_changing_other_days_or_audit(tmp_path):
    import json
    engine, state, fake, log, store = setup(tmp_path)
    try:
        ref = next(iter(work.index_things(records(fake))))
        before = NOW.replace(hour=23, minute=58)
        after = before + timedelta(minutes=5)
        log.set_review_picks([ref], [ref], now=before, operation_id="evening-pick")
        audit = log.full_reason(ref)
        log.set_review_picks([], [ref], now=after, operation_id="same-card-unpick")
        assert not any(row.response == "picked_today" for row in log.history())
        assert log.full_reason(ref)[:len(audit)] == audit
        detail = json.loads(log.signals_on("day_settled", after.date())[-1].detail)
        assert detail["review_day"] == before.date().isoformat()
        later = after.replace(hour=7)
        log.set_review_picks([ref], [ref], now=later, operation_id="next-card-pick")
        log.set_review_picks([], [ref], now=after, operation_id="same-card-unpick")
        picked = [row for row in log.history() if row.response == "picked_today"]
        assert len(picked) == 1 and picked[0].responded_at == later
    finally:
        store.close()
        log.close()
