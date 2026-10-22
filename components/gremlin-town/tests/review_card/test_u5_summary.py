"""U5: complete, explicit approval of fictional day choices."""
import anyio

from brain_core import work
from gremlin_town.review_card.render import walk
from .test_card import NOW, setup, records, content


def token_for(engine, result, action, value=None):
    matches = []
    for part in walk(result.message['components']):
        if part.get('custom_id') and part.get('type') == 2:
            spec = engine.store.action(part['custom_id'])['spec']
            if spec['action'] == action and (value is None or spec.get('value') == value):
                matches.append(part['custom_id'])
    assert len(matches) == 1, (action, matches, content(result))
    return matches[0]


async def last_summary_page(engine, result):
    while True:
        candidates = [part for part in walk(result.message['components']) if part.get('type') == 2 and part.get('custom_id')]
        next_token = next((part['custom_id'] for part in candidates if part.get('label') == 'Next summary page'), None)
        if not next_token:
            return result
        result = await engine.handle(next_token, 'fictional-summary-reading', owner_user_id='123')


async def act(engine, result, action, fields=None, value=None):
    if action == 'confirm':
        result = await last_summary_page(engine, result)
    return await engine.handle(token_for(engine, result, action, value), 'fictional-u5', fields,
                               owner_user_id='123')


async def choose_one(engine):
    result = await act(engine, await engine.open(), 'go_day')
    return await act(engine, result, 'toggle')


def test_last_estimate_returns_to_summary_without_saving_the_day(tmp_path):
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        result = await choose_one(engine)
        # U4's choosing screen is followed by a dedicated complete summary.
        actions = {store.action(p['custom_id'])['spec']['action']
                   for p in walk(result.message['components']) if p.get('custom_id')}
        result = await act(engine, result, 'review_choices' if 'review_choices' in actions else 'confirm')
        # Review choices can offer estimates before or on the final summary.
        if store.session(NOW.date()).get('screen') != 'lengths':
            result = await act(engine, result, 'confirm')
        result = await act(engine, result, 'length', value=60)
        assert not log.signals_on('day_settled', NOW.date()), content(result)
        record = next(iter(work.index_things(records(fake)).values()))
        assert record.data['timing']['duration_minutes'] == 60
        assert store.session(NOW.date())['screen'] == 'day_summary'
        assert token_for(engine, await last_summary_page(engine, result), 'confirm')
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_choosing_requires_complete_summary_before_save(tmp_path):
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        result = await choose_one(engine)
        assert token_for(engine, result, 'review_choices')
        actions = {store.action(p['custom_id'])['spec']['action']
                   for p in walk(result.message['components']) if p.get('custom_id')}
        assert 'confirm' not in actions
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_unknown_estimate_offers_custom_minutes_and_not_sure(tmp_path):
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        result = await choose_one(engine)
        actions = {store.action(p['custom_id'])['spec']['action']
                   for p in walk(result.message['components']) if p.get('custom_id')}
        result = await act(engine, result, 'review_choices' if 'review_choices' in actions else 'confirm')
        if store.session(NOW.date()).get('screen') != 'lengths':
            result = await act(engine, result, 'confirm')
        actions = {store.action(p['custom_id'])['spec']['action']
                   for p in walk(result.message['components']) if p.get('custom_id')}
        assert 'length_custom' in actions, content(result)
        assert 'length_unknown' in actions, content(result)
        before = counters['changes']
        result = await act(engine, result, 'length_unknown')
        assert counters['changes'] == before
        record = next(iter(work.index_things(records(fake)).values()))
        assert not record.data.get('rough_minutes')
        assert not record.data.get('timing', {}).get('duration_minutes')
        assert store.session(NOW.date())['screen'] == 'day_summary'
        assert 'estimate unknown' in content(result).lower()
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_session_only_commitment_is_counted_without_whole_task_focus():
    from types import SimpleNamespace
    from gremlin_town.review_card.planning import planning_summary
    record = SimpleNamespace(id='session-task', title='Find the first source', state='open',
        data={'planning': {'work_sessions': [{'day': NOW.date().isoformat(), 'minutes': 20, 'kind': 'timebox'}]}})
    result = planning_summary({'session-task': record}, capacity_minutes=15, now=NOW)
    assert result['required_minutes'] == 20
    assert any('20' in issue and '15' in issue for issue in result['issues'])


def test_custom_estimate_is_whole_task_and_requires_explicit_day_save(tmp_path):
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        result = await act(engine, await choose_one(engine), 'review_choices')
        result = await act(engine, result, 'confirm')
        token = token_for(engine, result, 'length_custom')
        form = await engine.prepare_modal(token, owner_user_id='123')
        assert form['data']['components'][0]['component']['custom_id'] == 'minutes'
        result = await engine.handle(token, 'fictional-custom', {'minutes': 'ten'}, owner_user_id='123')
        assert 'positive whole number' in content(result)
        result = await act(engine, result, 'length_custom', {'minutes': '37'})
        assert not log.signals_on('day_settled', NOW.date())
        assert '37 min' in store.session(NOW.date())['day_approval']['text']
        result = await act(engine, result, 'confirm')
        assert len(log.signals_on('day_settled', NOW.date())) == 1
        assert 'Choices saved' in content(result)
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_not_sure_can_save_flexible_focus_without_inventing_estimate(tmp_path):
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        result = await act(engine, await choose_one(engine), 'review_choices')
        result = await act(engine, result, 'confirm')
        result = await act(engine, result, 'length_unknown')
        assert 'estimate unknown' in content(result).lower()
        result = await act(engine, result, 'confirm')
        record = next(iter(work.index_things(records(fake)).values()))
        assert record.data['dates']['planned']['day'] == NOW.date().isoformat()
        assert not record.data.get('timing', {}).get('duration_minutes')
        assert len(log.signals_on('day_settled', NOW.date())) == 1
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_changed_fact_between_summary_and_save_requires_new_approval(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from test_work_door import WORK
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        result = await act(engine, await choose_one(engine), 'review_choices')
        result = await last_summary_page(engine, result)
        token = token_for(engine, result, 'confirm')
        ref = next(iter(work.index_things(records(fake))))
        await engine.backend.change(WorkRequest('fictional-renamed-source', 'Rename to Different commitment', 'owner', 'direct',
            ({'move': 'organise', 'thing': ref, 'title': 'Different commitment'},)), owner_id=WORK.owner_id)
        before = counters['changes']
        result = await engine.handle(token, 'fictional-stale-save', owner_user_id='123')
        assert counters['changes'] == before
        assert 'changed' in content(result).lower()
        assert 'Different commitment' in store.session(NOW.date())['day_approval']['text']
        assert not log.signals_on('day_settled', NOW.date())
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_complete_long_summary_pages_preserve_every_character(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from test_work_door import WORK
    engine, counters, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        title = 'Fictional long task ' + 'x' * 230
        body = 'Complete detail ' + 'y' * 2000 + ' IMPORTANT FINAL CONDITION'
        await engine.backend.change(WorkRequest('fictional-long-summary', title + '\n' + body, 'owner', 'direct',
            ({'move': 'new', 'title': title, 'details': body},)), owner_id=WORK.owner_id)
        result = await act(engine, await choose_one(engine), 'review_choices')
        seen = content(result)
        while True:
            actions = [store.action(p['custom_id'])['spec'] for p in walk(result.message['components']) if p.get('custom_id')]
            next_page = next((a for a in actions if a['action'] == 'summary_page' and a['value'] > store.session(NOW.date())['page']), None)
            if not next_page:
                break
            result = await act(engine, result, 'summary_page', value=next_page['value'])
            seen += content(result)
            assert len(list(walk(result.message['components']))) <= 40
            assert sum(len(p.get('content', '')) for p in walk(result.message['components'])) <= 4000
        assert 'IMPORTANT FINAL CONDITION' in seen
        assert title in store.session(NOW.date())['day_approval']['text']
        assert body in store.session(NOW.date())['day_approval']['text']
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_lost_day_save_reply_reopens_exact_intent_after_store_restart(tmp_path):
    from gremlin_town.review_card.engine import ReviewCardEngine
    from gremlin_town.review_card.store import CardStore
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        nonlocal store
        result = await act(engine, await choose_one(engine), 'review_choices')
        result = await act(engine, result, 'confirm')
        result = await act(engine, result, 'length', value=30)
        counters['lose_reply'] = True
        result = await act(engine, result, 'confirm')
        pending = store.pending(NOW.date())
        assert pending
        operation = pending['intent']['operation']
        branch = fake.branch
        store.close()
        store = CardStore(tmp_path / 'card.sqlite')
        restarted = ReviewCardEngine(engine.runtime, engine.settings, store, lambda: NOW)
        result = await restarted.current()
        assert store.pending(NOW.date())['intent']['operation'] == operation
        result = await restarted.handle(pending['token'], 'fictional-exact-retry', owner_user_id='123')
        assert fake.branch == branch
        assert store.pending(NOW.date()) is None
        assert len(log.signals_on('day_settled', NOW.date())) == 1
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_remove_focus_does_not_unplan_and_named_date_removal_is_checked(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from test_work_door import WORK
    from .test_card import tap
    engine, counters, fake, log, store = setup(tmp_path)
    async def run():
        ref = next(iter(work.index_things(records(fake))))
        result = await act(engine, await choose_one(engine), 'review_choices')
        result = await act(engine, result, 'confirm')
        result = await act(engine, result, 'length', value=30)
        result = await act(engine, result, 'confirm')
        result = await act(engine, result, 'go_day')
        result = await act(engine, result, 'toggle')
        result = await act(engine, result, 'review_choices')
        assert 'remains planned without focus' in store.session(NOW.date())['day_approval']['text']
        assert work.index_things(records(fake))[ref].data['dates']['planned']['day'] == NOW.date().isoformat()
        result = await tap(engine, result, 'Remove a planned date…', {'item': ref, 'note': ''})
        assert 'planned' not in work.index_things(records(fake))[ref].data.get('dates', {})
        assert work.index_things(records(fake))[ref].state == 'open'
        assert store.session(NOW.date())['screen'] == 'day_summary'
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_summary_hold_membership_uses_local_day_not_utc_date():
    from types import SimpleNamespace
    from gremlin_town.review_card.planning import day_approval
    record = SimpleNamespace(id='held-task', title='Hold evening free', state='open', body='',
        data={'planning': {'held_time': [{'start': '2026-10-05T01:00:00+00:00', 'end': '2026-10-05T02:00:00+00:00'}]}})
    view = {'index': {'held-task': record}, 'candidates': [], 'planning': {'required_minutes': 60},
            'details': {'held-task': {'record_id': 'held-version', 'record_sha256': 'a' * 64}}}
    summary = day_approval(view, {'day': '2026-10-04', 'timezone': 'America/Los_Angeles'})
    assert 'Hold evening free' in summary['text']
    assert 'Held time:' in summary['text']


def test_rejected_estimate_remains_recoverable_without_orphan_reminder_after_not_sure(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path)
    async def run():
        try:
            result=await choose_one(engine)
            result=await act(engine,result,'review_choices')
            result=await act(engine,result,'confirm')
            token=token_for(engine,result,'length_custom')
            await engine.prepare_modal(token,owner_user_id='123')
            result=await engine.handle(token,'fictional-zero',{'minutes':'0'},owner_user_id='123')
            assert 'Reopen the form' in content(result)
            result=await act(engine,result,'length_unknown')
            assert 'Reopen the form' not in content(result)
            result=await act(engine,result,'confirm')
            assert 'Choices saved' in content(result)
            assert 'Reopen the form' not in content(result)
            assert store.session(engine._day()).get('submitted_draft')
        finally:
            store.close();log.close()
    anyio.run(run)
