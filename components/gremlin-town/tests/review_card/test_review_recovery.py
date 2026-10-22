from datetime import date, timedelta
import pytest
from gremlin_town.review_card.actions import make_request
from gremlin_town.review_card.render import day_label
from tests.review_card.test_card import setup, tap, controls, content, NOW, WORK
from gremlin_town.mcp.brain_runtime import WorkRequest
from brain_core import work
from test_work_door import records

def test_finished_uses_commitment_day():
    request=make_request("finish",dict(action="finished",ref="x",expected={},label="Finished",scope="task",outcome_day="2026-09-29"),{},date(2026,9,30))
    assert request.moves[0]["when"] == "2026-09-29"

def test_appointment_uses_card_zone_and_rejects_dst_gap():
    spec=dict(action="appointment",ref="x",expected={},label="Move",timezone="America/New_York")
    fields=dict(when="2026-10-05 09:00",mode="confirmed",value="Move appointment")
    request=make_request("move",spec,fields,date(2026,9,30))
    assert request.moves[0]["when"] == "2026-10-05T09:00:00-04:00"
    from gremlin_town.review_card.models import CardError
    with pytest.raises(CardError,match="daylight saving"):
        make_request("gap",spec,dict(fields,when="2027-03-14 02:30"),date(2026,9,30))

@pytest.mark.anyio
async def test_batch_duplicate_choice_keeps_other_steps(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),"Start review")
        result=await tap(engine,result,"Add things",{"value":"Existing call"})
        result=await tap(engine,result,"Skip for now")
        result=await tap(engine,result,"Add things",{"value":"Existing call\nSend documents", "order":"ordered"})
        assert "Update existing" in controls(result)
        result=await tap(engine,result,"Update existing")
        index=work.index_things(records(fake))
        assert sorted(r.title for r in index.values()) == ["Existing call","Send documents"]
        first=next(ref for ref,r in index.items() if r.title=="Existing call")
        second=next(r for r in index.values() if r.title=="Send documents")
        assert second.data["after"] == [first]
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_pending_retry_keeps_newer_submitted_draft(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),"Start review")
        result=await tap(engine,result,"Add things",{"value":"Original task"})
        other=controls(result)["Add things\u2026"]
        await engine.prepare_modal(other,owner_user_id="123")
        state["lose_reply"]=True
        result=await tap(engine,result,"Today")
        assert "Check save again" in controls(result)
        result=await engine.handle(other,"save-newer",{"value":"Newer submitted title"},owner_user_id="123")
        result=await tap(engine,result,"Check save again")
        assert store.session(engine._day())["submitted_draft"]["value"]=="Newer submitted title"
        assert "Newer submitted title" in content(result)
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_original_outcome_identity_continues_after_midnight(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        yesterday=str(NOW.date()-timedelta(days=1))
        await engine.backend.change(WorkRequest("old-task", "Review filing "+yesterday,"owner","direct",({"move":"new","title":"Review filing"},{"move":"date","thing":"#1","when":yesterday,"kind":"planned"})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        original=store.session(engine._day())["item"]
        engine.clock=lambda: NOW+timedelta(days=1)
        result=await engine.current()
        continued=store.session(engine._day())
        assert continued["item"]==original
        assert continued["stage"]=="attention"
        assert "You planned this for " + day_label(date.fromisoformat(yesterday)) in content(result)
    finally:
        store.close();log.close()

def test_no_action_inbox_offers_material_directly():
    from gremlin_town.review_card.render import choices
    title,options=choices({"kind":"new_nothing_needed"},{"ref":"x"},{"added":["x"],"day":"2026-09-30"},False)
    assert "When will you do it" not in title
    assert any(action=="classify" for _,action,_ in options)

@pytest.mark.anyio
async def test_captured_task_can_be_put_under_existing_project(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        await engine.backend.change(WorkRequest("project","Fixture house project","owner","direct",({"move":"new","title":"Fixture house"},{"move":"organise","thing":"#1","called":"project"},)),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,"Add things\u2026",{"value":"Call plumber"})
        saved=store.session(engine._day());saved.update(screen="day",stage="day",item=None);store.save(engine._day(),saved)
        result=await tap(engine,await engine.current(),"Update an item\u2026",{"value":"Call plumber"})
        result=await tap(engine,result,'Add to a project…',{"value":"Fixture house"})
        index=work.index_things(records(fake))
        parent=next(ref for ref,r in index.items() if r.title=="Fixture house")
        task=next(r for r in index.values() if r.title=="Call plumber")
        assert task.data.get("parent")==parent, content(result)
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_appointment_reschedule_canonical_clock_and_zone(tmp_path):
    from zoneinfo import ZoneInfo
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        await engine.backend.change(WorkRequest("appointment-fixture","Dentist appointment 2026-10-01T09:00:00+08:00 (Asia/Singapore)","owner","direct",({"move":"new","title":"Dentist"},{"move":"date","thing":"#1","when":"2026-10-01T09:00:00+08:00","appointment_timezone":"Asia/Singapore","kind":"appointment"})),owner_id=WORK.owner_id)
        assert next(iter(work.index_things(records(fake)).values())).data.get("dates",{}).get("appointment"),next(iter(work.index_things(records(fake)).values())).data
        engine.planning_zone=ZoneInfo("America/New_York")
        saved=store.session(engine._day());saved.update(timezone="America/New_York");store.save(engine._day(),saved)
        saved=store.session(engine._day());saved.update(screen="day",stage="day",item=None);store.save(engine._day(),saved)
        result=await tap(engine,await engine.current(),"Update an item\u2026",{"value":"Dentist"})
        result=await tap(engine,result,"Move appointment\u2026",dict(when="2026-10-05 09:00",mode="confirmed",value="Move the dentist appointment"))
        record=next(iter(work.index_things(records(fake)).values()))
        assert record.data["dates"]["appointment"]["day"]=="2026-10-05", content(result)
        assert record.data["dates"]["appointment"]["at"]=="21:00",record.data
        assert record.data["dates"]["appointment"]["words"]=="2026-10-05T09:00:00-04:00 (America/New_York)",record.data
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_host_accepts_genuinely_opened_form_after_message_replacement(tmp_path):
    from dataclasses import replace
    from gremlin_town.review_card.host import InteractionHost
    from tests.review_card.test_host import Client
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    calls=[]
    host=InteractionHost(engine,Client(calls),replace(engine.settings,application_id="102",guild_id="101",morning_channel_id="104"))
    try:
        result=await tap(engine,await engine.open(),'Start review')
        token=controls(result)["Add things"]
        engine.bind_message(result.day,"old-message")
        event=dict(type=3,id="open-form",token="fictional",application_id="102",guild_id="101",channel_id="104",member={"user":{"id":"123"}},message={"id":"old-message"},data={"custom_id":token})
        assert await host.interaction(event)
        assert calls[-1][1]["type"]==9
        engine.clock=lambda:NOW+timedelta(days=1)
        fresh=await engine.current()
        engine.bind_message(fresh.day,"new-message")
        event.update(type=5,id="submitted-form",data={"custom_id":token,"components":[{"type":18,"component":{"custom_id":"value","value":"Keep submitted after replacement"}}]})
        assert await host.interaction(event)
        assert store.session(engine._day())["submitted_draft"]["value"]=="Keep submitted after replacement"
        assert state["changes"]==0
        assert calls[-1]=="edit"
        result=await engine.current()
        assert 'Review earlier submission' in controls(result)
        result=await tap(engine,result,'Review earlier submission')
        assert state["changes"]==0
        token=controls(result)['Save these items']
        result=await engine.handle(token,"confirm-kept",owner_user_id="123")
        assert [r.title for r in work.index_things(records(fake)).values()]==["Keep submitted after replacement"]
        branch=fake.branch
        await engine.handle(token,"retry-kept",owner_user_id="123")
        assert fake.branch==branch and len(work.index_things(records(fake)))==1
        assert "submitted_draft" not in store.session(engine._day())
    finally:
        host.close();store.close();log.close()

@pytest.mark.anyio
async def test_operation_approve_cancel_reapprove_start_keeps_obligation(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    async def menu():
        saved=store.session(engine._day());saved.update(screen="day",stage="day",item=None);store.save(engine._day(),saved)
        result=await tap(engine,await engine.current(),"Update an item\u2026",{"value":"Run oven"})
        result = await tap(engine,result,'More planning details…')
        return await tap(engine,result,'More options…')
    async def approve():
        return await tap(engine,await menu(),'Agree the start and follow-up…',dict(followup="Unload oven",delay="60",note="Approve run and unload"))
    try:
        await engine.backend.change(WorkRequest("operation-fixture","Run oven and Unload oven","owner","direct",({"move":"new","title":"Run oven"},{"move":"new","title":"Unload oven"})),owner_id=WORK.owner_id)
        result=await approve()
        record=next(r for r in work.index_things(records(fake)).values() if r.title=="Run oven")
        assert record.data["planning"]["operation"]["approved"] is True
        result=await tap(engine,await menu(),"Cancel before starting")
        record=next(r for r in work.index_things(records(fake)).values() if r.title=="Run oven")
        assert "operation" not in record.data["planning"],content(result)
        await approve()
        result=await tap(engine,await menu(),'Record when it started…',dict(start=NOW.isoformat(),note="Started oven just now"))
        record=next(r for r in work.index_things(records(fake)).values() if r.title=="Run oven")
        assert record.data["planning"]["operation"]["actual_start"]==NOW.isoformat(),content(result)
        result=await menu()
        assert "Cancel before starting" not in controls(result)
        assert "Required returns remain visible" in content(result)
        record=next(r for r in work.index_things(records(fake)).values() if r.title=="Run oven")
        assert record.data["planning"]["operation"]["actual_start"]==NOW.isoformat()
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_all_day_checks_paginate_without_losing_required_followups(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        saved=store.session(engine._day());saved.update(screen="day",stage="day",item=None);store.save(engine._day(),saved)
        view=await engine._view(store.session(engine._day()))
        view["planning"]={"issues":["Check "+str(n) for n in range(8)],"notices":[],"conditional_followups":[{"item_ref":"fictional-followup","conditional":False,"at":"2026-09-30T15:00:00+08:00"}]}
        from gremlin_town.review_card.render import render,walk
        def issue(label,action,options):return action
        saved.update(screen="planning_details",page=0,day=str(engine._day()))
        first=render(saved,view,issue)
        assert "More checks" in {entry.get("label") for entry in walk(first["components"])}
        saved["page"]=1
        final=render(saved,view,issue)
        text="\n".join(entry.get("content","") for entry in walk(final["components"]))
        assert "Check 7" in text and "15:00 +0800" in text and "Required follow-up" in text
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_unrelated_nonmodal_save_keeps_submitted_draft(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,"Add things",{"value":"Call fictional supplier"})
        saved=store.session(engine._day())
        draft={"submission_id":"earlier-answer","value":"Already submitted note","ref":"other-ref","spec":{"action":"note"}}
        saved["submitted_draft"]=draft
        store.save(engine._day(),saved)
        result=await tap(engine,result,"Today")
        assert store.session(engine._day())["submitted_draft"]==draft
        assert "Already submitted note" in content(result)
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_multiple_matching_titles_require_exact_choice_before_batch_save(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        await engine.backend.change(WorkRequest("duplicate-fixture","Matching call, Matching call","owner","direct",({"move":"new","title":"Matching call"},{"move":"new","title":"Matching call"})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,"Add things\u2026",{"value":"Matching call\nDeliver parcel", "order":"ordered"})
        assert "Choose existing\u2026" in controls(result)
        assert len(work.index_things(records(fake)))==2
        from gremlin_town.review_card.render import walk
        from gremlin_town.review_card.models import CardError
        result=await tap(engine,result,"Choose existing\u2026")
        menu=next(node for node in walk(result.message['components']) if node['type']==3)
        assert len(menu['options'])==2
        with pytest.raises(CardError,match='Choose one item'):
            await engine.handle(menu['custom_id'],'forged-ambiguous-choice',{'values':['0','1']},owner_user_id='123')
        assert len(work.index_things(records(fake)))==2
        chosen=store.action(menu['custom_id'])['spec']['allowed']['0']['ref']
        result=await engine.handle(menu['custom_id'],'choose-current-context',{'values':['0']},owner_user_id='123')
        assert len(work.index_things(records(fake)))==2
        result=await tap(engine,result,'Update existing')
        index=work.index_things(records(fake))
        assert len(index)==3
        assert next(r for r in index.values() if r.title=="Deliver parcel").data["after"]==[chosen]
        assert "Matching call\nDeliver parcel" in index[chosen].body
    finally:
        store.close();log.close()

def test_appointment_display_converts_same_instant_to_card_zone():
    from gremlin_town.review_card.render import appointment_label
    card={"dates":{"appointment":{"day":"2026-10-05","at":"21:00"}}}
    assert appointment_label(card,"America/New_York")=="Appointment \u00b7 05 Oct 09:00 \u00b7 America/New_York"

@pytest.mark.anyio
async def test_new_then_existing_then_new_preserves_both_requirements(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        await engine.backend.change(WorkRequest("reverse-fixture","Prior requirement then Existing second","owner","direct",({"move":"new","title":"Prior requirement"},{"move":"new","title":"Existing second","after":["#1"]})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,"Add things\u2026",{"value":"New first\nExisting second\nNew third", "order":"ordered"})
        assert "will wait for" in content(result) and "New first" in content(result)
        result=await tap(engine,result,"Update existing")
        index=work.index_things(records(fake))
        refs={r.title:ref for ref,r in index.items()}
        assert set(index[refs["Existing second"]].data["after"])=={refs["Prior requirement"],refs["New first"]},content(result)
        assert index[refs["New third"]].data["after"]==[refs["Existing second"]]
        assert "New first\nExisting second\nNew third" in index[refs["Existing second"]].body
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize("closed",[True,False])
async def test_duplicate_attachment_refuses_closed_or_changed_preview_without_any_batch_save(tmp_path,closed):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,"Add things",{"value":"Existing second"})
        result=await tap(engine,result,"Skip for now")
        result=await tap(engine,result,"Add things",{"value":"New first\nExisting second", "order":"ordered"})
        token=controls(result)["Update existing"]
        index=work.index_things(records(fake));ref=next(iter(index))
        move={"move":"tick","thing":ref} if closed else {"move":"organise","thing":ref,"note":"An unrelated update changed this item"}
        await engine.backend.change(WorkRequest("external-change","An unrelated update changed this item","owner","direct",(move,)),owner_id=WORK.owner_id)
        branch=fake.branch
        result=await engine.handle(token,"attach-after-change",owner_user_id="123")
        assert fake.branch==branch and len(work.index_things(records(fake)))==1
        assert "Nothing changed" in content(result)
        assert "Keep separate" in controls(result)
        if closed:
            result=await tap(engine,result,"Keep separate")
            assert len(work.index_things(records(fake)))==3
    finally:
        store.close();log.close()

@pytest.mark.anyio
@pytest.mark.parametrize("offset,zone",[(1,"Asia/Singapore"),(3,"Asia/Singapore"),(1,"America/Los_Angeles")])
async def test_finished_real_door_retains_original_calendar_commitment(tmp_path,offset,zone):
    from zoneinfo import ZoneInfo
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        engine.planning_zone=ZoneInfo(zone)
        day=(NOW.astimezone(engine.planning_zone).date()-timedelta(days=offset)).isoformat()
        saved=store.session(engine._day());saved.update(timezone=zone);store.save(engine._day(),saved)
        await engine.backend.change(WorkRequest("finish-fixture","File envelopes on "+day,"owner","direct",({"move":"new","title":"File envelopes"},{"move":"date","thing":"#1","when":day,"kind":"planned"})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        assert "You planned this for " + day_label(date.fromisoformat(day)) in content(result)
        result=await tap(engine,result,"Finished")
        record=next(iter(work.index_things(records(fake)).values()))
        assert record.state=="done",content(result)
        assert record.data.get("dates",{}).get("done",{}).get("day")==day,record.data
    finally:
        store.close();log.close()

@pytest.mark.anyio
async def test_retained_addition_reuses_words_after_pending_retry_with_fresh_duplicate_choice(tmp_path):
    engine,state,fake,log,store=setup(tmp_path,initial=False)
    try:
        result=await tap(engine,await engine.open(),'Start review')
        result=await tap(engine,result,"Add things",{"value":"Original task"})
        token=controls(result)["Add things\u2026"]
        await engine.prepare_modal(token,owner_user_id="123")
        state["lose_reply"]=True
        result=await tap(engine,result,"Today")
        assert "Check save again" in controls(result)
        result=await engine.handle(token,"newer-capture",{"value":"Original task\nSend documents", "order":"ordered"},owner_user_id="123")
        branch=fake.branch
        result=await tap(engine,result,"Check save again")
        assert fake.branch==branch
        assert store.session(engine._day())["submitted_draft"]["submission_id"]=="newer-capture"
        result=await tap(engine,result,'Review earlier submission')
        result=await tap(engine,result,'Save these items')
        assert "Update existing" in controls(result) and len(work.index_things(records(fake)))==1
        token=controls(result)["Update existing"]
        result=await engine.handle(token,"confirm-existing",owner_user_id="123")
        index=work.index_things(records(fake))
        assert len(index)==2
        first=next(ref for ref,r in index.items() if r.title=="Original task")
        assert next(r for r in index.values() if r.title=="Send documents").data["after"]==[first]
        assert "submitted_draft" not in store.session(engine._day())
        branch=fake.branch
        await engine.handle(token,"retry-existing",owner_user_id="123")
        assert fake.branch==branch
    finally:
        store.close();log.close()
