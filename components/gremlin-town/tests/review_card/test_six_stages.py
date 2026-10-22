from datetime import datetime, timezone, timedelta
import pytest
from gremlin_town.review_card.models import review_day
from tests.review_card.test_card import setup, tap, content, controls, NOW, WORK
from gremlin_town.mcp.brain_runtime import WorkRequest
from brain_core import work
from test_work_door import records


def test_calendar_day_before_delivery_and_midnight():
    assert str(review_day(datetime(2026, 10, 4, 21, tzinfo=timezone.utc))) == '2026-10-05'
    assert str(review_day(datetime(2026, 10, 5, 1, tzinfo=timezone(timedelta(hours=-4))), timezone(timedelta(hours=-4)))) == '2026-10-05'


@pytest.mark.anyio
async def test_outcome_progress_is_dated_and_retains_open_work(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        await engine.backend.change(WorkRequest('dated-fiction', 'File envelopes on ' + str(NOW.date() - timedelta(days=1)), 'owner', 'direct', (
            {'move': 'new', 'title': 'File fictional envelopes'},
            {'move': 'date', 'thing': '#1', 'when': str(NOW.date() - timedelta(days=1)), 'kind': 'planned'})), owner_id=WORK.owner_id)
        result = await tap(engine, await engine.open(), 'Start review')
        assert 'Yesterday: how did it go?' in content(result)
        assert 'Made progress' in controls(result)
        result = await tap(engine, result, 'Made progress', {'value': 'Sorted half the envelopes'})
        record = next(iter(work.index_things(records(fake)).values()))
        assert record.state == 'open'
        assert work.has_review_outcome(record, work.index_things(records(fake)), (NOW.date() - timedelta(days=1)).isoformat())
        assert 'Yesterday: how did it go?' not in content(result)
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_open_form_rollover_preserves_words_rechecks_and_restarts(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        result = await tap(engine, await engine.open(), 'Start review')
        token = controls(result)['Add things']
        await engine.prepare_modal(token, owner_user_id='123')
        engine.bind_message(str(NOW.date()), 'old')
        engine.clock = lambda: NOW + timedelta(days=1)
        result = await engine.current()
        engine.bind_message(result.day, 'new')
        assert engine.can_continue(token, submitted=True)
        result = await engine.handle(token, 'submitted-after-midnight', {'value': 'Keep these fictional words'}, owner_user_id='123')
        assert 'submitted words are kept' in content(result)
        assert state['changes'] == 0
        assert store.session(engine._day())['submitted_draft']['value'] == 'Keep these fictional words'
        from gremlin_town.review_card.engine import ReviewCardEngine
        restart = ReviewCardEngine(engine.runtime, engine.settings, store, engine.clock)
        assert 'Keep these fictional words' in content(await restart.current())
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_timezone_selection_and_public_four_part_gate(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        result = await engine.open()
        result = await tap(engine, result, 'Local time zone…', {'value': 'Sydney'})
        assert 'Australia/Sydney' in content(result)
        assert store.latest_timezone() == 'Australia/Sydney'
        engine.clock = lambda: NOW + timedelta(days=1)
        from gremlin_town.review.run import current_review_parts, ReviewContext
        from gremlin_town.review.why_log import WhyLogError
        context = ReviewContext(engine.runtime.review, log, engine.runtime.review_sources, engine.runtime.service)
        with pytest.raises(WhyLogError, match='no sent'):
            current_review_parts(context, engine.clock(), caller='system:review-card', code_callers=frozenset({'system:review-card'}), review_day=engine._day())
        assert (await engine.current()).day == str(engine._day())
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_explicit_resource_and_operation_forms_save_through_real_door(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        await engine.backend.change(WorkRequest('plan-fixture', 'Inspect fictional machine, return to fictional machine', 'owner', 'direct', (
            {'move': 'new', 'title': 'Inspect fictional machine'}, {'move': 'date', 'thing': '#1', 'when': str(NOW.date() - timedelta(days=1)), 'kind': 'planned'},
            {'move': 'new', 'title': 'Return to fictional machine'})), owner_id=WORK.owner_id)
        # The explicit date must occur in the owner words to remain a commitment.
        index = work.index_things(records(fake))
        ref = next(ref for ref,r in index.items() if r.title == 'Inspect fictional machine')
        await engine.backend.change(WorkRequest('set-plan-date', str(NOW.date()-timedelta(days=1)), 'owner', 'direct', ({'move':'date','thing':ref,'when':str(NOW.date()-timedelta(days=1)),'kind':'planned'},)), owner_id=WORK.owner_id)
        result = await tap(engine, await engine.open(), 'Start review')
        result = await tap(engine, result, 'Something changed…')
        result = await tap(engine, result, 'More planning details…')
        assert 'Book shared equipment…' not in controls(result)
        result = await tap(engine, result, 'More options…')
        result = await tap(engine, result, '← Back')
        assert 'More options…' in controls(result)
        assert 'Book shared equipment…' not in controls(result)
        result = await tap(engine, result, 'More options…')
        result = await tap(engine, result, 'Book shared equipment…', {'resource': 'Fictional oven', 'start':'2026-09-30 12:00+08:00','end':'2026-10-01 08:00+08:00'})
        assert work.index_things(records(fake))[ref].data['planning']['resources'][0]['resource'] == 'Fictional oven'
        result = await tap(engine, result, 'Something changed…')
        result = await tap(engine, result, 'More planning details…')
        result = await tap(engine, result, 'More options…')
        result = await tap(engine, result, 'Agree the start and follow-up…', {'followup':'Return to fictional machine','delay':'60','note':'Approve the machine operation and its required return'})
        assert work.index_things(records(fake))[ref].data['planning']['operation']['approved'] is True
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_note_timebox_hold_and_portion_actions_are_canonical(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    try:
        result = await tap(engine, await engine.open(), 'Start review')
        result = await tap(engine, result, 'Add things', {'value': 'Fictional meeting reflection'})
        result = await tap(engine, result, 'Something changed…')
        result = await tap(engine, result, 'Keep as a note…', {'material_kind':'reflection'})
        retained = next(iter(work.index_things(records(fake)).values()))
        assert retained.data['material_kind'] == 'reflection' and retained.state == 'open'
        result = await tap(engine, result, 'Add things', {'value':'Work on fictional tree'})
        ref = next(ref for ref,r in work.index_things(records(fake)).items() if r.title == 'Work on fictional tree')
        result = await tap(engine, result, 'Something changed…')
        result = await tap(engine, result, 'More planning details…')
        result = await tap(engine, result, 'More options…')
        result = await tap(engine, result, 'Add a part or batch…', {'id':'west-row','quantity':'8','ready':'yes','checks':'verified ripe'})
        result = await tap(engine, result, 'Something changed…')
        result = await tap(engine, result, 'More planning details…')
        result = await tap(engine, result, 'More options…')
        result = await tap(engine, result, 'Choose ready parts or batches…', {'ids':'west-row','value':'Approve west-row only today'})
        assert work.index_things(records(fake))[ref].data['planning']['portion_selections'][-1]['portions'] == [{'id':'west-row','quantity':8}]
        result = await tap(engine, result, 'Something changed…')
        result = await tap(engine, result, 'More planning details…')
        result = await tap(engine, result, 'More options…')
        result = await tap(engine, result, 'Update a part or batch…', {'id':'west-row','done_quantity':'3','value':'Completed three from west-row'})
        record = work.index_things(records(fake))[ref]
        assert record.state == 'open' and record.data['planning']['portions'][0]['done_quantity'] == 3
        result = await engine.open()
        # Explicit navigation to choices does not require answering every earlier capture.
        state_view = store.session(engine._day())
        state_view.update(screen='day',stage='day',item=None)
        store.save(engine._day(),state_view)
        result = await engine.current()
        result = await tap(engine, result, 'Set a work session…', {'item':'Work on fictional tree','minutes':'25','value':'25 minutes on west-row'})
        assert work.index_things(records(fake))[ref].data['planning']['work_sessions'][-1]['minutes'] == 25
        result = await tap(engine, result, 'Keep time free…', {'start':'2026-09-30 16:00+08:00','end':'2026-09-30 17:00+08:00','value':'Keep an hour for an unassigned visit'})
        assert any(r.data.get('planning',{}).get('held_time') for r in work.index_things(records(fake)).values())
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_requirement_change_preserves_drop_and_releases_only_affected_successor(tmp_path):
    engine,state,fake,log,store = setup(tmp_path, initial=False)
    try:
        result = await tap(engine, await engine.open(), 'Start review')
        result = await tap(engine,result,'Add things',{'value':'First fictional requirement\nSecond fictional step', 'order':'ordered'})
        index = work.index_things(records(fake))
        first = next(ref for ref,r in index.items() if r.title == 'First fictional requirement')
        second = next(ref for ref,r in index.items() if r.title == 'Second fictional step')
        result = await tap(engine,result,'Drop this task')
        assert work.has_unfinished_prerequisites(second,work.index_things(records(fake)))
        state_view=store.session(engine._day()); state_view.update(screen='day',stage='day',item=None); store.save(engine._day(),state_view)
        result = await engine.current()
        result = await tap(engine,result,'Change an earlier step…',{'item':'Second fictional step','remove':'First fictional requirement','add':'','note':'The first requirement is no longer required for the second step'})
        index=work.index_things(records(fake))
        assert index[first].state == 'dropped' and not work.has_unfinished_prerequisites(second,index)
    finally:
        store.close();log.close()


@pytest.mark.anyio
async def test_routine_outcome_scope_and_reopen_correction(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        await engine.backend.change(WorkRequest('routine-scope','Daily fictional medicine','owner','direct',({'move':'new','title':'Daily fictional medicine','repeat':'daily'},)),owner_id=WORK.owner_id)
        result = await tap(engine,await engine.open(),'Start review')
        result = await tap(engine,result,"Didn't do it")
        record=next(iter(work.index_things(records(fake)).values()))
        assert record.state=='open' and record.data['occurrence']['outcome']=='missed'
        assert record.data['occurrence']['day']==str(NOW.date()-timedelta(days=1))
        await engine.backend.change(WorkRequest('completed-fiction','Completed fictional filing','owner','direct',({'move':'new','title':'Completed fictional filing'},{'move':'tick','thing':'#1'})),owner_id=WORK.owner_id)
        state_view=store.session(engine._day());state_view.update(screen='settled',stage='day',item=None);store.save(engine._day(),state_view)
        result=await engine.current()
        result=await tap(engine,result,'Correct completed work…',{'item':'Completed fictional filing','value':'An attachment was missing; filing is still needed'})
        assert next(r for r in work.index_things(records(fake)).values() if r.title=='Completed fictional filing').state=='open'
    finally:
        store.close();log.close()


@pytest.mark.anyio
async def test_inspect_uncertainty_delegation_and_duplicate_attachment(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Add things',{'value':'Fictional supplier call'})
        result=await tap(engine,result,'Skip for now')
        result=await tap(engine,result,'Add things',{'value':'Fictional supplier call'})
        assert 'Update existing' in controls(result)
        result=await tap(engine,result,'Update existing')
        assert len(work.index_things(records(fake)))==1
        state_view=store.session(engine._day());state_view.update(screen='day',stage='day',item=None);store.save(engine._day(),state_view)
        result=await tap(engine,await engine.current(),'Update an item…',{'value':'Fictional supplier call'})
        assert 'Finished' in controls(result)
        result=await tap(engine,result,'Describe another change…',{'value':'Unclear whether the supplier accepted; keep this uncertainty'})
        record=next(iter(work.index_things(records(fake)).values()))
        assert 'keep this uncertainty' in record.body and record.state=='open'
        result=await tap(engine,result,'Someone else will do it…',{'who':'Fictional Sam','value':'Sam agreed to call; my remaining contribution is checking the reply'})
        record=next(iter(work.index_things(records(fake)).values()))
        assert record.data['whose']=='Fictional Sam' and record.state=='open'
    finally:
        store.close();log.close()


def test_timing_action_correlation_omits_owner_words(caplog):
    import logging
    from gremlin_town.review_card.latency import start_tap,mark_action,phase,finish_tap
    with caplog.at_level(logging.INFO):
        token=start_tap()
        try:
            mark_action('drop')
            with phase('apply_change'):
                pass
            mark_action('private owner note would be unsafe')
        finally:
            finish_tap(token)
    assert 'action=drop' in caplog.text and 'phase=apply_change' in caplog.text
    assert 'private owner note' not in caplog.text
    import re
    taps=re.findall('tap=([0-9a-f]{32})',caplog.text)
    assert len(set(taps))==1
