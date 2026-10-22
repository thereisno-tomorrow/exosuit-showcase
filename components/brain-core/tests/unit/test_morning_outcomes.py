from test_surface_moves import run
from test_moves import item
from brain_core import work

def test_progress_remaining_is_canonical():
    r=item('Clear room',data={'timing':{'duration_minutes':120,'padded_minutes':120,'estimated_by':'nic'}})
    out=run(r,{'move':'organise','thing':r.id,'review_outcome':'progress','review_day':'2026-09-29','note':'Half cleared','remaining_minutes':45},'Half cleared; 45 minutes left')
    assert out.refusal is None
    saved=out.records[0]
    assert saved.state=='open' and work.length_of(saved,work.index_things([saved]))==45
    assert saved.data['timing']['duration_minutes']==45 and saved.data['timing']['padded_minutes']==45
    assert work.has_review_outcome(saved,work.index_things([saved]),'2026-09-29')

def test_not_done_no_automatic_day():
    r=item('Call Hall')
    out=run(r,{'move':'organise','thing':r.id,'review_outcome':'not_done','review_day':'2026-09-29'},"Didn't do it")
    assert out.refusal is None
    assert out.records[0].state=='open' and not out.records[0].data.get('dates')

def test_dated_skip_does_not_end_series():
    r=item('Opening checks',data={'repeat':'daily'})
    out=run(r,{'move':'organise','thing':r.id,'occurrence_outcome':'skipped','review_day':'2026-09-29'},'Skipped opening checks for 2026-09-29')
    assert out.refusal is None
    assert out.records[0].state=='open'
    assert out.records[0].data['occurrence']=={'day':'2026-09-29','outcome':'skipped'}


def test_confirmed_arrival_removes_only_person_wait():
    r=item('Complete form',data={'needs':{'from':'Hall','what':'letter'}, 'dates':{'deadline':{'day':'2026-10-01','read_by':'code','read_on':'2026-09-29','words':'1 October'}}})
    out=run(r,{'move':'organise','thing':r.id,'clear_wait':True,'note':'Hall letter arrived'},'Hall letter arrived')
    assert out.refusal is None
    assert 'needs' not in out.records[0].data
    assert out.records[0].data['dates']==r.data['dates']


def test_outcome_extensions_require_authenticated_surface():
    r=item('Call Hall')
    out=run(r,{'move':'organise','thing':r.id,'review_outcome':'not_done','review_day':'2026-09-29'},"Didn't do it",tap=False)
    assert out.refusal and not out.records


def test_retained_reflection_is_not_personal_work():
    r=item('Feeling overwhelmed',data={'timing':{'duration_minutes':30,'padded_minutes':30,'estimated_by':'nic'}})
    out=run(r,{'move':'organise','thing':r.id,'material_kind':'reflection'},'Keep as reflection')
    assert out.refusal is None
    saved=out.records[0]; index=work.index_things([saved])
    assert saved.title==r.title and saved.body==r.body and saved.state=='open'
    assert not work.is_up(saved,index, __import__('test_moves').NOW)
    assert work.length_of(saved,index)==0
