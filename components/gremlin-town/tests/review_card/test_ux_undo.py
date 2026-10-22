import anyio
from brain_core import work
from .test_card import setup, tap, content, controls, records
from gremlin_town.review_card.store import CardStore


def test_recent_drop_has_checked_restore_and_survives_restart(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await tap(engine, await engine.open(), 'Start review')
        result = await tap(engine, result, 'Add things', {'value': 'File fictional pump manual'})
        ref = next(r for r in work.index_things(records(fake)) if work.index_things(records(fake))[r].title == 'File fictional pump manual')
        result = await tap(engine, result, 'Drop this task')
        assert work.index_things(records(fake))[ref].state == 'dropped'
        engine.store.close()
        engine.store = CardStore(tmp_path / "card.sqlite")
        result = await engine.current()
        assert 'Restore recent drop' in controls(result)
        before = state['changes']
        result = await tap(engine, result, 'Restore recent drop')
        assert work.index_things(records(fake))[ref].state == 'open'
        assert state['changes'] == before + 1
        assert 'Restore recent drop' not in controls(result)
    try:
        anyio.run(run)
    finally:
        engine.store.close()
        log.close()

async def add_and_drop(engine):
    result = await tap(engine, await engine.open(), 'Start review')
    result = await tap(engine, result, 'Add things', {'value': 'Fictional filing'})
    return await tap(engine, result, 'Drop this task')


def test_restore_refuses_after_intervening_canonical_change(tmp_path):
    from gremlin_town.mcp.brain_runtime import WorkRequest
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await add_and_drop(engine)
        ref = next(iter(work.index_things(records(fake))))
        await engine.backend.change(WorkRequest('changed-after-drop', 'Revised fictional filing', 'owner', 'direct',
            ({'move':'organise', 'thing':ref, 'title':'Revised fictional filing'},)), owner_id=engine.settings.owner_id)
        branch = fake.branch
        result = await tap(engine, await engine.current(), 'Restore recent drop')
        assert fake.branch == branch
        assert work.index_things(records(fake))[ref].state == 'dropped'
        assert 'has changed since' in content(result)
        assert 'Recorded' + ' \u00b7 Restore' not in content(result)
        assert store.session(engine._day())['last_drop']['ref'] == ref
    try:
        anyio.run(run)
    finally:
        store.close(); log.close()


def test_restore_lost_reply_retries_exact_intent_once(tmp_path):
    engine, state, fake, log, store = setup(tmp_path, initial=False)
    async def run():
        result = await add_and_drop(engine)
        state['lose_reply'] = True
        result = await tap(engine, result, 'Restore recent drop')
        assert 'Check save again' in controls(result)
        ref = next(iter(work.index_things(records(fake))))
        assert work.index_things(records(fake))[ref].state == 'open'
        branch = fake.branch
        engine.store.close()
        engine.store = CardStore(tmp_path / 'card.sqlite')
        result = await tap(engine, await engine.current(), 'Check save again')
        assert fake.branch == branch
        assert 'Restore recent drop' not in controls(result)
    try:
        anyio.run(run)
    finally:
        engine.store.close(); log.close()
