from dataclasses import replace
import pytest
from .test_card import setup,tap,controls,content,records
from gremlin_town.review_card.host import InteractionHost
from gremlin_town.review_card.models import CardRenderError

@pytest.mark.anyio
@pytest.mark.parametrize('lost_reply',[False,True])
async def test_render_failure_after_ack_keeps_actual_writer_outcome(tmp_path,lost_reply):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    calls=[];host=None
    try:
        result=await tap(engine,await engine.open(),'Start review')
        token=controls(result)['Add things']
        await engine.prepare_modal(token,owner_user_id='123')
        engine.bind_message(result.day,'108')
        original=engine._draw
        async def failed_draw(*args,**kwargs):
            raise CardRenderError('fictional-private-render-error')
        engine._draw=failed_draw
        counts['lose_reply']=lost_reply
        class Client:
            def callback(self,event,payload): calls.append(('ack',payload))
            def edit(self,*args): calls.append(('edit',args))
            def followup(self,*args): calls.append(('followup',args[-1]))
        settings=replace(engine.settings,application_id='102',guild_id='101',morning_channel_id='104')
        host=InteractionHost(engine,Client(),settings)
        event=dict(type=5,id='201',token='fictional',application_id='102',guild_id='101',channel_id='104',
            member={'user':{'id':'123'}},message={'id':'108'},data={'custom_id':token,'components':[
                {'type':18,'component':{'type':4,'custom_id':'value','value':'Fictional render recovery'}},
                {'type':18,'component':{'type':3,'custom_id':'order','values':['independent']}}]})
        assert await host.interaction(event) is False
        assert calls[0][0]=='ack' and calls[-1][0]=='followup'
        assert 'save result is unchanged' in calls[-1][1]
        assert 'private-render-error' not in str(calls)
        assert store.action(token)['status']==('pending' if lost_reply else 'done')
        assert len(records(fake))>0
        engine._draw=original;branch=fake.branch;before=counts['changes']
        result=await engine.current()
        if lost_reply:
            assert controls(result)['Check save again']==token
            result=await tap(engine,result,'Check save again')
        assert fake.branch==branch and store.action(token)['status']=='done'
    finally:
        if host:host.close()
        store.close();log.close()


@pytest.mark.anyio
async def test_native_button_retries_pending_modal_save_without_reopening_form(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    host=None;calls=[]
    try:
        result=await tap(engine,await engine.open(),'Start review')
        token=controls(result)['Add things']
        await engine.prepare_modal(token,owner_user_id='123')
        engine.bind_message(result.day,'108')
        class Client:
            def callback(self,event,payload): calls.append(('ack',payload))
            def edit(self,*args): calls.append(('edit',args))
            def followup(self,*args): calls.append(('followup',args[-1]))
        settings=replace(engine.settings,application_id='102',guild_id='101',morning_channel_id='104')
        host=InteractionHost(engine,Client(),settings)
        base=dict(token='fictional',application_id='102',guild_id='101',channel_id='104',
            member={'user':{'id':'123'}},message={'id':'108'})
        counts['lose_reply']=True
        submission=dict(base,type=5,id='301',data={'custom_id':token,'components':[
            {'type':18,'component':{'type':4,'custom_id':'value','value':'Fictional native recovery'}},
            {'type':18,'component':{'type':3,'custom_id':'order','values':['independent']}}]})
        assert await host.interaction(submission)
        result=await engine.current()
        assert controls(result)['Check save again']==token
        assert 'Recorded' not in content(result)
        branch=fake.branch;before=counts['changes'];calls.clear()
        retry=dict(base,type=3,id='302',data={'custom_id':token})
        assert await host.interaction(retry)
        assert calls[0]==('ack',{'type':6})
        assert store.action(token)['status']=='done' and fake.branch==branch
        assert counts['changes']==before+1
        assert len([r for r in records(fake) if r.title=='Fictional native recovery'])==1
    finally:
        if host:host.close()
        store.close();log.close()
