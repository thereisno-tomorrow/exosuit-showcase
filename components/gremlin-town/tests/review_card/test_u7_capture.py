import pytest
from brain_core import work
from .test_card import setup, tap, records, controls, content

@pytest.mark.anyio
async def test_capture_preserves_commas_inside_one_item(tmp_path):
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    try:
        result = await tap(engine,await engine.open(),'Start review')
        result = await tap(engine,result,'Add things',{'value':'Buy fictional coffee, tea and cups','order':'independent'})
        assert [r.title for r in work.index_things(records(fake)).values()] == ['Buy fictional coffee, tea and cups']
    finally:
        store.close(); log.close()

@pytest.mark.anyio
async def test_capture_order_is_explicit_and_atomic(tmp_path):
    engine, counts, fake, log, store = setup(tmp_path, initial=False)
    try:
        result = await tap(engine,await engine.open(),'Start review')
        token = controls(result)['Add things']
        form = await engine.prepare_modal(token,owner_user_id='123')
        assert any(part['component'].get('custom_id') == 'order' for part in form['data']['components'])
        result = await engine.handle(token,'capture-explicit-order',{'value':'Gather fictional documents\nSubmit fictional form','order':'ordered'},owner_user_id='123')
        index = work.index_things(records(fake))
        first = next(ref for ref,r in index.items() if r.title == 'Gather fictional documents')
        second = next(r for r in index.values() if r.title == 'Submit fictional form')
        assert second.data['after'] == [first]
        assert counts['changes'] == 1
    finally:
        store.close(); log.close()
