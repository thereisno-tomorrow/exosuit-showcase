from datetime import date
import pytest
from gremlin_town.review_card.models import screen_key
from gremlin_town.review_card.render import render, walk
from tests.review_card.test_card import setup, NOW
from tests.review_card.test_render import fixture

@pytest.mark.anyio
@pytest.mark.parametrize('action,fields', [('progress', {'value':'A fictional note longer than seven letters', 'remaining':'ten'}), ('dated_outcome', {'kind':'progress', 'day':'bad-date', 'value':'A fictional note'}), ('progress', {'value':'A short note', 'remaining':'12345678'})])
async def test_reopened_submission_keeps_each_field_and_action(tmp_path, action, fields):
    engine, _, _, log, store = setup(tmp_path, initial=False)
    try:
        state = store.session(engine._day()); state['day'] = str(engine._day())
        spec = dict(action=action, ref='fictional-item', modal=True, screen=screen_key(state))
        token = store.issue(state['day'], spec)
        await engine.prepare_modal(token, owner_user_id='123')
        state['submitted_draft'] = dict(fields, submission_id='submitted-1', ref='fictional-item', spec=spec)
        store.save(state['day'], state)
        form = await engine.prepare_modal(token, owner_user_id='123')
        components = {p['component']['custom_id']:p['component'] for p in form['data']['components']}
        for key, value in fields.items():
            field = components[key]
            if field['type'] == 4:
                assert field['value'] == value
                assert len(field['value']) <= field['max_length']
            else:
                assert [o['value'] for o in field['options'] if o.get('default')] == [value]
        other = store.issue(state['day'], dict(spec, action='wait'))
        other_form = await engine.prepare_modal(other, owner_user_id='123')
        assert all('value' not in p['component'] for p in other_form['data']['components'])
    finally:
        store.close(); log.close()

@pytest.mark.anyio
@pytest.mark.parametrize('zone', ['Asia/Singapore', 'Asia/Tokyo', 'America/Los_Angeles'])
async def test_timezone_keeps_choices_and_requires_only_changed_day_recheck(tmp_path, zone):
    engine, _, _, log, store = setup(tmp_path, initial=False)
    try:
        state = store.session(engine._day()); state['day'] = str(engine._day())
        state['picks'] = {'fictional-item':False}
        store.save(state['day'], state)
        token = store.issue(state['day'], dict(action='timezone', modal=True, screen=screen_key(state)))
        await engine.prepare_modal(token, owner_user_id='123')
        await engine.handle(token, 'zone-submit', {'value':zone}, owner_user_id='123')
        saved = store.session(engine._day())
        assert saved['picks'] == {'fictional-item':False}
        assert bool(saved.get('date_changed')) == (str(engine._day()) != str(NOW.date()))
    finally:
        store.close(); log.close()


def test_full_choices_with_errors_and_kept_submission_remain_navigable():
    state, view = fixture()
    state.update(screen='day', error='Fix the fictional date', win='A waiting item arrived', date_changed=True,
                 submitted_draft={'value':'A fictional submitted note', 'ref':'missing', 'spec':{'action':'progress'}})
    view['planning'] = {'issues':['Issue one', 'Issue two', 'Issue three'], 'notices':['Notice']}
    result = render(state, view, lambda label, action, options: action)
    assert len(list(walk(result['components']))) <= 40
    shown = [node for node in walk(result['components']) if node.get('custom_id') == 'toggle']
    assert shown
    assert any(node.get('custom_id') == 'more' for node in walk(result['components']))

@pytest.mark.anyio
async def test_checked_invalid_progress_survives_restart_and_saves_corrected_once(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.review_card.engine import ReviewCardEngine
    from gremlin_town.review_card.store import CardStore
    from gremlin_town.review_card.host import modal_fields
    from tests.review_card.test_card import WORK, controls, content
    from tests.review_card.test_phone_feedback import action
    engine, counts, _, log, store = setup(tmp_path, initial=False)
    try:
        await engine.backend.change(WorkRequest('u1-task', 'Fictional report', 'owner', 'direct',
            ({'move':'new','title':'Fictional report'},)), owner_id=WORK.owner_id)
        result = await action(engine, await engine.open(), 'go_day')
        result = await action(engine, result, 'inspect', {'value':'Fictional report'})
        token = next(t for t in controls(result).values() if store.action(t)['spec']['action'] == 'progress')
        await engine.prepare_modal(token, owner_user_id='123')
        fields = modal_fields({'components':[{'type':18,'component':{'type':4,'custom_id':'value','value':'Finished the first fictional section'}},
            {'type':18,'component':{'type':4,'custom_id':'remaining','value':'ten'}}]})
        before = counts['changes']
        result = await engine.handle(token, 'invalid-progress', fields, owner_user_id='123')
        assert counts['changes'] == before
        assert 'minutes' in content(result).lower()
        current = store.session(engine._day())
        capacity = store.issue(str(engine._day()), dict(action='capacity', modal=True, screen=screen_key(current)))
        await engine.prepare_modal(capacity, owner_user_id='123')
        await engine.handle(capacity, 'invalid-other-form', {'value':'many'}, owner_user_id='123')
        store.close()
        store = CardStore(tmp_path / 'card.sqlite')
        engine = ReviewCardEngine(engine.runtime, engine.settings, store, lambda:NOW)
        token = next(t for t in controls(await engine.current()).values() if store.action(t)['spec']['action'] == 'progress')
        form = await engine.prepare_modal(token, owner_user_id='123')
        values = {p['component']['custom_id']:p['component'].get('value') for p in form['data']['components']}
        assert values == fields
        result = await engine.handle(token, 'corrected-progress', dict(fields,remaining='10'), owner_user_id='123')
        assert counts['changes'] == before + 1
        await engine.handle(token, 'repeat-corrected-progress', dict(fields,remaining='10'), owner_user_id='123')
        assert counts['changes'] == before + 1
    finally:
        store.close(); log.close()


def test_adaptive_choices_pagination_visits_each_candidate_after_notice_changes():
    state, view = fixture()
    state.update(screen='day', error='Fix this fictional input', submitted_draft={'value':'Kept', 'spec':{'action':'add'}})
    view['planning'] = {'issues':['one','two','three']}
    seen = []
    while True:
        issued = {}
        def issue(label, action, options):
            token = str(len(issued))
            issued[token] = (action, options)
            return token
        result = render(state, view, issue)
        tokens = [node['custom_id'] for node in walk(result['components']) if node.get('custom_id')]
        seen.extend(issued[token][1]['ref'] for token in tokens if issued[token][0] == 'toggle')
        more = next((issued[token][1] for token in tokens if issued[token][0] == 'more'), None)
        if more is None:
            break
        state['day_offset'] = more['next_offset']; state['page'] += 1
        state.pop('error', None); state.pop('submitted_draft', None)
    assert seen == [card['ref'] for card in view['candidates']]


def test_oversized_view_recovery_keeps_exact_pending_control():
    state, view = fixture(); state['screen'] = 'weekly'
    view['weekly'][0]['lines'] = ['x'*220]*30
    result = render(state, view, lambda label,action,options:action)
    assert 'go_day' in str(result) and 'start' in str(result)
    state['submitted_draft'] = {'value':'Kept note','spec':{'action':'add'}}
    state['error'] = 'x'*100
    result = render(state, view, lambda label,action,options:action, pending={'token':'exact-pending-save'})
    assert 'exact-pending-save' in str(result)
    assert len(list(walk(result['components']))) <= 40
