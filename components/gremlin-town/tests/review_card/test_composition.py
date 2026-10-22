import asyncio
from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest

from gremlin_town.contracts import Actor, MessageKind, Packet, ReplyRequest
from gremlin_town.mcp.brain_runtime import WorkRequest
from gremlin_town.review_card.composition import build_engine
from gremlin_town.review_card.host import InteractionHost
from tests.dashboard.test_ask import hub as draft_hub
from tests.review_card.test_card import setup, controls, tap, content, WORK, NOW
from brain_core import work
from test_work_door import records


def composed(tmp_path, hub, *, tidy=False):
    original,state,fake,log,store=setup(tmp_path,initial=False)
    runtime=original.runtime
    settings=replace(original.settings,application_id='102',guild_id='101',morning_channel_id='104',
                     state_file=tmp_path/'card.sqlite',ask_diablo=True,auto_tidy=tidy)
    runtime.discord=settings
    runtime.review=replace(runtime.review,sender_participant='fixture-sender',
                           delivery_participant='fixture-recipient',project='fixture-project')
    runtime.service=hub.service
    runtime.review_handoff_notes=runtime.placement_context=None
    runtime.orientation_context=SimpleNamespace(schedule_enabled=False,sources=SimpleNamespace(schedule=None))
    engine=build_engine(runtime,settings,store,lambda:NOW)
    return engine,state,fake,log,store


@pytest.mark.anyio
async def test_composed_host_serializes_real_draft_save_and_refinement(tmp_path,draft_hub):
    engine,state,fake,log,store=composed(tmp_path,draft_hub)
    host=None
    entered,release=asyncio.Event(),asyncio.Event()
    try:
        await engine.backend.change(WorkRequest('fixture-long','Build fixture project 180 min','owner','direct',(
            {'move':'new','title':'Build fixture project'},
            {'move':'organise','thing':'#1','rough_minutes':180,'length_is_nics':True})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Ask Diablo')
        result=await tap(engine,result,'Research and break it down')
        binding=engine.current_diablo_binding()
        draft_hub.worker.dispatch_once()
        operation=draft_hub.store.operation_by_client('fixture-sender',engine.diablo.prefix+binding['operation_id'])
        request=draft_hub.store.request(operation.request_id)
        packet=Packet(1,MessageKind.RESULT,None,'Fixture draft',json.dumps({'steps':[
            {'title':'Inspect fixture list','detail':'Read each fixture line'}]}),{},(),'','','private')
        draft_hub.service.requests_reply(Actor('fixture-recipient'),ReplyRequest('fixture-result',request.request_id,
            request.revision,MessageKind.RESULT,packet))
        draft_hub.worker.dispatch_once()
        result=await engine.current()
        proof=await engine.authorize_item(WORK.owner_id,binding['day'],binding['line_ref'],binding['item_ref'])
        engine.bind_message(binding['day'],'108')
        class Client:
            def callback(self,*args):
                pass
            def edit(self,*args):
                pass
            def followup(self,*args):
                pass
        refinements=[]
        class Threads:
            def owns_interaction(self,event):
                return False
            async def sync(self):
                return {'outcome':'unchanged'}
            async def on_message(self,event):
                refinements.append(True)
                # The actual adapter still checks the newly saved record; a
                # thread reply to a now-answered item cannot produce a request.
                try:
                    await engine.diablo.refine({**{key:binding[key] for key in ('day','line_ref','item_ref')},
                        'operation_id':'fixture-refine','previous_operation_id':binding['operation_id'],
                        'expected':{'record_id':proof['record_id'],'sha256':proof['record_sha256']},
                        'text':'Use the fixture list'},owner_id=WORK.owner_id)
                except ValueError:
                    return {'outcome':'ignored'}
                pytest.fail('answered draft accepted a new refinement')
        change=engine.backend.change
        async def held(*args,**kwargs):
            entered.set()
            await release.wait()
            return await change(*args,**kwargs)
        engine.backend.change=held
        host=InteractionHost(engine,Client(),engine.settings,threads=Threads())
        event=dict(type=3,id='201',token='fictional',application_id='102',guild_id='101',channel_id='104',
                   member={'user':{'id':'123'}},message={'id':'108'},data={'custom_id':controls(result)['Save this step']})
        before=len(draft_hub.store.fetchall('SELECT operation_id FROM operations'))
        saving=asyncio.create_task(host.interaction(event))
        await asyncio.wait_for(entered.wait(),3)
        reply=asyncio.create_task(host.thread_message({'fictional':'reply'}))
        await asyncio.sleep(0)
        assert not refinements
        assert len(draft_hub.store.fetchall('SELECT operation_id FROM operations'))==before
        assert store.pending(binding['day'])['intent']['request']['expected_records']
        release.set()
        await asyncio.gather(saving,reply)
        assert refinements and len(draft_hub.store.fetchall('SELECT operation_id FROM operations'))==before
    finally:
        release.set()
        if host:
            host.close()
        store.close()
        log.close()


@pytest.mark.anyio
async def test_composed_add_enqueues_tidy_only_after_confirmed_save(tmp_path,draft_hub):
    engine,state,fake,log,store=composed(tmp_path,draft_hub,tidy=True)
    try:
        result=await tap(engine,await engine.open(),'Start review')
        assert not log.name_requests(NOW.date())
        result=await tap(engine,result,'Add things',{'value':'Prepare fictional monthly report'})
        requests=log.name_requests(NOW.date())
        assert len(requests)==1
        assert requests[0]['request']['constraints']['saved_item_refs']==store.session(NOW.date())['added']
        assert state['changes']==1
        await engine.current()
        assert len(log.name_requests(NOW.date()))==1
    finally:
        store.close()
        log.close()


@pytest.mark.anyio
async def test_real_listener_dispatches_ack_then_checked_save_and_closes(tmp_path,draft_hub,monkeypatch):
    import gremlin_town.review_card.run as run_module
    engine,state,fake,log,store=composed(tmp_path,draft_hub)
    engine.settings=replace(engine.settings,ask_diablo=False)
    engine.runtime.discord=engine.settings
    stop=threading.Event()
    calls=[]
    try:
        result=await tap(engine,await engine.open(),'Start review')
        engine.bind_message(NOW.date(),'108')
        store.save_delivery(NOW.date(),route='discord',phase='sent')
        payload=dict(type=5,id='201',token='fictional',application_id='102',guild_id='101',channel_id='104',
            member={'user':{'id':'123'}},message={'id':'108'},data={'custom_id':controls(result)['Add things'],
            'components':[{'type':18,'component':{'custom_id':'value','value':'File fictional receipt'}}]})
        class Client:
            def bot_user(self,*args):
                return '102'
            def callback(self,*args):
                assert state['changes']==0
                calls.append('ack')
            def edit(self,*args):
                body = str(args[-1])
                if 'Received.' in body or 'Saving your checked answer' in body:
                    assert state['changes'] == 0
                    assert calls[0] == 'ack'
                    calls.append('progress')
                else:
                    assert state['changes'] == 1
                    calls.append('edit')
                    stop.set()
        class Gateway:
            def __init__(self,client,event,**kwargs):
                self.event=event
                self.ready=threading.Event()
            def run(self):
                self.ready.set()
                self.event('INTERACTION_CREATE',payload)
                assert stop.wait(5)
            def close(self):
                stop.set()
        monkeypatch.setattr(run_module,'datetime',SimpleNamespace(now=lambda *_:NOW))
        await asyncio.wait_for(run_module.serve(engine.runtime,engine.settings,Client(),stop=stop,
                                               gateway_factory=Gateway),8)
        assert calls==['ack','progress','progress','edit'] and state['changes']==1
    finally:
        stop.set()
        store.close()
        log.close()


@pytest.mark.anyio
async def test_actual_discord_picker_submission_saves_exact_chosen_day(tmp_path,draft_hub):
    engine,state,fake,log,store=composed(tmp_path,draft_hub)
    host=None
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Add things',{'value':'File fictional receipt'})
        result=await tap(engine,result,'Another day…')
        token=next(value for label,value in controls(result).items() if label.startswith('Pick a date'))
        engine.bind_message(NOW.date(),'108')
        calls=[]
        class Client:
            def callback(self,*args):
                calls.append('ack')
            def edit(self,*args):
                calls.append('edit')
            def followup(self,*args):
                pytest.fail('valid date was refused')
        host=InteractionHost(engine,Client(),engine.settings)
        submitted=dict(type=5,id='201',token='fictional',application_id='102',guild_id='101',channel_id='104',
            member={'user':{'id':'123'}},message={'id':'108'},data={'custom_id':token,'components':[
                {'type':18,'component':{'type':3,'custom_id':'week','values':['2026-10-05']}},
                {'type':18,'component':{'type':21,'custom_id':'day','values':['0']}}]})
        assert await host.interaction(submitted)
        item=next(iter(work.index_things(records(fake)).values()))
        assert item.data['dates']['planned']['day']=='2026-10-05'
        assert calls==['ack','edit','edit','edit']
    finally:
        if host:
            host.close()
        store.close()
        log.close()
