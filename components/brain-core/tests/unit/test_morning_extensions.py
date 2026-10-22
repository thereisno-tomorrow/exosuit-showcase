from hashlib import sha256
from dataclasses import replace
import pytest
from brain_core import moves, planning, work
from brain_core.contracts import canonical_json, stable_hash
from brain_core.errors import BrainError
from test_moves import NOW, item


def surface(records, move, message):
    change={'operation_id':'morning-extension','message':message,'said_by':'owner','came_by':'direct','moves':[move]}
    context=moves.ExecutionContext(raw_records={r.id:r for r in records},write_index=work.write_index(records),planned_ids={(0,0):'brn_20260930_060000_'+sha256(message.encode()).hexdigest()[:16]},operation_hash=stable_hash(change),allowed_code_callers=frozenset({'system:dashboard'}),authenticated_owner_tap=True)
    return moves.apply_change(work.index_things(records),{**change,'_context':context},actor='system:dashboard',now=NOW)


def test_dropped_requirement_waits_until_explicit_target_removal():
    delivery=item('Delivery',state='dropped',data={'reason':'supplier canceled'})
    refill=item('Refill shelves',number=2,data={'after':[delivery.id]})
    unrelated=item('Payroll',number=3,data={'after':[delivery.id]})
    index=work.index_things([delivery,refill,unrelated])
    assert not work.is_up(refill,index,NOW)
    change={'remove':[delivery.id],'add':[]}
    result=surface([delivery,refill,unrelated],{'move':'organise','thing':refill.id,'requirements':change},'Remove requirement for Refill shelves '+canonical_json(change))
    assert result.refusal is None
    fresh=work.index_things([delivery,refill,unrelated,*result.records])
    assert work.is_up(refill.id,fresh,NOW)
    assert not work.is_up(unrelated,fresh,NOW)
    assert fresh[delivery.id].state=='dropped'


def test_explicit_replacement_preserves_actual_dependency():
    old=item('Canceled delivery',state='dropped',data={'reason':'canceled'})
    replacement=item('Collect replacement',number=2)
    successor=item('Refill shelves',number=3,data={'after':[old.id]})
    change={'remove':[old.id],'add':[replacement.id]}
    result=surface([old,replacement,successor],{'move':'organise','thing':successor.id,'requirements':change},canonical_json(change))
    assert result.refusal is None
    saved=result.records[0]
    assert saved.data['after']==[replacement.id]
    assert not work.is_up(saved,work.index_things([old,replacement,saved]),NOW)


def test_ready_group_selection_and_partial_outcome_keep_parent_open():
    portions=[{'id':'first40','quantity':40,'ready':True,'checks':['picked and verified']},{'id':'later120','quantity':120,'ready':False,'checks':[]}]
    r=item('Sort harvest',data={'planning':{'portions':portions}})
    selection={'day':'2026-09-30','ids':['first40']}
    result=surface([r],{'move':'organise','thing':r.id,'portion_selection':selection},canonical_json(selection))
    assert result.refusal is None
    chosen=result.records[0]
    assert planning.selected_portions(chosen,'2026-09-30',NOW)[0]['selected_quantity']==40
    bad={'day':'2026-09-30','ids':['later120']}
    assert surface([r],{'move':'organise','thing':r.id,'portion_selection':bad},canonical_json(bad)).refusal
    outcome=[{'id':'first40','done_quantity':20,'text':'20 crates sorted'}]
    # Fresh operation ID prevents a fake retry context; only the canonical updated record is used.
    progressed=surface([chosen],{'move':'organise','thing':r.id,'portion_outcomes':outcome,'review_day':'2026-09-30'},canonical_json(outcome))
    assert progressed.refusal is None
    saved=progressed.records[0]
    assert saved.state=='open'
    assert planning.ready_portions(saved,NOW)[0]['remaining_quantity']==20
    assert saved.data['planning']['portions'][1]['quantity']==120 and not saved.data['planning']['portions'][1]['ready']
    assert len(saved.data['planning']['portion_history'])==1
    reduced=[{'id':'first40','done_quantity':10}]
    assert surface([saved],{'move':'organise','thing':r.id,'portion_outcomes':reduced,'review_day':'2026-09-30'},canonical_json(reduced)).refusal


@pytest.mark.parametrize('kind',['information','reflection','idea','arrangement'])
def test_retained_material_remains_saved_and_is_not_an_action(kind):
    r=item('Original note',data={'dates':{'appointment':{'day':'2026-10-01','at':'15:00','end':'2026-10-01','read_by':'code','read_on':'2026-09-30','words':'1 October at 3 pm'}}})
    out=surface([r],{'move':'organise','thing':r.id,'material_kind':kind},'Keep as '+kind)
    assert out.refusal is None
    saved=out.records[0]; index=work.index_things([saved])
    assert saved.body==r.body and saved.title==r.title
    assert not work.is_up(saved,index,NOW) and work.length_of(saved,index)==0
    assert work.dates_of(saved,index)==work.dates_of(r,work.index_things([r]))
    assert work.needs_nic(saved,index,NOW)==()


@pytest.mark.parametrize('move',[{'material_kind':[]},{'portion_selection':{'day':'2026-09-30','ids':[{}]}},{'portion_outcomes':[{'id':[],'done_quantity':1}],'review_day':'2026-09-30'}])
def test_malformed_extensions_refuse_structurally(move):
    r=item('Sort harvest')
    out=surface([r],{'move':'organise','thing':r.id,**move},canonical_json(move))
    assert out.refusal and not out.records
