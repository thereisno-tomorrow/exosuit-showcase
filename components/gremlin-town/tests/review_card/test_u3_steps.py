from datetime import date
from gremlin_town.review_card.render import render, walk, modal
from gremlin_town.review_card.actions import make_request
from .test_render import fixture


def test_all_twenty_draft_steps_are_readable_in_full_bounded_pages():
    state, view = fixture()
    state['screen'] = 'catch'
    draft = dict(state='draft', operation_id='fixture-op', draft_sha256='a'*64,
                 steps=[dict(title=f'Step {i} '+ 't'*230 + f' TITLE-END-{i}',
                             detail='d'*970 + f' DETAIL-END-{i}') for i in range(20)])
    seen = ''
    for page in range(100):
        state['draft_page'] = page
        result = render(state, view, lambda *a, **k: 'fixture', draft=draft)
        nodes = list(walk(result['components']))
        assert len(nodes) <= 40
        assert sum(len(n.get('content','')) for n in nodes) <= 4000
        seen += '\n'.join(n.get('content','') for n in nodes)
        if not any(n.get('label') == 'Next draft page' for n in nodes):
            break
    for i in range(20):
        assert f'TITLE-END-{i}' in seen
        assert f'DETAIL-END-{i}' in seen


def test_step_entry_preserves_commas_and_requires_explicit_order():
    spec = dict(action='parts', ref='fixture-parent', expected={'id':'fixture-version'}, label='Add steps')
    request = make_request('fixture-operation', spec,
        {'value':'Compare red, green and blue\nChoose the winner', 'order':['ordered']}, date(2026,10,5))
    assert request.moves[0]['parts'] == [dict(title='Compare red, green and blue'), dict(title='Choose the winner', then=True)]
    independent = make_request('fixture-operation', spec,
        {'value':'Compare red, green and blue\nChoose the winner', 'order':'independent'}, date(2026,10,5))
    assert not any(p.get('then') for p in independent.moves[0]['parts'])
    form = modal('fixture', spec, date(2026,10,5))
    assert any(n.get('custom_id') == 'order' and n['type'] == 3 for n in walk(form['data']['components']))


def test_existing_steps_show_full_order_and_dropped_prerequisite():
    from types import SimpleNamespace
    from gremlin_town.review_card.render import step_context
    from brain_core import work
    state, view = fixture()
    parent = SimpleNamespace(id='parent', title='Parent', state='open', data={}, body='')
    first = SimpleNamespace(id='first', title='Choose red, green or blue', state='dropped',
                            data={'parent':'parent','order':1}, body='Full original detail')
    second = SimpleNamespace(id='second', title='Apply selected colour', state='open',
                             data={'parent':'parent','order':2,'after':['first']}, body='Hidden ending matters')
    view['index'] = {'parent':parent,'first':first,'second':second}
    state.update(screen='step_context', steps_ref='parent')
    words = '\n'.join(n.get('content','') for n in walk(render(state,view,lambda *a:'fixture')['components']))
    assert words.index('Choose red, green or blue') < words.index('Apply selected colour')
    assert 'Hidden ending matters' in words
    assert 'Requires: Choose red, green or blue (dropped)' in words
    assert 'Only completion satisfies a prerequisite' in words


def test_step_form_requires_order_before_any_write():
    import pytest
    from gremlin_town.review_card.models import CardError
    with pytest.raises(CardError,match='Choose ordered'):
        make_request('fixture',dict(action='parts',ref='parent',expected={},label='Add steps'),
                     {'value':'First step\nSecond step'}, date(2026,10,5))


def test_full_preview_preserves_mentions_markdown_and_multiline_details():
    from gremlin_town.review_card.render import full_text
    raw = 'Full @everyone detail\nA `literal` option\\path'
    assert full_text(raw) == 'Full @\u200beveryone detail\nA \\`literal\\` option\\\\path'

from tests.dashboard.test_ask import hub as draft_hub


def test_actual_draft_page_navigation_binds_refinement_and_stale_save(tmp_path, draft_hub):
    import anyio, json
    from dataclasses import replace
    from gremlin_town.contracts import Actor, MessageKind, Packet, ReplyRequest
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.review_card.diablo import ReviewDiablo
    from .test_card import setup, tap, controls, content, NOW, WORK
    engine, counters, fake, log, store = setup(tmp_path, initial=False)
    engine.settings = replace(engine.settings, ask_diablo=True)
    engine.diablo = ReviewDiablo(draft_hub.service, owner_id=WORK.owner_id,
        sender_participant='fixture-sender', recipient_participant='fixture-recipient', project='fixture-project',
        authorize_item=engine.authorize_item, saver=engine.backend.change, enabled=True)
    async def run():
        await engine.backend.change(WorkRequest('fixture-page-parent','Plan fixture build 180 min','owner','direct',(
            {'move':'new','title':'Plan fixture build'},
            {'move':'organise','thing':'#1','rough_minutes':180,'length_is_nics':True})),owner_id=WORK.owner_id)
        result = await tap(engine,await engine.open(),'Start review')
        result = await tap(engine,result,'Ask Diablo')
        result = await tap(engine,result,'Research and break it down')
        binding = engine.current_diablo_binding()
        draft_hub.worker.dispatch_once()
        operation = draft_hub.store.operation_by_client('fixture-sender',engine.diablo.prefix+binding['operation_id'])
        requested = draft_hub.store.request(operation.request_id)
        steps = [{'title':f'Perform fictional action {i}', 'detail':'d'*370+f' END-{i}'} for i in range(20)]
        packet = Packet(1,MessageKind.RESULT,None,'Fixture draft',json.dumps({'steps':steps}),{},(),'','','private')
        draft_hub.service.requests_reply(Actor('fixture-recipient'),ReplyRequest('fixture-pages-reply',requested.request_id,requested.revision,MessageKind.RESULT,packet))
        draft_hub.worker.dispatch_once()
        result = await engine.current()
        old_save = controls(result)['Save these steps']
        seen = content(result)
        for i in range(19):
            result = await tap(engine,result,'Next draft page')
            seen += content(result)
        assert all(f'END-{i}' in seen for i in range(20))
        assert counters['changes'] == 1
        result = await tap(engine,result,'Previous draft page')
        assert 'END-18' in content(result)
        expected = store.action(old_save)['spec']['expected']
        await engine.diablo.refine({**{k:binding[k] for k in ('day','line_ref','item_ref')},
            'operation_id':'fixture-new-refinement','previous_operation_id':binding['operation_id'],
            'expected':expected,'text':'Change the final action.'},owner_id=WORK.owner_id)
        result = await engine.handle(old_save,'fixture-obsolete-save',owner_user_id='123')
        assert counters['changes'] == 1
        assert 'newer' in content(result).lower() or 'changed' in content(result).lower()
    try:
        anyio.run(run)
    finally:
        store.close(); log.close()


def test_append_ordered_steps_keeps_existing_work_and_comma_title(tmp_path):
    import anyio
    from brain_core import work
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from .test_card import setup, tap, content, NOW, WORK, records
    engine, counters, fake, log, store = setup(tmp_path,initial=False)
    async def run():
        await engine.backend.change(WorkRequest('fixture-existing-steps',
            'Build fictional sample First existing step Second existing step','owner','direct',(
                {'move':'new','title':'Build fictional sample'},
                {'move':'parts','thing':'#1','parts':[{'title':'First existing step'},{'title':'Second existing step','then':True}]})),owner_id=WORK.owner_id)
        original = work.index_things(records(fake))
        parent = next(ref for ref,r in original.items() if r.title=='Build fictional sample')
        result = await tap(engine,await engine.open(),"Today's choices")
        result = await tap(engine,result,'Update an item…',{'value':'Build fictional sample'})
        result = await tap(engine,result,'View existing steps and blockers')
        assert 'First existing step' in content(result) and 'Second existing step' in content(result)
        assert 'Requires: First existing step (open)' in content(result)
        result = await tap(engine,result,'Add steps…',{'value':'Compare red, green and blue\nChoose final colour','order':['ordered']})
        updated = work.index_things(records(fake))
        assert all(updated[ref].data == old.data for ref,old in original.items())
        children = sorted((r for r in updated.values() if r.data.get('parent')==parent),key=lambda r:r.data['order'])
        assert [r.title for r in children] == ['First existing step','Second existing step','Compare red, green and blue','Choose final colour']
        assert children[3].data['after'] == [work.first_id(children[2])]
        assert counters['changes'] == 2
    try:
        anyio.run(run)
    finally:
        store.close(); log.close()
