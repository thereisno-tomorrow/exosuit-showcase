"""Complete fictional owner journeys through the actual offered controls."""
import socket
import httpx
import pytest
from brain_core import work
from gremlin_town.review_card.store import CardStore
from gremlin_town.review_card.engine import ReviewCardEngine
from .test_card import setup, tap, content, controls, records, NOW, WORK
from gremlin_town.mcp.brain_runtime import WorkRequest

@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        # Windows asyncio uses a local socket pair for its wake-up pipe.
        if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1'}:
            return original(sock,address)
        raise AssertionError('Fictional UX journey attempted an external connection')
    def forbidden(*args,**kwargs):
        raise AssertionError('Fictional UX journey attempted a real HTTP transport')
    monkeypatch.setattr(socket.socket,'connect',connect)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)

async def approve_choices(engine,result):
    label = next(label for label in controls(result) if label.startswith('Review choices'))
    result = await tap(engine,result,label)
    while 'Next summary page' in controls(result):
        result = await tap(engine,result,'Next summary page')
    label = next(label for label in controls(result) if label.startswith("Save today's choices"))
    result = await tap(engine,result,label)
    if engine.store.session(engine._day())['screen'] == 'lengths':
        before = len(engine.runtime.review_why_log.signals_on('day_settled',engine._day()))
        while 'Not sure' in controls(result):
            result = await tap(engine,result,'Not sure')
        assert len(engine.runtime.review_why_log.signals_on('day_settled',engine._day())) == before
        while 'Next summary page' in controls(result):
            result = await tap(engine,result,'Next summary page')
        result = await tap(engine,result,"Save today's choices")
    return result

@pytest.mark.anyio
async def test_empty_review_finishes_with_explicit_approval_then_reopens_choices(tmp_path):
    engine,counts,fake,log,store = setup(tmp_path,initial=False)
    try:
        result = await tap(engine,await engine.open(),'Start review')
        assert 'Anything else to add?' in content(result)
        result = await tap(engine,result,"Choose today's work")
        assert not log.signals_on('day_settled',NOW.date())
        result = await approve_choices(engine,result)
        assert 'saved' in content(result).lower()
        assert len(log.signals_on('day_settled',NOW.date())) == 1
        assert not records(fake)
        result = await tap(engine,result,'Change plan')
        assert store.session(engine._day())['screen'] == 'day'
        assert counts['changes'] == 0
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_fresh_unknown_estimate_journey_preserves_literal_title_and_requires_save(tmp_path):
    engine,counts,fake,log,store = setup(tmp_path,initial=False)
    try:
        result = await tap(engine,await engine.open(),'Start review')
        result = await tap(engine,result,'Add things',{'value':'Buy fictional coffee, tea and cups','order':'independent'})
        result = await tap(engine,result,'Today')
        result = await tap(engine,result,'Not sure')
        assert not log.signals_on('day_settled',NOW.date())
        result = await tap(engine,result,"Today's choices")
        result = await approve_choices(engine,result)
        index = work.index_things(records(fake))
        record = next(iter(index.values()))
        assert record.title == 'Buy fictional coffee, tea and cups'
        assert not record.data.get('timing',{}).get('duration_minutes')
        assert len(log.signals_on('day_settled',NOW.date())) == 1
        assert record.title in content(result)
    finally:
        store.close();log.close()


@pytest.mark.anyio
async def test_interrupted_two_item_review_recovers_answer_then_finishes_day(tmp_path):
    from datetime import timedelta
    engine,counts,fake,log,store = setup(tmp_path,initial=False)
    try:
        yesterday = (NOW.date()-timedelta(days=1)).isoformat()
        await engine.backend.change(WorkRequest('journey-yesterday',yesterday,'owner','direct',(
            {'move':'new','title':'First fictional call'},
            {'move':'date','thing':'#1','kind':'planned','when':yesterday},
            {'move':'new','title':'Second fictional call'},
            {'move':'date','thing':'#2','kind':'planned','when':yesterday})),owner_id=WORK.owner_id)
        result = await tap(engine,await engine.open(),'Start review')
        await engine.prepare_modal(controls(result)['Made progress'],owner_user_id='123')
        result = await tap(engine,result,'Made progress',{'value':'Drafted a fictional question','remaining':'ten'})
        assert 'minutes' in content(result).lower()
        token = controls(result)['Made progress']
        form = await engine.prepare_modal(token,owner_user_id='123')
        restored = {p['component']['custom_id']:p['component'].get('value') for p in form['data']['components']}
        assert restored == {'value':'Drafted a fictional question','remaining':'ten'}
        result = await tap(engine,result,'Made progress',dict(restored,remaining='10'))
        assert '2 of 2' in content(result)
        current = store.session(engine._day())['review_ref']
        result = await tap(engine,result,"Today's choices")
        store.close();engine.store = CardStore(tmp_path/'card.sqlite')
        result = await tap(engine,await engine.current(),'Continue review')
        assert engine.store.session(engine._day())['review_ref'] == current
        assert '2 of 2' in content(result)
        result = await tap(engine,result,"Didn't do it")
        result = await tap(engine,result,"Today's choices")
        result = await approve_choices(engine,result)
        assert 'saved' in content(result).lower()
        assert len(log.signals_on('day_settled',NOW.date())) == 1
    finally:
        engine.store.close();log.close()

@pytest.mark.anyio
async def test_waiting_arrival_returns_to_choices_without_completing_work(tmp_path):
    engine,counts,fake,log,store = setup(tmp_path,initial=False)
    try:
        await engine.backend.change(WorkRequest('journey-wait','Submit fictional permit; waiting for Fictional Pat signed permit; owner estimate 10 min','owner','direct',(
            {'move':'new','title':'Submit fictional permit','rough_minutes':10,'length_is_nics':True},
            {'move':'wait','thing':'#1','kind':'needs','who':'Fictional Pat','what':'signed permit'})),owner_id=WORK.owner_id)
        result = await tap(engine,await engine.open(),"Today's choices")
        result = await tap(engine,result,'Update something waiting…')
        assert 'Fictional Pat' in content(result) and 'signed permit' in content(result)
        result = await tap(engine,result,'Update…')
        result = await tap(engine,result,'What I needed is here…',{'value':''})
        assert 'Continue choosing' in controls(result)
        result = await tap(engine,result,'Continue choosing')
        record = next(iter(work.index_things(records(fake)).values()))
        assert record.state == 'open'
        assert record.data.get('status') != 'waiting'
        result = await tap(engine,result,'○ Add')
        result = await approve_choices(engine,result)
        assert 'saved' in content(result).lower()
        assert len(log.signals_on('day_settled',NOW.date())) == 1
    finally:
        store.close();log.close()


@pytest.mark.anyio
async def test_changed_required_return_journey_transfers_then_saves_complete_day(tmp_path):
    from .test_u8_disposition import started_fixture,preview
    engine,counts,fake,log,store = setup(tmp_path,initial=False)
    try:
        source,old,replacement = await started_fixture(engine,fake,done=True)
        state = store.session(engine._day());state.update(screen='open',stage='',page=0);store.save(engine._day(),state)
        result = await tap(engine,await engine.open(),"Today's choices")
        result = await tap(engine,result,'Check time and follow-ups')
        assert 'Stop fictional pump' in content(result)
        result = await preview(engine,result,source,replacement,'transfer')
        before = counts['changes']
        result = await tap(engine,result,'Approve return change')
        assert counts['changes'] == before+1
        result = await tap(engine,result,'Continue choosing')
        review = next(label for label in controls(result) if label.startswith('Review choices'))
        result = await tap(engine,result,review)
        pages = [content(result)]
        while 'Next summary page' in controls(result):
            result = await tap(engine,result,'Next summary page');pages.append(content(result))
        assert 'Replacement stop' in '\n'.join(pages)
        result = await tap(engine,result,"Save today's choices")
        assert 'saved' in content(result).lower()
        planning = work.index_things(records(fake))[source].data['planning']
        assert planning['operation']['followup_ref'] == old
        assert planning['followup_dispositions'][-1]['to_ref'] == replacement
        assert len(log.signals_on('day_settled',NOW.date())) == 1
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_existing_steps_and_dropped_prerequisite_stay_blocked_through_day_approval(tmp_path):
    engine,counts,fake,log,store = setup(tmp_path,initial=False)
    try:
        await engine.backend.change(WorkRequest('journey-steps','Fictional existing project and ordered steps','owner','direct',(
            {'move':'new','title':'Fictional filing project'},
            {'move':'parts','thing':'#1','parts':[{'title':'Gather fictional forms'},{'title':'Submit fictional forms','then':True}]})),owner_id=WORK.owner_id)
        index = work.index_things(records(fake));refs={r.title:ref for ref,r in index.items()}
        result = await tap(engine,await engine.open(),"Today's choices")
        result = await tap(engine,result,'Update an item…',{'value':'Gather fictional forms'})
        result = await tap(engine,result,'Drop this step')
        index = work.index_things(records(fake))
        assert index[refs['Gather fictional forms']].state == 'dropped'
        assert work.has_unfinished_prerequisites(refs['Submit fictional forms'],index)
        result = await tap(engine,result,"Today's choices")
        result = await tap(engine,result,'Update an item…',{'value':'Fictional filing project'})
        result = await tap(engine,result,'View existing steps and blockers')
        pages = [content(result)]
        while 'Next steps page' in controls(result):
            result = await tap(engine,result,'Next steps page');pages.append(content(result))
        assert 'dropped' in '\n'.join(pages)
        assert 'Only completion satisfies' in '\n'.join(pages)
        result = await tap(engine,result,'Add steps…',{'value':'Compare red, green and blue\nFile fictional copy','order':'ordered'})
        index = work.index_things(records(fake));new={r.title:ref for ref,r in index.items()}
        assert index[new['File fictional copy']].data['after'] == [new['Compare red, green and blue']]
        assert work.has_unfinished_prerequisites(refs['Submit fictional forms'],index)
        result = await tap(engine,result,"Today's choices")
        result = await approve_choices(engine,result)
        assert 'saved' in content(result).lower()
        assert work.has_unfinished_prerequisites(refs['Submit fictional forms'],work.index_things(records(fake)))
    finally:
        store.close();log.close()


@pytest.mark.anyio
async def test_slow_actual_writer_and_lost_reply_keep_durable_exact_intent(tmp_path):
    import asyncio
    engine,counts,fake,log,store = setup(tmp_path,initial=False)
    entered,release=asyncio.Event(),asyncio.Event()
    original=engine.backend.change
    seen=[]
    async def slow(request,**kwargs):
        entered.set()
        await release.wait()
        return await original(request,**kwargs)
    async def progress(stage):
        pending=store.pending(str(engine._day()))
        assert pending and pending['intent']['request']['operation_id']
        shown=engine.action_progress(pending['token'],owner_user_id='123',stage=stage)
        assert 'Saving' in content(shown)
        seen.append(stage)
    pending_task=None
    try:
        engine.backend.change=slow
        result=await tap(engine,await engine.open(),'Start review')
        token=controls(result)['Add things']
        await engine.prepare_modal(token,owner_user_id='123')
        counts['lose_reply']=True
        pending_task=asyncio.create_task(engine.handle(token,'slow-capture',{'value':'Fictional slow item','order':'independent'},owner_user_id='123',progress=progress))
        await asyncio.wait_for(entered.wait(),timeout=5)
        assert seen==['saving']
        assert not records(fake)
        release.set()
        result=await pending_task
        assert controls(result)['Check save again']==token
        branch=fake.branch
        store.close();engine.store=CardStore(tmp_path/'card.sqlite')
        result=await engine.current()
        assert controls(result)['Check save again']==token
        result=await tap(engine,result,'Check save again')
        assert fake.branch==branch
        assert [r.title for r in work.index_things(records(fake)).values()]==['Fictional slow item']
        assert engine.store.action(token)['status']=='done'
    finally:
        release.set()
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
        engine.store.close();log.close()
