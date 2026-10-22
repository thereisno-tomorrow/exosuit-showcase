from datetime import datetime
from dataclasses import replace
import pytest
from brain_core import work, planning
from brain_core.contracts import canonical_json
from test_moves import item
from test_surface_moves import run

NOW=datetime.fromisoformat('2026-09-30T14:00:00+08:00')

def test_confirmed_delayed_start_releases_only_required_followup():
    stop=item('Stop irrigation',number=2)
    start=item('Start irrigation',data={'planning':{'operation':{'followup_ref':stop.id,'delay_minutes':30,'approved':True}}})
    stop=replace(stop,data={**stop.data,'after':[start.id]})
    index=work.index_things([start,stop])
    assert not work.is_up(stop,index,NOW)
    assert work.has_unfinished_prerequisites(stop,index)
    spec={'operation':{'followup_ref':stop.id,'delay_minutes':30,'approved':True,'actual_start':'2026-09-30T13:10:00+08:00'}}
    from brain_core import moves
    from brain_core.contracts import stable_hash
    change={'operation_id':'actual-start','message':canonical_json(spec),'said_by':'owner','came_by':'direct','moves':[{'move':'organise','thing':start.id,'planning':spec}]}
    context=moves.ExecutionContext(raw_records={r.id:r for r in (start,stop)},write_index=work.write_index([start,stop]),planned_ids={(0,0):'brn_20260930_060000_abcdef12'},operation_hash=stable_hash(change),allowed_code_callers=frozenset({'system:dashboard'}),authenticated_owner_tap=True)
    out=moves.apply_change(index,{**change,'_context':context},actor='system:dashboard',now=NOW)
    assert out.refusal is None
    actual=out.records[0]
    index=work.index_things([start,actual,stop])
    assert work.is_up(stop,index,NOW)
    assert not work.has_unfinished_prerequisites(stop,index)
    assert planning.required_followup(actual,index)['at']=='2026-09-30T13:40:00+08:00'
    refusal=run(actual,{'move':'organise','thing':start.id,'planning':{}},'{}')
    assert refusal.refusal and not refusal.records

def test_verified_portion_does_not_make_future_batch_ready():
    r=item('Sort harvest',data={'planning':{'portions':[{'id':'crates-1-40','quantity':40,'ready':True,'checks':['picked and verified']},{'id':'crates-41-160','quantity':120,'ready':False,'checks':[]}]}})
    assert [p['id'] for p in planning.ready_portions(r,NOW)]==['crates-1-40']
    with pytest.raises(ValueError):
        planning.validate_planning({'portions':[{'id':'x','quantity':1,'ready':True,'checks':[]}]})

def test_exclusive_overnight_occupancy_counts_ended_work():
    a=item('Ferment first batch',state='done',data={'check':{'locator':'fixture','checked_by':'nic'},'planning':{'resources':[{'resource':'vat','start':'2026-09-30T22:00:00+08:00','end':'2026-10-01T10:00:00+08:00'}]}})
    b=item('Ferment second batch',number=2,data={'planning':{'resources':[{'resource':'vat','start':'2026-10-01T08:00:00+08:00','end':'2026-10-01T12:00:00+08:00'}]}})
    conflict=planning.resource_conflicts(work.index_things([a,b]).values())
    assert len(conflict)==1 and conflict[0]['start']=='2026-10-01T08:00:00+08:00'


@pytest.mark.parametrize('data',[{'planning':{'resources':'vat'}},{'remaining_minutes':False},{'review_outcomes':[{'day':'Sunday','outcome':'progress','recorded_at':'now'}]}])
def test_invalid_new_record_fields_refused_at_parser(data):
    from brain_core.errors import BrainError
    with pytest.raises(BrainError):
        item('Invalid stored planning',data=data)


def test_portion_remaining_and_day_approval_cannot_include_future_batches():
    r=item('Sort harvest',data={'planning':{'portions':[{'id':'first40','quantity':40,'ready':True,'checks':['verified'],'done_quantity':20},{'id':'later120','quantity':120,'ready':False,'checks':[]}]}})
    offered=planning.ready_portions(r,NOW)
    assert offered[0]['remaining_quantity']==20
    assert [p['id'] for p in offered]==['first40']


def test_timebox_and_shared_setup_consume_time_without_completion():
    spec={'work_sessions':[{'day':'2026-09-30','minutes':45,'kind':'timebox'}], 'held_time':[{'start':'2026-09-30T15:00:00+08:00','end':'2026-09-30T15:30:00+08:00'}], 'shared_blocks':[{'id':'supplier-trip','kind':'travel','minutes':20,'task_refs':['brn_20260929_060000_0000000000000001']}]}
    planning.validate_planning(spec)
    r=item('Research',data={'planning':spec})
    effort=planning.day_effort([r],'2026-09-30')
    assert effort=={'session_minutes':45,'held_minutes':30,'shared_minutes':20}
    assert r.state=='open'


def test_required_followup_is_schedulable_after_actual_start_without_finishing_startup():
    from brain_core.schedule import eligible_at
    stop=item('Stop irrigation',number=2,data={'timing':{'duration_minutes':5,'padded_minutes':5,'estimated_by':'nic'}})
    start=item('Start irrigation',data={'planning':{'operation':{'followup_ref':stop.id,'delay_minutes':30,'approved':True}}})
    stop=replace(stop,data={**stop.data,'after':[start.id]})
    assert not eligible_at(stop.id,work.index_things([start,stop]),NOW)
    started=replace(start,data={**start.data,'planning':{'operation':{'followup_ref':stop.id,'delay_minutes':30,'approved':True,'actual_start':'2026-09-30T13:30:00+08:00'}}})
    assert eligible_at(stop.id,work.index_things([started,stop]),NOW)


def test_held_local_day_uses_elapsed_minutes_across_dst():
    from datetime import tzinfo, timedelta, date
    class FallBack(tzinfo):
        def utcoffset(self, value):
            return timedelta(hours=-4 if value.date() < date(2026,11,2) else -5)
        def dst(self, value):
            return self.utcoffset(value)-timedelta(hours=-5)
    zone=FallBack()
    r=item('Keep time free',data={'planning':{'held_time':[{'start':'2026-11-01T00:00:00-04:00','end':'2026-11-02T00:00:00-05:00'}]}})
    assert planning.day_effort([r],'2026-11-01',local_zone=zone)['held_minutes']==1500


@pytest.mark.parametrize('state', ['done', 'dropped', 'no_longer_needed'])
def test_ended_work_does_not_offer_or_select_remaining_portions(state):
    spec={'portions':[{'id':'batch','quantity':40,'ready':True,'checks':['verified']}],
          'portion_selections':[{'day':'2026-09-30','portions':[{'id':'batch','quantity':20}]}],
          'resources':[{'resource':'vat','start':'2026-09-30T13:00:00+08:00','end':'2026-09-30T15:00:00+08:00'}]}
    r=replace(item('Sort batch',data={'planning':spec}),state=state)
    assert planning.ready_portions(r,NOW)==()
    assert planning.selected_portions(r,'2026-09-30',NOW)==()
    other=item('Other batch',number=2,data={'planning':{'resources':spec['resources']}})
    assert len(planning.resource_conflicts([r,other]))==1


@pytest.mark.parametrize('kind', ['information', 'reflection', 'idea', 'arrangement'])
def test_retained_material_does_not_offer_work_portions(kind):
    r=replace(item('Batch notes'),data={'material_kind':kind,'planning':{
        'portions':[{'id':'batch','quantity':40,'ready':True,'checks':['verified']}],
        'portion_selections':[{'day':'2026-09-30','portions':[{'id':'batch','quantity':20}]}]}})
    assert planning.ready_portions(r,NOW)==()
    assert planning.selected_portions(r,'2026-09-30',NOW)==()


def test_future_actual_start_refused_without_releasing_followup():
    from test_morning_extensions import surface
    stop=item('Stop irrigation',number=2)
    start=item('Start irrigation')
    stop=replace(stop,data={**stop.data,'after':[start.id]})
    spec={'operation':{'followup_ref':stop.id,'delay_minutes':30,'approved':True,
                       'actual_start':'2026-09-30T14:01:00+08:00'}}
    result=surface([start,stop],{'move':'organise','thing':start.id,'planning':spec},canonical_json(spec))
    assert result.refusal and result.refusal['field']=='planning.operation.actual_start'
    assert not result.records
    assert not work.is_up(stop,work.index_things([start,stop]),NOW)


def test_finishing_source_keeps_confirmed_required_followup_available():
    stop=item('Stop irrigation',number=2)
    start=item('Start irrigation',data={'planning':{'operation':{
        'followup_ref':stop.id,'delay_minutes':30,'approved':True,
        'actual_start':'2026-09-30T13:30:00+08:00'}}})
    stop=replace(stop,data={**stop.data,'after':[start.id]})
    ended=replace(start,state='done')
    index=work.index_things([ended,stop])
    assert work.is_up(stop,index,NOW)
    assert planning.followup_triggered(ended,stop.id)
    assert planning.required_followup(ended,index)['at']=='2026-09-30T14:00:00+08:00'


@pytest.mark.parametrize('change', [
    {'move':'end','reason':'No longer doing this'},
    {'move':'park','until':'tomorrow'},
    {'move':'organise','planning':{}},
])
def test_started_operation_cannot_discard_pending_followup(change):
    from test_morning_extensions import surface
    stop=item('Stop irrigation',number=2)
    start=item('Start irrigation',data={'planning':{'operation':{
        'followup_ref':stop.id,'delay_minutes':30,'approved':True,
        'actual_start':'2026-09-30T13:30:00+08:00'}}})
    stop=replace(stop,data={**stop.data,'after':[start.id]})
    result=surface([start,stop],{'thing':start.id,**change},canonical_json(change))
    assert result.refusal and not result.records
    assert planning.required_followup(start,work.index_things([start,stop]))['at']=='2026-09-30T14:00:00+08:00'


@pytest.mark.parametrize('instant,zone,day,at', [
    ('2026-10-05T09:00:00-04:00','America/New_York','2026-10-05','21:00'),
    ('2026-10-05T18:00:00-04:00','America/New_York','2026-10-06','06:00'),
    ('2026-11-01T01:30:00-04:00','America/New_York','2026-11-01','13:30'),
    ('2026-11-01T01:30:00-05:00','America/New_York','2026-11-01','14:30'),
])
def test_private_appointment_retains_explicit_instant_and_zone_provenance(instant,zone,day,at):
    from test_morning_extensions import surface
    r=item('Dentist')
    result=surface([r],{'move':'date','thing':r.id,'when':instant,'kind':'appointment',
                       'appointment_timezone':zone},f'Reschedule to {instant} ({zone})')
    assert result.refusal is None
    saved=result.records[0].data['dates']['appointment']
    assert (saved['day'],saved['at'])==(day,at)
    assert saved['words']==f'{instant} ({zone})'


@pytest.mark.parametrize('instant,zone,message', [
    ('2026-10-05T09:00:00','America/New_York',None),
    ('2026-10-05T09:00:00-05:00','America/New_York',None),
    ('2026-03-08T02:30:00-05:00','America/New_York',None),
    ('2026-10-05T09:00:01-04:00','America/New_York',None),
    ('2026-10-05T09:00:00-04:00','Missing/Zone',None),
    ('2026-10-05T09:00:00-04:00','America/New_York','Reschedule dentist'),
])
def test_private_appointment_refuses_unguarded_or_wrong_zone_instant(instant,zone,message):
    from test_morning_extensions import surface
    r=item('Dentist')
    result=surface([r],{'move':'date','thing':r.id,'when':instant,'kind':'appointment',
                       'appointment_timezone':zone},message or f'{instant} ({zone})')
    assert result.refusal and not result.records


def test_private_tentative_appointment_keeps_appointment_without_deadline():
    from test_morning_extensions import surface
    r=item('Dentist')
    instant='2026-10-05T09:00:00-04:00'
    result=surface([r],{'move':'date','thing':r.id,'when':instant,'kind':'appointment',
                       'appointment_timezone':'America/New_York','proposed':True},
                   f'{instant} (America/New_York)')
    assert result.refusal is None
    assert result.records[0].data['dates']['appointment']['confirmed'] is False


def test_generic_caller_cannot_use_private_typed_appointment():
    from test_moves import run
    r=item('Dentist')
    result=run([r],'2026-10-05T09:00:00-04:00 (America/New_York)',[{
        'move':'date','thing':r.id,'when':'2026-10-05T09:00:00-04:00',
        'appointment_timezone':'America/New_York'}])
    assert result.refusal and not result.records
