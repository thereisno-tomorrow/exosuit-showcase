from datetime import datetime
import pytest
from brain_core import planning, work
from test_moves import item

NOW=datetime.fromisoformat('2026-09-30T14:00:00+08:00')


def test_resource_conflicts_keep_flat_pair_order_across_interleaved_resources():
    def occupancy(resource,start,end):
        return {'resource':resource,'start':f'2026-09-30T{start}:00+08:00','end':f'2026-09-30T{end}:00+08:00'}
    first=item('First setup',number=1,data={'planning':{'resources':[occupancy('pump','09:00','11:00'),occupancy('vat','09:00','11:00')]}})
    second=item('Second setup',number=2,data={'planning':{'resources':[occupancy('vat','10:00','12:00'),occupancy('pump','10:00','12:00')]}})
    third=item('Third setup',number=3,data={'planning':{'resources':[occupancy('pump','10:30','11:30')]}})
    conflicts=planning.resource_conflicts([first,second,third])
    assert [(row['resource'],row['left'],row['right']) for row in conflicts] == [('pump',first.id,second.id),('pump',first.id,third.id),('vat',first.id,second.id),('pump',second.id,third.id)]
    assert [row['start'] for row in conflicts] == ['2026-09-30T10:00:00+08:00','2026-09-30T10:30:00+08:00','2026-09-30T10:00:00+08:00','2026-09-30T10:30:00+08:00']
    assert [row['end'] for row in conflicts] == ['2026-09-30T11:00:00+08:00','2026-09-30T11:00:00+08:00','2026-09-30T11:00:00+08:00','2026-09-30T11:30:00+08:00']


@pytest.mark.parametrize('plain_mapping',[False,True])
def test_reader_observes_nested_operation_insertion_and_mutation(plain_mapping):
    source=item('Start irrigation',number=1)
    followup=item('Stop irrigation',number=2)
    index=work.index_things([source,followup])
    if plain_mapping:
        index=dict(index)
    assert work.is_up(followup,index,NOW)
    assert not work.has_unfinished_prerequisites(followup,index)
    index[source.id].data['planning']={'operation':{'followup_ref':followup.id,'delay_minutes':30,'approved':False}}
    assert not work.is_up(followup,index,NOW)
    assert work.has_unfinished_prerequisites(followup,index)
    operation=index[source.id].data['planning']['operation']
    operation.update(approved=True,actual_start='2026-09-30T13:00:00+08:00')
    assert work.is_up(followup,index,NOW)
    assert not work.has_unfinished_prerequisites(followup,index)
    operation['followup_ref']=source.id
    assert work.is_up(followup,index,NOW)


@pytest.mark.parametrize('operation,expected',[
    ({'followup_ref':'stop','approved':False,'actual_start':'2026-09-30T13:00:00+08:00'},False),
    ({'followup_ref':'stop','approved':True},False),
    ({'followup_ref':'another','approved':True,'actual_start':'2026-09-30T13:00:00+08:00'},False),
    ({'followup_ref':'stop','approved':True,'actual_start':'2026-09-30T13:00:00+08:00'},True),
])
def test_trigger_predicate_retains_each_release_guard(operation,expected):
    source=item('Start irrigation')
    source.data['planning']={'operation':operation}
    assert planning.followup_triggered(source,'stop') is expected
