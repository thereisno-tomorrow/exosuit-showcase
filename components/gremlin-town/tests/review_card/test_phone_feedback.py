"""Owner-visible regressions reported during the fictional phone review."""
from datetime import timedelta
import pytest
from brain_core import work
from gremlin_town.mcp.brain_runtime import WorkRequest
from tests.review_card.test_card import setup, content, controls, NOW, WORK
from test_work_door import records

async def action(engine, result, kind, fields=None, ref=None):
    from tests.review_card.test_card import tap
    for _ in range(4):
        found = [(label, token) for label, token in controls(result).items()
            if (engine.store.action(token)['spec']['action'] == kind or engine.store.action(token)['spec'].get('selected_action') == kind)
            and (ref is None or engine.store.action(token)['spec'].get('ref') == ref)]
        if found:
            return await tap(engine, result, found[0][0], fields)
        if kind == 'back':
            return await tap(engine, result, '← Back', fields)
        result = await tap(engine, result, 'More options…')
    raise AssertionError(f'No offered {kind} control')

@pytest.mark.anyio
async def test_back_revisits_answer_without_undoing_it_and_resumes(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        yesterday = str(NOW.date() - timedelta(days=1))
        await engine.backend.change(WorkRequest("two-yesterday", yesterday, "owner", "direct", (
            {"move":"new", "title":"Call fictional office"},
            {"move":"date", "thing":"#1", "when":yesterday, "kind":"planned"},
            {"move":"new", "title":"Clear fictional desk"},
            {"move":"date", "thing":"#2", "when":yesterday, "kind":"planned"},
        )), owner_id=WORK.owner_id)
        result = await action(engine, await engine.open(), "start")
        first_title = store.session(engine._day())["review_title"]
        result = await action(engine, result, "not_done")
        next_title = store.session(engine._day())["review_title"]
        before = [r.sha256 for r in records(fake)]
        result = await action(engine, result, "back")
        assert first_title in content(result)
        assert "does not undo" in content(result)
        assert store.session(engine._day())["screen"] == "answered"
        assert [r.sha256 for r in records(fake)] == before
        result = await action(engine, result, "resume_review")
        assert next_title in content(result)
        assert store.session(engine._day())["screen"] == "yesterday"
        assert [r.sha256 for r in records(fake)] == before
    finally:
        store.close(); log.close()

@pytest.mark.anyio
async def test_waiting_list_is_explicit_arrival_clears_wait_without_finishing(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        await engine.backend.change(WorkRequest("memo-wait", "Get fictional inspector memo; Submit fictional permit; waiting for fictional inspector memo", "owner", "direct", (
            {"move":"new", "title":"Get fictional inspector memo"},
            {"move":"wait", "thing":"#1", "kind":"needs", "who":"fictional inspector", "what":"memo"},
            {"move":"new", "title":"Submit fictional permit"},
        )), owner_id=WORK.owner_id)
        result = await engine.open()
        assert store.session(engine._day())["screen"] == "open"
        result = await action(engine, result, "waiting_updates")
        assert "Get fictional inspector memo" in content(result)
        assert "Submit fictional permit" not in content(result)
        index = work.index_things(records(fake))
        ref = next(ref for ref,record in index.items() if record.title == "Get fictional inspector memo")
        result = await action(engine, result, "waiting_item", ref=ref)
        result = await action(engine, result, "arrival", {"value":"The memo arrived"})
        index = work.index_things(records(fake))
        assert index[ref].state == "open"
        assert work.status_of(index[ref], index, NOW) != "waiting"
    finally:
        store.close(); log.close()

@pytest.mark.anyio
async def test_waiting_selection_refuses_wait_cleared_elsewhere(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        await engine.backend.change(WorkRequest("wait-one", "Supplier", "owner", "direct", (
            {"move":"new", "title":"Fictional supplier reply"},
            {"move":"wait", "thing":"#1", "kind":"needs", "who":"supplier", "what":"reply"},
        )), owner_id=WORK.owner_id)
        result = await action(engine, await engine.open(), "waiting_updates")
        ref = next(iter(work.index_things(records(fake))))
        await engine.backend.change(WorkRequest("external-arrival", "Reply arrived", "owner", "direct", (
            {"move":"organise", "thing":ref, "clear_wait":True, "note":"Reply arrived"},
        )), owner_id=WORK.owner_id)
        before = [r.sha256 for r in records(fake)]
        result = await action(engine, result, "waiting_item", ref=ref)
        assert "no longer available" in content(result)
        assert [r.sha256 for r in records(fake)] == before
    finally:
        store.close(); log.close()


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["finished", "drop"])
async def test_back_to_closed_item_is_read_only_and_can_continue(tmp_path, ending):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        yesterday = str(NOW.date() - timedelta(days=1))
        await engine.backend.change(WorkRequest("close-yesterday", "First fictional task; Second fictional task; " + yesterday, "owner", "direct", (
            {"move":"new", "title":"First fictional task"},
            {"move":"date", "thing":"#1", "when":yesterday, "kind":"planned"},
            {"move":"new", "title":"Second fictional task"},
            {"move":"date", "thing":"#2", "when":yesterday, "kind":"planned"},
        )), owner_id=WORK.owner_id)
        result = await action(engine, await engine.open(), "start")
        title = store.session(engine._day())["review_title"]
        if ending == "drop":
            result = await action(engine, result, "update")
        result = await action(engine, result, ending)
        # Drop visits the change menu first; Back returns there before the task.
        result = await action(engine, result, "back")
        if store.session(engine._day())["screen"] != "answered":
            result = await action(engine, result, "back")
        assert store.session(engine._day())["screen"] == "answered"
        assert title in content(result)
        kinds = {store.action(token)["spec"]["action"] for token in controls(result).values()}
        assert "inspect_saved" not in kinds
        before = [r.sha256 for r in records(fake)]
        result = await engine.current()
        assert title in content(result)
        assert [r.sha256 for r in records(fake)] == before
        result = await action(engine, result, "resume_review")
        assert store.session(engine._day())["screen"] != "answered"
        assert [r.sha256 for r in records(fake)] == before
    finally:
        store.close(); log.close()


@pytest.mark.anyio
async def test_back_from_manual_reschedule_keeps_the_inspected_item(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        yesterday = str(NOW.date() - timedelta(days=1))
        await engine.backend.change(WorkRequest("manual-identities", "Original task; Other task; " + yesterday, "owner", "direct", (
            {"move":"new", "title":"Original task"},
            {"move":"date", "thing":"#1", "when":yesterday, "kind":"planned"},
            {"move":"new", "title":"Other task"},
        )), owner_id=WORK.owner_id)
        result = await action(engine, await engine.open(), "start")
        result = await action(engine, result, "go_day")
        result = await action(engine, result, "inspect", {"value":"Other task"})
        result = await action(engine, result, "when")
        result = await action(engine, result, "date")
        result = await action(engine, result, "back")
        assert store.session(engine._day())["review_title"] == "Other task"
        assert "## Other task" in content(result)
        assert "## Original task" not in content(result)
    finally:
        store.close(); log.close()


@pytest.mark.anyio
async def test_repeated_back_returns_to_the_interrupted_unanswered_item(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        yesterday = str(NOW.date() - timedelta(days=1))
        moves=[]
        for i in range(3):
            moves.extend(({"move":"new", "title":f"Fictional task {i}"},
                {"move":"date", "thing":f"#{i+1}", "when":yesterday, "kind":"planned"}))
        await engine.backend.change(WorkRequest("three-back", "Three fictional tasks for " + yesterday,
            "owner", "direct", tuple(moves)), owner_id=WORK.owner_id)
        result = await action(engine, await engine.open(), "start")
        result = await action(engine, result, "not_done")
        result = await action(engine, result, "not_done")
        interrupted = store.session(engine._day())["review_ref"]
        before = [r.sha256 for r in records(fake)]
        result = await action(engine, result, "back")
        result = await action(engine, result, "back")
        result = await action(engine, result, "resume_review")
        assert store.session(engine._day())["screen"] == "yesterday"
        assert store.session(engine._day())["review_ref"] == interrupted
        assert [r.sha256 for r in records(fake)] == before
    finally:
        store.close(); log.close()


@pytest.mark.anyio
async def test_actual_outcome_select_form_is_parsed_and_saved(tmp_path):
    from gremlin_town.review_card.host import modal_fields
    from gremlin_town.review_card.render import walk
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        await engine.backend.change(WorkRequest("typed-date-fixture", "Fictional task", "owner", "direct",
            ({"move":"new", "title":"Fictional task"},)), owner_id=WORK.owner_id)
        result = await action(engine, await engine.open(), "go_day")
        result = await action(engine, result, "inspect", {"value":"Fictional task"})
        result = await action(engine, result, "item_more")
        token = next(token for token in controls(result).values() if store.action(token)["spec"]["action"] == "dated_outcome")
        form = await engine.prepare_modal(token, owner_user_id="123")
        fields=[]
        for field in walk(form["data"]["components"]):
            key=field.get("custom_id")
            if key == "kind": fields.append({"type":3,"custom_id":key,"values":["progress"]})
            elif key == "day": fields.append({"type":4,"custom_id":key,"value":str(engine._day())})
            elif key == "value": fields.append({"type":4,"custom_id":key,"value":"Half finished"})
        submitted=modal_fields({"components":fields})
        assert submitted["kind"] == "progress"
        result=await engine.handle(token,"real-outcome-form",submitted,owner_user_id="123")
        index=work.index_things(records(fake))
        record=next(iter(index.values()))
        assert work.has_review_outcome(record,index,str(engine._day()))
        assert record.state == "open"
        assert store.action(token)["status"] == "done", content(result)
    finally:
        store.close();log.close()

@pytest.mark.parametrize("values", [[],["finished","progress"]])
def test_outcome_select_refuses_missing_or_multiple_answers(values):
    from gremlin_town.review_card.host import modal_fields
    with pytest.raises(ValueError):
        modal_fields({"components":[{"type":3,"custom_id":"kind","values":values}]})


@pytest.mark.anyio
async def test_inherited_wait_offers_actual_step_not_its_parent(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        result = await engine.backend.change(WorkRequest("parent-wait", "Fictional paperwork; Get inspector memo", "owner", "direct", (
            {"move":"new", "title":"Fictional paperwork"},
            {"move":"parts", "thing":"#1", "parts":[{"title":"Get inspector memo"}]},
        )), owner_id=WORK.owner_id)
        assert result["status"] == "accepted"
        index = work.index_things(records(fake))
        parent = next(ref for ref, r in index.items() if r.title == "Fictional paperwork")
        child = next(ref for ref, r in index.items() if r.title == "Get inspector memo")
        result = await engine.backend.change(WorkRequest("wait-child", "Waiting for inspector memo", "owner", "direct",
            ({"move":"wait", "thing":child,"kind":"needs","who":"inspector","what":"memo"},)), owner_id=WORK.owner_id)
        assert result["status"] == "accepted"
        view = await engine.backend.fresh(NOW)
        assert view["details"][parent]["status"] == "waiting"
        result = await action(engine, await engine.open(), "waiting_updates")
        refs = {store.action(token)["spec"].get("ref") for token in controls(result).values()
            if store.action(token)["spec"]["action"] == "waiting_item"}
        assert child in refs and parent not in refs
        result = await action(engine, result, "back")
        result = await action(engine, result, "go_day")
        result = await action(engine, result, "inspect", {"value":"Fictional paperwork"})
        assert all(store.action(token)["spec"]["action"] != "arrival" for token in controls(result).values())
    finally:
        store.close();log.close()
