import pytest, anyio
from dataclasses import replace
from brain_core import work
from brain_core.review import MorningInputs, ItemRecord, build_morning, with_name_suggestions
from gremlin_town.mcp.brain_runtime import WorkRequest
from gremlin_town.review.run import _current_part, ReviewContext
from gremlin_town.review_card.render import choices
from tests.review_card.test_card import setup, NOW, WORK
from test_work_door import call, records

def make_chain(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    change=WorkRequest('synthetic-chain', 'First synthetic step Second synthetic step Third synthetic step', 'owner','direct',
        tuple({'move':'new','title':title} for title in ['First synthetic step','Second synthetic step','Third synthetic step']))
    call(engine.test_provider, change)
    originals=records(fake)
    modified=[]
    for i,r in enumerate(originals):
        modified.append(replace(r,data={**r.data,'review_marks':[{'mark':'question','text':'Its name?'}],
            **({'after':[originals[i-1].id]} if i else {})}))
    engine.runtime.review_sources=replace(engine.runtime.review_sources,
        brain_items=lambda: tuple(ItemRecord(r, 'synthetic/'+r.id+'.md') for r in modified))
    engine.review_probe_fake = fake
    return engine, modified, log, store


def test_chain_queue_waits_for_done(tmp_path):
    engine, rows, log, store=make_chain(tmp_path)
    try:
        index=work.index_things(rows)
        assert work.has_unfinished_prerequisites(rows[1],index)
        assert work.has_unfinished_prerequisites(rows[2],index)
        view=anyio.run(engine.backend.fresh,NOW)
        queue=engine._queue({'screen':'new'},view)
        assert {line['item_ref'] for line in queue} == {rows[0].id}
        words_only=replace(rows[0],data={**rows[0].data,'review_marks':[], 'next_action':{'text':'A physical step','done':True}})
        assert work.has_unfinished_prerequisites(rows[1],work.index_things([words_only,*rows[1:]]))
        completed=replace(words_only,state='done')
        assert not work.has_unfinished_prerequisites(rows[1],work.index_things([completed,*rows[1:]]))
        assert work.has_unfinished_prerequisites(rows[2],work.index_things([completed,*rows[1:]]))
        assert not work.has_unfinished_prerequisites(rows[2],work.index_things([completed,replace(rows[1],state='done'),rows[2]]))
    finally: store.close(); log.close()

def test_saved_blocked_questions_hidden_without_deleting_authority(tmp_path):
    from gremlin_town.review.why_log import Surfacing
    engine, rows, log, store=make_chain(tmp_path)
    try:
        for i,record in enumerate(rows[1:],1):
            log.record_surfacings([Surfacing(NOW,'review',record.id,record.sha256,'needs_you',
                {'review_part':'new','number':i}, question='Synthetic name question',offered=('title',))])
        saved=log.review_part_state('new', original=(),now=NOW,review_day=NOW.date())
        assert len(saved)==2
        view=anyio.run(engine.backend.fresh,NOW)
        assert not {record.id for record in rows[1:]} & {line['item_ref'] for line in view['parts'][3]}
        assert all(log.answer_context(line['reference']).open for line in saved)
        assert len(log.review_part_state('new',original=(),now=NOW,review_day=NOW.date()))==3
    finally: store.close(); log.close()

def test_retained_name_only_catch_question_does_not_offer_next(tmp_path):
    from gremlin_town.review.why_log import Surfacing
    from tests.review_card.test_card import controls, tap
    engine, rows, log, store=make_chain(tmp_path)
    try:
        record=rows[0]
        engine.runtime.review_sources=replace(engine.runtime.review_sources,
            brain_items=lambda: tuple(ItemRecord(r,'synthetic/'+r.id+'.md') for r in records(engine.review_probe_fake)))
        log.record_surfacings([Surfacing(NOW,'review',record.id,record.sha256,'stalled',
            {'review_part':'catch_up','number':1},question='Synthetic name question',offered=('title',))])
        async def run():
            result=await tap(engine,await engine.open(),'Start review')
            assert 'Type the next step…' not in controls(result)
            assert 'Change name…' in controls(result)
        anyio.run(run)
    finally: store.close();log.close()

@pytest.mark.parametrize('state',['open','parked','dropped','no_longer_needed'])
def test_ancestor_after_blocks_child_until_literal_done(tmp_path,state):
    engine, rows, log, store=make_chain(tmp_path)
    try:
        blocker=replace(rows[0],state=state)
        parent=replace(rows[1],data={**rows[1].data,'called':'project'})
        child=replace(rows[2],data={**rows[2].data,'after':[],'parent':parent.id})
        index=work.index_things([blocker,parent,child])
        assert work.has_unfinished_prerequisites(child,index)
        assert work.has_unfinished_prerequisites(child,work.index_things([parent,child]))
        assert not work.has_unfinished_prerequisites(child,work.index_things([replace(blocker,state='done'),parent,child]))
    finally:store.close();log.close()

def test_waiting_on_person_remains_reviewable(tmp_path):
    from datetime import timedelta
    engine,rows,log,store=make_chain(tmp_path)
    try:
        waiting=replace(rows[0],created_at=NOW-timedelta(days=10),updated_at=NOW-timedelta(days=10),
            data={**rows[0].data,'review_marks':[],'needs':{'from':'Fictional helper','what':'Synthetic reply'}})
        index=work.index_things([waiting])
        assert not work.has_unfinished_prerequisites(waiting,index)
        morning=build_morning(MorningInputs((ItemRecord(waiting,'synthetic.md'),),(),(),(),{},NOW))
        assert any(line.rule=='to_chase' for part in morning.parts for line in part.lines)
    finally:store.close();log.close()

def test_task_views_omit_providers_but_day_and_back_refresh(tmp_path):
    from tests.review_card.test_card import controls,tap
    from tests.review_card.test_latency import counted_sources
    engine,rows,log,store=make_chain(tmp_path)
    try:
        counts,_=counted_sources(engine)
        async def run():
            result=await engine.open()
            assert counts['calendar']==1
            counts.clear()
            result=await tap(engine,result,'Start review')
            assert counts['calendar']==0 and counts['mail']==0
            while 'Skip for now' in controls(result):
                result=await tap(engine,result,'Skip for now')
            assert counts['calendar']==0 and counts['mail']==0
            result=await tap(engine,result,"Choose today's work")
            assert counts['calendar']==1
            assert engine.store.session(NOW.date())['screen']=='day'
            result=await tap(engine,result,'← Back')
            # Back reads fresh records, but the destination needs no day providers.
            assert counts['calendar']==1
        anyio.run(run)
    finally:store.close();log.close()

def test_ask_authorization_rejects_retained_blocked_item(tmp_path):
    from gremlin_town.review.why_log import Surfacing
    from gremlin_town.review_card.models import CardError
    engine,rows,log,store=make_chain(tmp_path)
    try:
        record=rows[1]
        log.record_surfacings([Surfacing(NOW,'review',record.id,record.sha256,'needs_you',
            {'review_part':'new','number':1},question='Synthetic next question',offered=('next_action',))])
        (saved,)=log.review_part_state('new',original=(),now=NOW,review_day=NOW.date())
        async def run():
            with pytest.raises(CardError):
                await engine.authorize_item(WORK.owner_id,str(NOW.date()),saved['reference'],record.id)
        anyio.run(run)
        assert log.answer_context(saved['reference']).open
    finally:store.close();log.close()


def test_old_button_cannot_write_when_dependency_appears(tmp_path):
    from tests.review_card.test_card import controls,content,tap
    engine,rows,log,store=make_chain(tmp_path)
    try:
        async def run():
            result=await tap(engine,await engine.open(),'Start review')
            token=controls(result)['Change name…']
            head=engine.review_probe_fake.branch
            blocker=replace(rows[0],id='brn_synthetic_blocker',title='Synthetic new prerequisite',
                data={**rows[0].data,'review_marks':[]})
            rows[0]=replace(rows[0],data={**rows[0].data,'after':[blocker.id]})
            rows.append(blocker)
            result=await engine.handle(token,'synthetic-stale-button',{'value':'Synthetic new name'},owner_user_id='123')
            assert 'Check the fresh question before answering' in content(result)
            assert engine.review_probe_fake.branch==head
            assert store.action(token)['status']=='ready'
        anyio.run(run)
    finally:store.close();log.close()


def test_unmarked_child_of_blocked_parent_is_not_a_day_choice(tmp_path):
    from brain_core.review import MorningBootstrap
    engine,rows,log,store=make_chain(tmp_path)
    try:
        first=replace(rows[0],data={**rows[0].data,'review_marks':[]})
        parent=replace(rows[1],data={**rows[1].data,'review_marks':[]})
        child=replace(rows[2],data={**rows[2].data,'review_marks':[],'after':[],'parent':parent.id})
        def day_choices(values):
            args=MorningInputs(tuple(ItemRecord(r,'synthetic/'+r.id+'.md') for r in values),(),(),(),{},NOW,
                bootstrap=MorningBootstrap('a'*40,frozenset(r.id for r in values)))
            return {line.item_ref for part in build_morning(args).parts for line in part.lines if line.rule=='your_day'}
        assert child.id not in day_choices([first,parent,child])
        assert child.id in day_choices([replace(first,state='done'),parent,child])
    finally:store.close();log.close()


def test_ask_checks_fresh_records_and_owner_without_calendar(tmp_path):
    from tests.review_card.test_latency import counted_sources
    from gremlin_town.review_card.models import CardError
    engine,rows,log,store=make_chain(tmp_path)
    try:
        rows[0]=replace(rows[0],data={**rows[0].data,'review_marks':[{'mark':'question','text':"What's next?"}]})
        counts,_=counted_sources(engine)
        async def run():
            view=await engine.backend.fresh(NOW,full_day=False)
            line=next(line for line in view['parts'][3] if line['item_ref']==rows[0].id)
            counts.clear()
            result=await engine.authorize_item(WORK.owner_id,str(NOW.date()),line['reference'],rows[0].id)
            assert 'next_action' in result['offered']
            assert counts['items']==1 and counts['calendar']==0 and counts['mail']==0
            counts.clear()
            with pytest.raises(CardError):
                await engine.authorize_item('synthetic-wrong-owner',str(NOW.date()),line['reference'],rows[0].id)
            assert not counts
        anyio.run(run)
    finally:store.close();log.close()


def test_card_skips_request_query_without_changing_global_morning_or_controls(tmp_path):
    from brain_core.review import OpenRequest
    from gremlin_town.review.run import current_review_parts
    engine,rows,log,store=make_chain(tmp_path)
    try:
        calls=[]
        request=OpenRequest('req_synthetic_external','synthetic-worker',NOW.date(),
            objective='Synthetic returned work',work_state='result_returned',proof='Synthetic proof')
        def requests():
            calls.append(True)
            return (request,)
        original=replace(engine.runtime.review_sources,open_requests=requests)
        engine.runtime.review_sources=original
        async def run():
            view=await engine.backend.fresh(NOW,full_day=False)
            assert not calls
            assert engine.runtime.review_sources is original
            context=ReviewContext(engine.runtime.review,log,original,engine.runtime.service)
            global_parts=current_review_parts(context,NOW,caller='system:review-card',
                code_callers=frozenset({'system:review-card'}),review_day=NOW.date(),section_cap=2000)
            assert len(calls)==1
            assert any(line['item_ref']==request.id and line['kind']=='request_returned_proof'
                       for line in global_parts[2]['lines'])
            def card_choices(candidate):
                return {(line['item_ref'],line['kind'],tuple(line['offered']))
                        for stage in ('catch','new') for line in engine._queue({'screen':stage},candidate)}
            assert card_choices(view)==card_choices({**view,'parts':{part:result['lines'] for part,result in global_parts.items()}})
            await engine.backend.fresh(NOW,full_day=False)
            assert len(calls)==1
        anyio.run(run)
    finally:store.close();log.close()
