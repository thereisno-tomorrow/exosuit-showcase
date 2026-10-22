from datetime import date
import pytest
from brain_core.contracts import canonical_json
from gremlin_town.review_card.actions import make_request

@pytest.mark.parametrize('kind',['transfer','no_longer_required'])
def test_disposition_compiles_only_the_bound_approved_request(kind):
    disposition = {'kind':kind,'from_ref':'old-return','reason':'The fictional plan changed.'}
    if kind == 'transfer':
        disposition.update(to_ref='replacement-return',at='2026-10-05T10:00:00+08:00',assignee='Fictional owner',approved=True)
    proofs = {'source':{'record_id':'source-v1','sha256':'a'*64},'old-return':{'record_id':'old-v1','sha256':'b'*64}}
    spec = {'action':'followup_disposition','label':'Approve return change','ref':'source','expected':proofs['source'],
        'disposition':disposition,'disposition_expected':proofs}
    request = make_request('disposition-once',spec,{'to_ref':'unbound-browser-input'},date(2026,10,5))
    assert request.moves == ({'move':'organise','thing':'source','followup_disposition':disposition},)
    assert request.expected_records == proofs
    assert canonical_json(disposition) in request.message
    assert 'unbound-browser-input' not in request.message
    assert request.answers == ()

from brain_core import work
from gremlin_town.mcp.brain_runtime import WorkRequest
from tests.review_card.test_card import setup, records, controls, content, tap, NOW, WORK

async def started_fixture(engine, fake, *, done=False):
    await engine.backend.change(WorkRequest('return-fixture', 'Fictional pump tasks, each 5 min', 'owner', 'direct', tuple(
        {'move':'new','title':title,'rough_minutes':5,'length_is_nics':True} for title in ('Start fictional pump','Stop fictional pump','Replacement stop'))), owner_id=WORK.owner_id)
    index = work.index_things(records(fake))
    refs = {record.title: ref for ref,record in index.items()}
    source, old, replacement = (refs[title] for title in ('Start fictional pump','Stop fictional pump','Replacement stop'))
    planning = {'operation':{'followup_ref':old,'delay_minutes':30,'approved':True,'actual_start':NOW.isoformat()}}
    await engine.backend.change(WorkRequest('start-fixture', canonical_json(planning), 'owner', 'direct',
        ({'move':'organise','thing':source,'planning':planning},)), owner_id=WORK.owner_id)
    if done:
        await engine.backend.change(WorkRequest('finish-startup', 'Startup finished; return remains required', 'owner', 'direct',
            ({'move':'tick','thing':source},)), owner_id=WORK.owner_id)
    saved=engine.store.session(engine._day());saved.update(screen='planning_details',stage='day',page=0)
    engine.store.save(engine._day(),saved)
    return source,old,replacement

@pytest.mark.anyio
async def test_started_return_has_explicit_change_entry_even_after_startup_done(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        source,old,replacement=await started_fixture(engine,fake,done=True)
        result=await engine.current()
        assert 'Change required return' in controls(result), content(result)
        before=state['changes']
        result=await tap(engine,result,'Change required return')
        result=await choose(engine,result,source)
        assert 'Transfer to another task' in controls(result)
        assert 'No longer required' in controls(result)
        assert state['changes']==before
    finally:
        store.close();log.close()
from datetime import timedelta
from gremlin_town.review_card.render import walk
from gremlin_town.review_card.store import CardStore

async def choose(engine,result,ref):
    select=next(value for value in walk(result.message['components']) if value.get('type')==3)
    token=engine.store.action(select['custom_id'])
    value=next(value for value,item in token['spec']['allowed'].items() if item['ref']==ref)
    return await engine.handle(select['custom_id'],'fictional-select',{'values':[value]},owner_user_id='123')

async def preview(engine,result,source,replacement,kind,*,agreed='agreed',reason='The fictional owner changed the plan.'):
    result=await tap(engine,result,'Change required return')
    if any(value.get('type')==3 for value in walk(result.message['components'])):
        result=await choose(engine,result,source)
    if kind=='transfer':
        result=await tap(engine,result,'Transfer to another task')
        result=await choose(engine,result,replacement)
    else:
        result=await tap(engine,result,'No longer required')
    token=controls(result)['Explain this change…']
    await engine.prepare_modal(token,owner_user_id='123')
    fields={'reason':reason}
    if kind=='transfer': fields.update(assignee='Fictional owner',agreed=agreed)
    result=await engine.handle(token,'fictional-reason',fields,owner_user_id='123')
    while 'More preview' in controls(result): result=await tap(engine,result,'More preview')
    return result

@pytest.mark.anyio
@pytest.mark.parametrize('kind',['transfer','no_longer_required'])
@pytest.mark.parametrize('lost_reply',[False,True])
async def test_exact_approved_return_change_survives_restart_and_midnight(tmp_path,kind,lost_reply):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        source,old,replacement=await started_fixture(engine,fake,done=True)
        original=work.index_things(records(fake))[source].data['planning']['operation'].copy()
        before=state['changes']
        result=await preview(engine,await engine.current(),source,replacement,kind)
        assert state['changes']==before
        assert 'Approve return change' in controls(result),content(result)
        assert 'Neither task is marked finished' in content(result)
        state['lose_reply']=lost_reply
        result=await tap(engine,result,'Approve return change')
        if lost_reply:
            assert 'Check save again' in controls(result)
            store.close();engine.store=CardStore(tmp_path/'card.sqlite')
            result=await tap(engine,await engine.current(),'Check save again')
        index=work.index_things(records(fake));planning=index[source].data['planning']
        assert planning['operation']==original
        assert len(planning['followup_dispositions'])==1
        assert index[old].state=='open' and index[replacement].state=='open'
        engine.clock=lambda: NOW+timedelta(days=1)
        result=await engine.open()
        view=await engine._view(engine.store.session(engine._day()))
        followups=view['planning']['conditional_followups']
        assert any(f['item_ref']==replacement for f in followups) if kind=='transfer' else not followups
    finally:
        engine.store.close();log.close()

@pytest.mark.anyio
async def test_return_preview_refuses_changed_replacement(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        source,old,replacement=await started_fixture(engine,fake)
        result=await preview(engine,await engine.current(),source,replacement,'transfer')
        await engine.backend.change(WorkRequest('intervening-change','Replacement details changed','owner','direct',
            ({'move':'organise','thing':replacement,'note':'Replacement details changed'},)),owner_id=WORK.owner_id)
        before=state['changes']
        result=await tap(engine,result,'Approve return change')
        assert state['changes']==before
        assert 'changed' in content(result).lower()
        assert not work.index_things(records(fake))[source].data['planning'].get('followup_dispositions')
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_transfer_does_not_accept_unagreed_responsibility(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        source,old,replacement=await started_fixture(engine,fake)
        before=state['changes']
        result=await preview(engine,await engine.current(),source,replacement,'transfer',agreed='not_agreed')
        assert 'Approve return change' not in controls(result)
        assert state['changes']==before
    finally:
        store.close();log.close()
@pytest.mark.anyio
async def test_complete_reason_preview_paginated_and_frozen_approval(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        source,old,replacement=await started_fixture(engine,fake)
        result=await tap(engine,await engine.current(),'Change required return')
        result=await choose(engine,result,source)
        result=await tap(engine,result,'No longer required')
        token=controls(result)['Explain this change…'];await engine.prepare_modal(token,owner_user_id='123')
        reason='Long fictional reason '+('*'*960)+' final-tail'
        result=await engine.handle(token,'long-reason',{'reason':reason},owner_user_id='123')
        assert 'Approve return change' not in controls(result)
        pages=[content(result)]
        while 'More preview' in controls(result):
            result=await tap(engine,result,'More preview');pages.append(content(result))
        assert 'final-tail' in '\n'.join(pages)
        approve=controls(result)['Approve return change']
        saved=store.session(engine._day());saved['return_change']['disposition']['reason']='Different unapproved reason';store.save(engine._day(),saved)
        before=state['changes']
        result=await engine.handle(approve,'stale-preview',owner_user_id='123')
        assert state['changes']==before and 'preview changed' in content(result).lower()
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_late_reason_form_cannot_bind_to_another_return_selection(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        source,old,replacement=await started_fixture(engine,fake)
        result=await tap(engine,await engine.current(),'Change required return')
        result=await choose(engine,result,source)
        result=await tap(engine,result,'No longer required')
        token=controls(result)['Explain this change…'];await engine.prepare_modal(token,owner_user_id='123')
        saved=store.session(engine._day());saved['return_change']['from_ref']=replacement;store.save(engine._day(),saved)
        before=state['changes']
        from gremlin_town.review_card.models import CardError
        with pytest.raises(CardError,match='earlier return choice'):
            await engine.handle(token,'old-reason-form',{'reason':'Old form words'},owner_user_id='123')
        assert state['changes']==before
        assert store.session(engine._day())['submitted_draft']['reason']=='Old form words'
    finally:
        store.close();log.close()