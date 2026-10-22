import pytest
from gremlin_town.review_card.host import modal_fields

@pytest.mark.parametrize('key', ['ready', 'correct', 'mode', 'item', 'remove', 'add', 'followup', 'order'])
def test_typed_single_choices_have_one_value(key):
    data = {'components':[{'type':18, 'component':{'type':3, 'custom_id':key, 'values':['fictional-choice']}}]}
    assert modal_fields(data) == {key:'fictional-choice'}
    data['components'][0]['component']['values'].append('unoffered-second')
    with pytest.raises(ValueError, match='one option'):
        modal_fields(data)

@pytest.mark.parametrize('key', ['tasks', 'ids'])
def test_named_multiple_choices_remain_lists(key):
    data = {'components':[{'type':18, 'component':{'type':3, 'custom_id':key, 'values':['a','b']}}]}
    assert modal_fields(data) == {key:['a','b']}
