from datetime import date
from gremlin_town.review_card.render import modal, render, walk
from gremlin_town.review_card.store import CardStore
from .test_render import fixture


def buttons(message):
    return [n['label'] for n in walk(message['components']) if n['type'] == 2]


def test_regular_date_rollover_keeps_focus_for_recheck(tmp_path):
    store = CardStore(tmp_path / 'card.sqlite')
    try:
        state = store.session('2026-09-30')
        state.update(screen='day', picks={'fictional-work': True})
        store.save('2026-09-30', state)
        next_day = store.session('2026-10-01')
        assert next_day['picks'] == {'fictional-work': True}
        assert next_day['date_changed']
    finally:
        store.close()


def test_operation_controls_require_an_operation_and_portions_require_parts():
    state, view = fixture()
    state.update(screen='planning_menu', page=1)
    labels = buttons(render(state, view, lambda *args: 'fictional'))
    assert 'Record when it started…' not in labels
    assert 'Cancel before starting' not in labels
    assert 'Update a part or batch…' not in labels
    assert 'Choose ready parts or batches…' not in labels


def test_outcome_has_one_changed_plan_control():
    state, view = fixture()
    state.update(screen='yesterday', stage='yesterday')
    view['line']['kind'] = 'outcome'
    labels = buttons(render(state, view, lambda *args: 'fictional'))
    assert labels.count('Something changed…') == 1


def test_forms_use_typed_choices_and_current_local_dates():
    for action, field in [('planning_portion', 'ready'), ('portion_result', 'correct'), ('appointment', 'mode'), ('planning_shared', 'kind')]:
        data = modal('fictional', {'action': action}, date(2027, 3, 11))['data']
        node = next(v['component'] for v in data['components'] if v['component']['custom_id'] == field)
        assert node['type'] == 3, action
        assert node['max_values'] == 1
        assert '2026-10-05' not in str(data)


def test_waiting_list_shows_requested_person_and_check_date():
    state, view = fixture()
    state.update(screen='waiting_updates')
    view['waiting_items'] = [{**view['item'], 'needs': 'signed fictional permit', 'whose': 'Fictional Pat', 'expected': '2026-10-03'}]
    result = render(state, view, lambda *args: 'fictional')
    assert 'Fictional Pat' in str(result)
    assert 'signed fictional permit' in str(result)
    assert '2026-10-03' in str(result)


def test_review_queue_progress_keeps_original_denominator(tmp_path):
    from .test_card import setup
    engine, counters, fake, log, store = setup(tmp_path, initial=False)
    try:
        state = store.session('2026-09-30')
        state.update(stage='yesterday', screen='yesterday', item='first')
        first = {'reference': 'first', 'item_ref': 'one', 'answered': False}
        second = {'reference': 'second', 'item_ref': 'two', 'answered': False}
        view = {'stages': {'yesterday': [first, second]}, 'details': {'one': {}, 'two': {}}}
        engine._queue(state, view)
        view['stages']['yesterday'] = [second]
        state['item'] = 'second'
        engine._queue(state, view)
        assert state['queue_snapshots']['yesterday'] == ['first', 'second']
    finally:
        store.close()
        log.close()


def test_interrupted_review_resumes_same_item_after_store_restart(tmp_path):
    import anyio
    from datetime import timedelta
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.review_card.engine import ReviewCardEngine
    from .test_card import setup, tap, content, controls, NOW, WORK
    engine, counters, fake, log, store = setup(tmp_path, initial=False)
    async def journey():
        yesterday = (NOW.date() - timedelta(days=1)).isoformat()
        moves = []
        for number, title in enumerate(('First fictional task', 'Second fictional task'), 1):
            moves.extend(({'move': 'new', 'title': title}, {'move': 'date', 'thing': '#' + str(number), 'when': yesterday, 'kind': 'planned'}))
        await engine.backend.change(WorkRequest('u4-resume-fixture', yesterday, 'owner', 'direct', tuple(moves)), owner_id=WORK.owner_id)
        result = await tap(engine, await engine.open(), 'Start review')
        result = await tap(engine, result, 'Made progress', {'value': 'Prepared fictional materials'})
        assert '2 of 2' in content(result)
        interrupted = store.session(NOW.date())['review_ref']
        result = await tap(engine, result, "Today's choices")
        store.close()
        restarted_store = CardStore(tmp_path / 'card.sqlite')
        try:
            restart = ReviewCardEngine(engine.runtime, engine.settings, restarted_store, engine.clock)
            result = await restart.current()
            assert 'Continue review' in controls(result)
            result = await tap(restart, result, 'Continue review')
            assert restarted_store.session(NOW.date())['review_ref'] == interrupted
            assert '2 of 2' in content(result)
        finally:
            restarted_store.close()
    try:
        anyio.run(journey)
    finally:
        try:
            store.close()
        except Exception:
            pass
        log.close()


def menu(result):
    return next(node for node in walk(result.message['components']) if node['type'] == 3)


def test_context_picker_pages_and_stale_page_refusal(tmp_path):
    import anyio
    import pytest
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.review_card.models import CardError
    from .test_card import setup, tap, controls, content, WORK
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    async def journey():
        titles = [f'Fictional choice {i:02d}' for i in range(26)]
        for offset in (0, 20):
            batch = titles[offset:offset + 20]
            reply = await engine.backend.change(WorkRequest('u4-26-choices-' + str(offset), '\n'.join(batch), 'owner', 'direct', tuple({'move':'new','title':title} for title in batch)), owner_id=WORK.owner_id)
            assert reply['status'] == 'accepted'
        result = await tap(engine, await engine.open(), "Today's choices")
        result = await tap(engine, result, 'Update an item…')
        first = menu(result)
        assert len(first['options']) == 25
        assert all('No project' in option['description'] and 'open' in option['description'] for option in first['options'])
        assert all(not option['value'].startswith('brn_') for option in first['options'])
        result = await tap(engine, result, 'More choices')
        assert len(menu(result)['options']) == 1
        with pytest.raises(CardError, match='moved on'):
            await engine.handle(first['custom_id'], 'old-selector-page', {'values':['0']}, owner_user_id='123')
        latest = menu(result)
        result = await engine.handle(latest['custom_id'], 'choose-last', {'values':[latest['options'][0]['value']]}, owner_user_id='123')
        assert 'Fictional choice 25' in content(result)
        assert store.session(result.day)['screen'] == 'inspect'
    try:
        anyio.run(journey)
    finally:
        store.close(); log.close()


def test_shared_picker_preserves_other_pages_and_can_clear_each_page(tmp_path):
    import anyio
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from .test_card import setup, tap, controls, content, WORK
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    async def select(result, values):
        return await engine.handle(menu(result)['custom_id'], 'multi-page-select', {'values': values}, owner_user_id='123')
    async def journey():
        titles = [f'Fictional shared {i:02d}' for i in range(26)]
        for offset in (0, 20):
            batch = titles[offset:offset + 20]
            reply = await engine.backend.change(WorkRequest('u4-shared-choices-' + str(offset), '\n'.join(batch), 'owner', 'direct', tuple({'move':'new','title':title} for title in batch)), owner_id=WORK.owner_id)
            assert reply['status'] == 'accepted'
        result = await tap(engine, await engine.open(), "Today's choices")
        result = await tap(engine, result, 'Update an item…', {'value':titles[0]})
        result = await tap(engine, result, 'More planning details…')
        result = await tap(engine, result, 'More options…')
        result = await tap(engine, result, 'Share a trip or setup…')
        result = await select(result, ['0','1'])
        assert '2 selected' in content(result)
        result = await tap(engine, result, 'More choices')
        result = await select(result, ['0'])
        assert '3 selected' in content(result)
        result = await tap(engine, result, 'Previous choices')
        result = await select(result, [])
        assert '1 selected' in content(result)
        result = await tap(engine, result, 'More choices')
        result = await select(result, [])
        result = await tap(engine, result, 'Use these choices')
        assert 'Choose at least one item' in content(result)
        assert store.session(result.day)['screen'] == 'selector'
        result = await select(result, ['0'])
        result = await tap(engine, result, 'Use these choices')
        assert store.session(result.day)['screen'] == 'form_ready'
        token = controls(result)['Continue with these choices…']
        form = await engine.prepare_modal(token, owner_user_id='123')
        assert {part['component']['custom_id'] for part in form['data']['components']} == {'id','kind','minutes'}
        result = await engine.handle(token, 'save-shared', {'id':'Fictional trip', 'kind':'travel','minutes':'20'}, owner_user_id='123')
        assert store.action(token)['status'] == 'done', content(result)
    try:
        anyio.run(journey)
    finally:
        store.close(); log.close()


def test_selected_context_cannot_be_replaced_by_browser_fields(tmp_path):
    import anyio
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from .test_card import setup, tap, controls, content, WORK, records
    from brain_core import work
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    async def journey():
        reply = await engine.backend.change(WorkRequest('u4-bound-context', 'Fictional session A Fictional session B', 'owner', 'direct', ({'move':'new','title':'Fictional session A'}, {'move':'new','title':'Fictional session B'})), owner_id=WORK.owner_id)
        assert reply['status'] == 'accepted'
        result = await tap(engine, await engine.open(), "Today's choices")
        result = await tap(engine, result, 'Set a work session…')
        result = await engine.handle(menu(result)['custom_id'], 'session-target', {'values':['0']}, owner_user_id='123')
        token = controls(result)['Continue with these choices…']
        await engine.prepare_modal(token, owner_user_id='123')
        index = work.index_things(records(fake))
        other = next(ref for ref,record in index.items() if record.title == 'Fictional session B')
        before = counts['changes']
        result = await engine.handle(token, 'forged-item', {'item':other,'minutes':'5','value':'Prepare a sample'}, owner_user_id='123')
        assert 'changed context cannot be supplied' in content(result)
        assert counts['changes'] == before
        chosen = next(ref for ref,record in index.items() if record.title == 'Fictional session A')
        changed = await engine.backend.change(WorkRequest('u4-external-edit', 'Fictional session A changed', 'owner', 'direct', ({'move':'organise','thing':chosen,'title':'Fictional session A changed'},)), owner_id=WORK.owner_id)
        assert changed['status'] == 'accepted'
        before = counts['changes']
        latest = controls(result)['Continue with these choices…']
        await engine.prepare_modal(latest, owner_user_id='123')
        result = await engine.handle(latest, 'changed-selected-proof', {'minutes':'5','value':'Prepare a sample'}, owner_user_id='123')
        assert 'A chosen item changed' in content(result)
        assert counts['changes'] == before
    try:
        anyio.run(journey)
    finally:
        store.close(); log.close()


def test_lost_reply_from_bound_form_keeps_exact_retry_after_restart(tmp_path):
    import anyio
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.review_card.engine import ReviewCardEngine
    from .test_card import setup, tap, controls, content, WORK
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    async def journey():
        reply = await engine.backend.change(WorkRequest('u4-uncertain-fixture', 'Fictional session target', 'owner', 'direct', ({'move':'new','title':'Fictional session target'},)), owner_id=WORK.owner_id)
        assert reply['status'] == 'accepted'
        result = await tap(engine, await engine.open(), "Today's choices")
        result = await tap(engine, result, 'Set a work session…')
        result = await engine.handle(menu(result)['custom_id'], 'choose-session', {'values':['0']}, owner_user_id='123')
        token = controls(result)['Continue with these choices…']
        await engine.prepare_modal(token, owner_user_id='123')
        counts['lose_reply'] = True
        result = await engine.handle(token, 'uncertain-session', {'minutes':'5','value':'Prepare fictional sample'}, owner_user_id='123')
        assert controls(result)['Check save again'] == token
        assert 'Continue with these choices…' not in controls(result)
        store.close()
        restarted_store = CardStore(tmp_path / 'card.sqlite')
        try:
            restart = ReviewCardEngine(engine.runtime, engine.settings, restarted_store, engine.clock)
            result = await restart.current()
            assert controls(result)['Check save again'] == token
            branch = fake.branch
            result = await restart.handle(token, 'check-exact-session', owner_user_id='123')
            assert restarted_store.action(token)['status'] == 'done', content(result)
            assert fake.branch == branch
        finally:
            restarted_store.close()
    try:
        anyio.run(journey)
    finally:
        store.close(); log.close()


def test_ambiguous_duplicate_uses_bound_choices_and_explicit_attach(tmp_path):
    import anyio
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from .test_card import setup, tap, controls, content, WORK, records
    from brain_core import work
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    async def journey():
        reply = await engine.backend.change(WorkRequest('u4-duplicate-fixture', 'File fictional report', 'owner', 'direct', ({'move':'new','title':'File fictional report'}, {'move':'new','title':'File fictional report'})), owner_id=WORK.owner_id)
        assert reply['status'] == 'accepted'
        result = await tap(engine, await engine.open(), 'Start review')
        while 'Skip for now' in controls(result):
            result = await tap(engine, result, 'Skip for now')
        result = await tap(engine, result, 'Add things', {'value':'File fictional report'})
        assert store.session(result.day)['screen'] == 'duplicate'
        assert 'exact item ID' not in content(result)
        assert all(ref not in content(result) for ref in work.index_things(records(fake)))
        before = counts['changes']
        result = await tap(engine, result, 'Choose existing…')
        options = menu(result)['options']
        assert len(options) == 2 and len({option['label'] for option in options}) == 2
        result = await engine.handle(menu(result)['custom_id'], 'choose-duplicate', {'values':[options[1]['value']]}, owner_user_id='123')
        assert store.session(result.day)['screen'] == 'duplicate'
        assert counts['changes'] == before
        assert 'Update existing' in controls(result)
        result = await tap(engine, result, 'Update existing')
        assert counts['changes'] == before + 1
        assert len(work.index_things(records(fake))) == 2
    try:
        anyio.run(journey)
    finally:
        store.close(); log.close()
