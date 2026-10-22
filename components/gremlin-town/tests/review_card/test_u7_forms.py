from datetime import date
import pytest
from gremlin_town.review_card.models import screen_key
from gremlin_town.review_card.render import modal
from gremlin_town.review_card.actions import make_request
from .test_card import setup

@pytest.mark.anyio
async def test_recovery_does_not_copy_note_between_different_selected_parts(tmp_path):
    engine, _, _, log, store = setup(tmp_path, initial=False)
    try:
        state = store.session(engine._day()); state['day'] = str(engine._day())
        base = dict(action='portion_select',ref='fictional-parent',modal=True,screen=screen_key(state))
        old = dict(base, selected_fields={'ids':['part-a']})
        state['submitted_draft'] = dict(value='Only applies to fictional part A',submission_id='invalid-a',ref='fictional-parent',spec=old)
        store.save(state['day'],state)
        new = dict(base,selected_fields={'ids':['part-b']})
        token = store.issue(state['day'],new)
        form = await engine.prepare_modal(token,owner_user_id='123')
        note = next(p['component'] for p in form['data']['components'] if p['component']['custom_id']=='value')
        assert not note.get('value')
    finally:
        store.close(); log.close()

@pytest.mark.parametrize('action,selected,fields',[('portion_select',{'ids':['part-a']},{'ids':['part-a'],'value':''}),('timebox',{'item':'fictional-parent'},{'minutes':'20','value':''})])
def test_selected_work_notes_are_optional_in_form_and_checked_request(action,selected,fields):
    spec = dict(action=action,ref='fictional-parent',expected='fresh-proof',label='Approve fictional choice',planning={},selected_fields=selected)
    form = modal('fictional',spec,date(2026,10,5))
    note = next(p['component'] for p in form['data']['components'] if p['component']['custom_id']=='value')
    assert not note['required']
    request = make_request('fictional-operation',spec,fields,date(2026,10,5))
    assert request.moves
