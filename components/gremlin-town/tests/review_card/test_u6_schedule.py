import pytest
from gremlin_town.review_card.render import render, walk
from .test_render import fixture

@pytest.mark.parametrize('screen',['open','settled'])
def test_unavailable_schedule_is_not_an_empty_schedule(screen):
    state, view = fixture()
    state['screen'] = screen
    view['day']['timeline'] = []
    view['day']['schedule'] = {'available':False,'label':"Couldn't see the schedule",'fetched_at':None,'age_seconds':None}
    result = render(state,view,lambda *args:'fictional')
    contents = '\n'.join(v.get('content','') for v in walk(result['components']))
    assert 'Nothing scheduled today yet' not in contents
    assert 'unavailable' in contents.lower()


def test_last_known_schedule_keeps_timestamp_without_claiming_freshness():
    state, view = fixture()
    state['day'] = '2026-09-30'
    view['day']['timeline'] = []
    view['day']['schedule'] = {'available':False,'label':"Couldn't see the schedule",'fetched_at':None,'age_seconds':None}
    state['last_schedule'] = {'fetched_at':'2026-09-30T11:00:00+08:00',
        'timeline':[{'type':'task','title':'Fictional earlier placement','start':'2026-09-30T13:00:00+08:00','end':'2026-09-30T13:30:00+08:00'}]}
    result = render(state,view,lambda *args:'fictional')
    contents = '\n'.join(v.get('content','') for v in walk(result['components']))
    assert 'last known' in contents.lower()
    assert 'Fictional earlier placement' in contents
    assert '11:00' in contents or '179' in contents


@pytest.mark.anyio
async def test_schedule_refresh_is_read_only_and_last_known_survives_restart(tmp_path):
    from .test_card import setup, controls, content
    from gremlin_town.review_card.store import CardStore
    import anyio
    engine, counts, fake, log, store = await anyio.to_thread.run_sync(setup, tmp_path)
    original = engine.backend.fresh
    available = [True]
    async def fresh(*args, **kwargs):
        view = await original(*args, **kwargs)
        if view.get('full_day'):
            view['day']['schedule'] = dict(available=available[0], label='Fictional schedule',
                fetched_at='2026-09-30T11:00:00+08:00' if available[0] else None, age_seconds=0 if available[0] else None)
            view['day']['timeline'] = [dict(type='task', title='Fictional earlier placement',
                start='2026-09-30T13:00:00+08:00', end='2026-09-30T13:30:00+08:00')] if available[0] else []
        return view
    engine.backend.fresh = fresh
    try:
        first = await engine.open()
        state = store.session(first.day)
        state['picks'] = {'fictional-focus': True}
        state['interrupted_review'] = {'screen':'anything','stage':'anything','item':None}
        store.save(first.day, state)
        cached = store.session(first.day)['last_schedule']
        before = counts['changes']
        available[0] = False
        second = await engine.handle(controls(first)['Check schedule'], 'fictional-refresh', owner_user_id='123')
        assert 'last known' in content(second).lower()
        assert 'Fictional earlier placement' in content(second)
        assert counts['changes'] == before
        assert store.session(first.day)['picks'] == {'fictional-focus': True}
        assert store.session(first.day)['interrupted_review']['screen'] == 'anything'
        assert store.session(first.day)['last_schedule'] == cached
        store.close()
        engine.store = CardStore(tmp_path/'card.sqlite')
        reopened = await engine.open()
        assert 'Fictional earlier placement' in content(reopened)
        assert engine.store.session(first.day)['last_schedule'] == cached
    finally:
        engine.store.close()
        log.close()


def test_unavailable_previous_day_cache_never_appears_as_today():
    state, view = fixture()
    state['last_schedule'] = dict(day='2026-09-29', fetched_at='2026-09-29T11:00:00+08:00',
        timeline=[dict(type='task', title='Fictional yesterday placement', start='2026-09-29T13:00:00+08:00', end='2026-09-29T13:30:00+08:00')])
    view['day']['schedule']['available'] = False
    output = render(state, view, lambda *args:'fictional')
    contents = '\n'.join(v.get('content','') for v in walk(output['components']))
    assert 'yesterday placement' not in contents
    assert 'unavailable' in contents.lower()



def test_saved_manifest_all_pages_survive_lost_reply_restart_and_read_refresh(tmp_path):
    import anyio
    from .test_card import setup, content, controls, WORK, NOW
    from .test_u5_summary import act, choose_one
    from gremlin_town.mcp.brain_runtime import WorkRequest
    from gremlin_town.review_card.store import CardStore
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        detail = 'Fictional complete detail ' + 'x' * 2100 + ' FINAL AGREED CONDITION'
        await engine.backend.change(WorkRequest('fictional-saved-pages', 'Fictional saved task\n' + detail + '\n30 min', 'owner', 'direct',
            ({'move':'new','title':'Fictional saved task','details':detail},
             {'move':'organise','thing':'#1','rough_minutes':30,'length_is_nics':True})), owner_id=WORK.owner_id)
        result = await act(engine, await choose_one(engine), 'review_choices')
        approved = engine.store.session(NOW.date())['day_approval']
        counts['lose_reply'] = True
        result = await act(engine, result, 'confirm')
        pending = engine.store.pending(NOW.date())
        assert pending and 'saved_choices' not in engine.store.session(NOW.date())
        engine.store.close()
        engine.store = CardStore(tmp_path/'card.sqlite')
        result = await engine.handle(pending['token'], 'fictional-retry-save', owner_user_id='123')
        assert engine.store.session(NOW.date())['saved_choices']['text'] == approved['text']
        seen = content(result)
        while 'Next saved page' in controls(result):
            result = await engine.handle(controls(result)['Next saved page'], 'fictional-read-saved', owner_user_id='123')
            seen += content(result)
        assert 'FINAL AGREED CONDITION' in seen
        assert 'Schedule unavailable' in content(result)
        before = counts['changes']
        result = await engine.handle(controls(result)['Check schedule'], 'fictional-read-only', owner_user_id='123')
        assert counts['changes'] == before
        assert engine.store.session(NOW.date())['saved_choices']['text'] == approved['text']
        assert len(log.signals_on('day_settled', NOW.date())) == 1
    try:
        anyio.run(run)
    finally:
        engine.store.close()
        log.close()
