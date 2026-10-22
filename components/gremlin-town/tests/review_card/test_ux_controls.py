"""Fictional coverage of less common controls through their offered UI."""
from datetime import timedelta
import pytest
from brain_core import work
from gremlin_town.mcp.brain_runtime import WorkRequest
from .test_card import setup,tap,content,controls,records,NOW,WORK
from .test_suggestion_drop import name_card
from .test_card import draft_hub
from .test_composition import composed
from .test_ux_journeys import approve_choices,no_external_network

async def seed(engine,title='Fictional controls task',**extra):
    return await engine.backend.change(WorkRequest('controls-seed',title+(f" {extra['rough_minutes']} min" if 'rough_minutes' in extra else ''),'owner','direct',
        ({'move':'new','title':title,**extra},)),owner_id=WORK.owner_id)

@pytest.mark.anyio
async def test_accept_suggested_name_via_offered_save(name_card):
    engine,counts,fake,log,store,old=name_card
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Use this name')
        assert work.index_things(records(fake))[old.id].title=='Ask fictional office about forms'
        assert counts['changes']==1
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize('custom',[False,True])
async def test_both_draft_request_controls_use_fictional_provider(tmp_path,draft_hub,custom):
    engine,counts,fake,log,store=composed(tmp_path,draft_hub)
    try:
        await seed(engine,rough_minutes=180,length_is_nics=True)
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await tap(engine,result,'Update an item…',{'value':'Fictional controls task'})
        assert 'Ask Diablo' not in controls(result)
        while not any(n in controls(result) for n in ('Start review','Continue review')):
            result=await tap(engine,result,'\u2190 Back')
        result=await tap(engine,result,'Start review' if 'Start review' in controls(result) else 'Continue review')
        result=await tap(engine,result,'Ask Diablo')
        before=counts['changes']
        result=await tap(engine,result,'In my own words…',{'value':'Draft a fictional checklist.'}) if custom else await tap(engine,result,'Draft the next step')
        binding=engine.current_diablo_binding()
        assert binding and binding['operation_id']
        assert counts['changes']==before
        assert 'Working…' in controls(result)
        from gremlin_town.review_card.render import walk
        assert next(n for n in walk(result.message['components']) if n.get('label')=='Working…')['disabled']
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize('outcome',['Canceled','No show','Cancel this appointment'])
async def test_appointment_controls_are_scoped_to_appointment(tmp_path,outcome):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        when=(NOW+timedelta(days=1)).isoformat()
        await engine.backend.change(WorkRequest('controls-appointment',f'Fictional consultation {when} (Asia/Singapore)','owner','direct',
            ({'move':'new','title':'Fictional consultation'}, {'move':'date','thing':'#1','kind':'appointment','when':when,'appointment_timezone':'Asia/Singapore'})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await tap(engine,result,'Update an item…',{'value':'Fictional consultation'})
        before=counts['changes']
        result=await tap(engine,result,outcome)
        assert counts['changes']==before+1,content(result)
        assert work.index_things(records(fake))[next(iter(work.index_things(records(fake))))].state!='done'
        assert store.pending(engine._day()) is None
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_wait_window_and_someday_forms_keep_task_unfinished(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        await seed(engine)
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await tap(engine,result,'Update an item…',{'value':'Fictional controls task'})
        result=await tap(engine,result,'Waiting for something…',{'who':'Fictional Jo','value':'fictional consent','expected':(NOW.date()+timedelta(days=2)).isoformat()})
        item=next(iter(work.index_things(records(fake)).values()))
        assert item.state=='open' and item.data.get('needs',{}).get('from')=='Fictional Jo',item.data
        result=await tap(engine,result,"Today's choices")
        result=await tap(engine,result,'Update an item…',{'value':'Fictional controls task'})
        result=await tap(engine,result,'More planning details…')
        result=await tap(engine,result,'Set when this can happen…',{'start':(NOW+timedelta(days=1)).isoformat(),'end':(NOW+timedelta(days=1,hours=2)).isoformat()})
        item=next(iter(work.index_things(records(fake)).values()))
        assert item.data['planning']['windows']
        result=await tap(engine,result,"Today's choices")
        result=await tap(engine,result,'Update an item…',{'value':'Fictional controls task'})
        result=await tap(engine,result,'Put aside…')
        result=await tap(engine,result,'Someday')
        item=next(iter(work.index_things(records(fake)).values()))
        assert item.state=='parked',item.data
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_complete_paging_backwards_and_forwards_is_read_only(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        detail='Fictional complete wording '+ 'x'*2000+' FINAL WORD'
        await engine.backend.change(WorkRequest('controls-long',detail+' 15 min','owner','direct',
            ({'move':'new','title':'Fictional long task','details':detail,'rough_minutes':15,'length_is_nics':True},)),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await tap(engine,result,'Set available hours…',{'value':'3'})
        result=await tap(engine,result,'○ Add')
        review=next(n for n in controls(result) if n.startswith('Review choices'))
        result=await tap(engine,result,review)
        first=content(result)
        result=await tap(engine,result,'Next summary page')
        result=await tap(engine,result,'Previous summary page')
        assert content(result)==first
        result=await approve_summary(engine,result)
        first=content(result);before=counts['changes']
        result=await tap(engine,result,'Next saved page')
        result=await tap(engine,result,'Previous saved page')
        assert content(result)==first and counts['changes']==before
    finally:
        store.close();log.close()

async def approve_summary(engine,result):
    while 'Next summary page' in controls(result): result=await tap(engine,result,'Next summary page')
    return await tap(engine,result,"Save today's choices")

@pytest.mark.anyio
async def test_long_schedule_and_candidate_list_all_navigation(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    original=engine.backend.fresh
    async def fresh(*args,**kwargs):
        view=await original(*args,**kwargs)
        if view.get('full_day'):
            view['day']['schedule']=dict(available=True,label='Fictional schedule',fetched_at=NOW.isoformat(),age_seconds=0)
            view['day']['timeline']=[dict(type='task',title=f'Fictional scheduled item {i} '+ 'z'*80,start=NOW.isoformat(),end=(NOW+timedelta(minutes=15)).isoformat()) for i in range(15)]
        return view
    engine.backend.fresh=fresh
    try:
        await engine.backend.change(WorkRequest('controls-many',f'Fictional candidate list; each task takes 15 min on {NOW.date().isoformat()}','owner','direct',tuple(move for i in range(8) for move in ({'move':'new','title':f'Fictional candidate {i}','rough_minutes':15,'length_is_nics':True},{'move':'date','thing':f'#{i+1}','kind':'planned','when':NOW.date().isoformat()}))),owner_id=WORK.owner_id)
        result=await engine.open();first=content(result);before=counts['changes']
        result=await tap(engine,result,'Next schedule page')
        result=await tap(engine,result,'Previous schedule page')
        assert content(result)==first and counts['changes']==before
        result=await tap(engine,result,"Today's choices")
        assert 'Show more' in controls(result),(content(result),[r.data for r in work.index_things(records(fake)).values()])
        while 'Show more' in controls(result): result=await tap(engine,result,'Show more')
        result=await tap(engine,result,'Show first')
        assert store.session(engine._day())['day_offset']==0
        assert counts['changes']==before
        result=await tap(engine,result,next(n for n in controls(result) if n.startswith('Review choices')))
        assert store.session(engine._day())['screen']=='day_summary'
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_previous_saved_notes_are_complete_and_read_only(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        day=(NOW.date()-timedelta(days=1)).isoformat()
        notes='Fictional saved note '+ 'x'*1900+' END OF SAVED NOTE'
        await engine.backend.change(WorkRequest('controls-notes',day+' '+notes,'owner','direct',(
            {'move':'new','title':'First fictional notes task','details':notes}, {'move':'date','thing':'#1','kind':'planned','when':day}, {'move':'organise','thing':'#1','note':notes},
            {'move':'new','title':'Second fictional notes task'}, {'move':'date','thing':'#2','kind':'planned','when':day})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Made progress',{'value':'Fictional outline finished','remaining':'10'})
        result=await tap(engine,result,'← Back');first=content(result);before=counts['changes']
        result=await tap(engine,result,'Next saved notes page')
        result=await tap(engine,result,'Previous saved notes page')
        assert content(result)==first
        seen=content(result)
        while 'Next saved notes page' in controls(result):
            result=await tap(engine,result,'Next saved notes page');seen+=content(result)
        assert 'END OF SAVED NOTE' in seen and counts['changes']==before
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_weekly_review_reachable_after_saved_friday(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    engine.clock=lambda: NOW+timedelta(days=(4-NOW.weekday())%7)
    try:
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await approve_choices(engine,result)
        before=counts['changes']
        result=await tap(engine,result,'Weekly review')
        while 'Next section' in controls(result): result=await tap(engine,result,'Next section')
        assert store.session(engine._day())['screen']=='weekly'
        assert counts['changes']==before
        assert any(label.startswith('← Back') for label in controls(result))
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_rename_form_uses_offered_suggested_name_context(name_card):
    engine,counts,fake,log,store,old=name_card
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Change name\u2026',{'value':'A clearer fictional name'})
        assert work.index_things(records(fake))[old.id].title=='A clearer fictional name'
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize('finish',[False,True])
async def test_next_step_form_on_real_stalled_question(tmp_path,finish):
    from gremlin_town.review.why_log import Surfacing
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        await seed(engine)
        old=records(fake)[0]
        await engine.backend.change(WorkRequest('completed-next','Fictional next action: read fictional form, now finished','owner','direct',(
            {'move':'next_action','thing':old.id,'text':'Read fictional form'},
            {'move':'tick','thing':old.id,'what':'next_action'})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        for _ in range(5):
            if 'Type the next step\u2026' in controls(result):break
            result=await tap(engine,result,'Skip for now')
        result=await tap(engine,result,'Done') if finish else await tap(engine,result,'Type the next step\u2026',{'value':'Call the fictional counter'})
        latest=work.index_things(records(fake))[old.id]
        assert latest.state=='done' if finish else latest.data['next_action']['text']=='Call the fictional counter'
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_changed_missed_deadline_form(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        day=(NOW.date()-timedelta(days=1)).isoformat()
        await engine.backend.change(WorkRequest('missed-control',f'Fictional deadline {day}','owner','direct',(
            {'move':'new','title':'Fictional deadline'}, {'move':'date','thing':'#1','kind':'deadline','when':day})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        if 'Change' not in controls(result): result=await tap(engine,result,'Skip for now')
        result=await tap(engine,result,'Change',{'value':'Changed fictional deadline task'})
        assert next(iter(work.index_things(records(fake)).values())).title=='Changed fictional deadline task'
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize('choice',['Tomorrow','monday','week','picker'])
async def test_all_park_date_controls(tmp_path,choice):
    from gremlin_town.review_card.render import day_label
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        await seed(engine)
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await tap(engine,result,'Update an item\u2026',{'value':'Fictional controls task'})
        result=await tap(engine,result,'Put aside\u2026')
        monday=NOW.date()+timedelta(days=(7-NOW.weekday()) or 7)
        label={'Tomorrow':'Tomorrow','monday':day_label(monday),'week':'In a week \u00b7 '+day_label(NOW.date()+timedelta(days=7)),'picker':'Pick a date\u2026'}[choice]
        fields={'week':monday.isoformat(),'day':'0'} if choice=='picker' else None
        result=await tap(engine,result,label,fields)
        assert store.pending(engine._day()) is None
        assert next(iter(work.index_things(records(fake)).values())).state=='parked',content(result)
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize('choice',['monday','week'])
async def test_other_planned_date_buttons(tmp_path,choice):
    from gremlin_town.review_card.render import day_label
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        await seed(engine)
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Another day\u2026')
        when=NOW.date()+timedelta(days=((7-NOW.weekday()) or 7) if choice=='monday' else 7)
        result=await tap(engine,result,('In a week \u00b7 ' if choice=='week' else '')+day_label(when))
        assert next(iter(work.index_things(records(fake)).values())).data['dates']['planned']['day']==when.isoformat()
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize('minutes',[15,120])
async def test_short_and_long_estimate_buttons(tmp_path,minutes):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Add things',{'value':'Fictional estimate task','order':'independent'})
        result=await tap(engine,result,'Today')
        result=await tap(engine,result,'15 min' if minutes==15 else '2 hours')
        assert next(iter(work.index_things(records(fake)).values())).data['timing']['duration_minutes']==minutes
        assert not log.signals_on('day_settled',engine._day())
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_project_drop_and_typed_steps_controls(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        await seed(engine,rough_minutes=180,length_is_nics=True)
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,'Type the steps\u2026',{'value':'First fictional step\nSecond fictional step','order':'ordered'})
        result=await tap(engine,result,"Today's choices")
        result=await tap(engine,result,'Update an item\u2026',{'value':'Fictional controls task'})
        parent=next(ref for ref,r in work.index_things(records(fake)).items() if r.title=='Fictional controls task')
        await engine.backend.change(WorkRequest('controls-project-kind','This is a project','owner','direct',({'move':'organise','thing':parent,'called':'project'},)),owner_id=WORK.owner_id)
        result=await engine.current()
        result=await tap(engine,result,'Drop this project')
        index=work.index_things(records(fake))
        assert index[parent].state=='dropped' and all(work.status_of(ref,index,NOW)=='ended' for ref in index),content(result)
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_summary_change_and_check_back_navigation(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await tap(engine,result,'Check time and follow-ups')
        result=await tap(engine,result,'\u2190 Back to choices')
        result=await tap(engine,result,next(n for n in controls(result) if n.startswith('Review choices')))
        before=counts['changes']
        result=await tap(engine,result,'Change choices')
        assert store.session(engine._day())['screen']=='day' and counts['changes']==before
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_return_preview_previous_page_retains_frozen_approval(tmp_path):
    from .test_u8_disposition import started_fixture,preview
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        source,old,replacement=await started_fixture(engine,fake,done=True)
        result=await tap(engine,await engine.current(),'Change required return')
        result=await tap(engine,result,'\u2190 Back to checks')
        result=await preview(engine,result,source,replacement,'transfer',reason='Fictional explicit approval reason '+ 'r'*850)
        before=counts['changes'];spec=store.action(controls(result)['Approve return change'])['spec']
        result=await tap(engine,result,'Previous preview')
        while 'More preview' in controls(result):result=await tap(engine,result,'More preview')
        assert store.action(controls(result)['Approve return change'])['spec']['disposition']==spec['disposition']
        assert counts['changes']==before
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_appointment_reschedule_primary_control(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        when=(NOW+timedelta(days=1)).isoformat()
        await engine.backend.change(WorkRequest('controls-reschedule',f'Fictional consultation {when} (Asia/Singapore)','owner','direct',(
            {'move':'new','title':'Fictional consultation'}, {'move':'date','thing':'#1','kind':'appointment','when':when,'appointment_timezone':'Asia/Singapore'})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),"Today's choices")
        result=await tap(engine,result,'Update an item\u2026',{'value':'Fictional consultation'})
        result=await tap(engine,result,'Reschedule appointment\u2026')
        result=await tap(engine,result,'Tomorrow')
        assert next(iter(work.index_things(records(fake)).values())).data['dates']['appointment']['day']==(NOW.date()+timedelta(days=1)).isoformat()
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_new_deadline_primary_control(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        day=(NOW.date()-timedelta(days=1)).isoformat()
        await engine.backend.change(WorkRequest('deadline-controls',f'Fictional deadline {day}','owner','direct',(
            {'move':'new','title':'Fictional deadline'}, {'move':'date','thing':'#1','kind':'deadline','when':day})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        if 'Set a new deadline\u2026' not in controls(result):result=await tap(engine,result,'Skip for now')
        result=await tap(engine,result,'Set a new deadline\u2026')
        result=await tap(engine,result,'Tomorrow')
        assert next(iter(work.index_things(records(fake)).values())).data['dates']['deadline']['day']==(NOW.date()+timedelta(days=1)).isoformat()
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_informational_question_can_become_action(tmp_path):
    from gremlin_town.review.why_log import Surfacing
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        await seed(engine)
        old=records(fake)[0]
        log.record_surfacings([Surfacing(NOW,'review',old.id,old.sha256,'new_nothing_needed',
            {'review_part':'new','number':1},question='Fictional note or action?',offered=('organise','keep'))])
        result=await tap(engine,await engine.open(),'Start review')
        for _ in range(5):
            if 'This needs action' in controls(result):break
            result=await tap(engine,result,'Skip for now')
        result=await tap(engine,result,'This needs action')
        result=await tap(engine,result,'Tomorrow')
        assert work.index_things(records(fake))[old.id].data.get('dates',{}).get('planned',{}).get('day')==(NOW.date()+timedelta(days=1)).isoformat(),content(result)
    finally:
        store.close();log.close()
