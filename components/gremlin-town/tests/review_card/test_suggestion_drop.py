"""Owner Drop must not pretend to answer a suggested-name question."""
import pytest
import anyio
from brain_core import work
from gremlin_town.mcp.brain_runtime import WorkRequest
from gremlin_town.review.why_log import Surfacing
from gremlin_town.review_card.models import CardError
from gremlin_town.review_card.render import choices
from tests.review_card.test_card import setup, tap, controls, content, NOW, WORK
from test_work_door import call, records


@pytest.fixture
def name_card(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    call(engine.test_provider, WorkRequest("fixture-named", "Call fictional office", "owner", "direct",
        ({"move": "new", "title": "Call fictional office"},)))
    record = records(fake)[0]
    log.record_surfacings([Surfacing(NOW, "review", record.id, record.sha256, "stalled",
        {"review_part": "catch_up", "number": 1, "suggested_name": "Ask fictional office about forms"},
        question="Synthetic name question", offered=("keep", "change", "organise", "title", "save"))])
    return engine, state, fake, log, store, record


@pytest.mark.anyio
async def test_suggested_name_drop_is_whole_item_change(name_card):
    engine, state, fake, log, store, original = name_card
    try:
        result = await tap(engine, await engine.open(), "Start review")
        assert 'Use this name?' in content(result)
        assert 'Put aside…' not in controls(result)
        token = controls(result)['Drop this task']
        pinned = store.action(token)["spec"]["expected"]
        assert pinned == {"record_id": original.id, "sha256": original.sha256}
        result = await tap(engine, result, 'Drop this task')
        action = store.action(token)
        assert action["status"] == "done", content(result)
        assert not action["intent"]["request"]["answers"]
        assert action["intent"]["request"]["expected_records"][original.id] == pinned
        latest = work.index_things(records(fake))[original.id]
        assert latest.state == "dropped"
        assert latest.title == original.title
        assert latest.source["words"] == "Drop"
        assert state["changes"] == 1
        assert store.pending(NOW.date()) is None
    finally:
        store.close(); log.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["stale", "revoked", "wrong_owner"])
async def test_whole_item_drop_preserves_freshness_and_owner_checks(name_card, failure):
    engine, state, fake, log, store, original = name_card
    try:
        result = await tap(engine, await engine.open(), "Start review")
        token = controls(result)['Drop this task']
        if failure == "stale":
            await anyio.to_thread.run_sync(call, engine.test_provider, WorkRequest("fixture-change", "Call fictional office after lunch", "owner", "direct",
                ({"move": "organise", "thing": original.id, "title": "Call fictional office after lunch"},)))
            result = await tap(engine, result, 'Drop this task')
            assert store.action(token)["status"] == "refused", content(result)
        else:
            if failure == "revoked":
                state["allowed"] = False
            with pytest.raises(CardError):
                await engine.handle(token, "fixture-owner-check", None,
                    owner_user_id="999" if failure == "wrong_owner" else "123")
        assert work.index_things(records(fake))[original.id].state != "dropped"
        assert state["changes"] == 0
    finally:
        store.close(); log.close()


def test_drop_keeps_reference_when_question_really_offers_it():
    _, answers = choices({"suggested_name": "Synthetic name", "offered": ["title", "drop", "park"]}, {}, {}, False)
    assert next(options for label, _, options in answers if label == 'Drop this task') == {}
    assert 'Put aside…' in [label for label, _, _ in answers]
