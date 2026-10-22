"""Physical-start obligations cannot be abandoned through another checked route."""
from dataclasses import replace
import pytest
from brain_core import moves, planning, work
from brain_core.contracts import canonical_json, stable_hash
from test_moves import item, NOW
from test_morning_extensions import surface


def operation_records(*, started=True, project=False):
    parent = item('Irrigation project', number=3, data={'called': 'project'})
    stop = item('Stop irrigation', number=2, data={'parent': parent.id} if project else {})
    spec = {'followup_ref': stop.id, 'delay_minutes': 30, 'approved': True}
    if started:
        spec['actual_start'] = '2026-09-30T13:30:00+08:00'
    start = item('Start irrigation', data={'planning': {'operation': spec}})
    return [start, stop, parent] if project else [start, stop]


@pytest.mark.parametrize('project', [False, True])
@pytest.mark.parametrize('action', [
    {'move': 'end', 'ending': 'dropped', 'reason': 'No longer doing this'},
    {'move': 'end', 'ending': 'no_longer_needed', 'reason': 'No longer doing this'},
    {'move': 'park', 'until': 'tomorrow'},
])
def test_triggered_return_or_its_project_cannot_be_abandoned(project, action):
    records = operation_records(project=project)
    target = records[-1]
    result = surface(records, {'thing': target.id, **action}, canonical_json(action))
    assert result.refusal is not None
    assert not result.records


def test_undo_cannot_erase_confirmed_physical_start():
    records = operation_records(started=False)
    spec = operation_records()[0].data['planning']
    started = surface(records, {'move': 'organise', 'thing': records[0].id, 'planning': spec}, canonical_json(spec))
    assert started.refusal is None
    current = started.records[0]
    change = {'operation_id': 'undo-start', 'message': 'Undo confirmed startup', 'said_by': 'owner',
              'came_by': 'direct', 'moves': [{'move': 'undo', 'change': 'morning-extension'}]}
    context = moves.ExecutionContext(raw_records={r.id: r for r in [*records, current]},
        write_index=work.write_index([*records, current]), planned_ids={(0, 0): 'brn_20260930_060000_abcd1234'},
        undo_changes={'morning-extension': [current.id]}, operation_hash=stable_hash(change),
        allowed_code_callers=frozenset({'system:dashboard'}), authenticated_owner_tap=True)
    result = moves.apply_change(work.index_things([*records, current]), {**change, '_context': context},
                                actor='system:dashboard', now=NOW)
    assert result.refusal is not None
    assert not result.records
    assert planning.followup_triggered(current, records[1].id)



@pytest.mark.parametrize('project', [False, True])
def test_unstarted_operation_does_not_prevent_ordinary_drop(project):
    records = operation_records(started=False, project=project)
    action = {'move': 'end', 'thing': records[-1].id, 'ending': 'dropped', 'reason': 'No longer doing this'}
    result = surface(records, action, canonical_json(action))
    assert result.refusal is None
    assert len(result.records) == 1


def test_completed_followup_does_not_prevent_project_drop():
    records = operation_records(project=True)
    records[1] = replace(records[1], state='done', data={**records[1].data,
        'check': {'locator': 'fixture', 'checked_by': 'nic'}})
    action = {'move': 'end', 'thing': records[-1].id, 'ending': 'dropped', 'reason': 'No longer doing this'}
    result = surface(records, action, canonical_json(action))
    assert result.refusal is None


def test_multiple_started_sources_still_protect_one_return():
    records = operation_records()
    second = replace(records[0], id='brn_20260929_060000_0000000000000004', title='Start second pump')
    action = {'move': 'end', 'thing': records[1].id, 'ending': 'dropped', 'reason': 'No longer doing this'}
    result = surface([*records, second], action, canonical_json(action))
    assert result.refusal is not None
    assert not result.records


@pytest.mark.parametrize('project', [False, True])
def test_exact_recent_ordinary_drop_can_be_undone(project):
    from test_moves import run
    original = item('Ordinary work', data={'called': 'project'} if project else {})
    dropped = run([original], 'Drop Ordinary work because plans changed', [
        {'move': 'end', 'thing': original.id, 'ending': 'dropped', 'reason': 'plans changed',
         'words': 'Drop Ordinary work because plans changed'}], operation='ordinary-drop')
    assert dropped.refusal is None
    result = run([original, *dropped.records], 'undo Ordinary work drop', [
        {'move': 'undo', 'change': 'ordinary-drop', 'words': 'undo Ordinary work drop'}],
        operation='ordinary-undo', undo_changes={'ordinary-drop': [r.id for r in dropped.records]})
    assert result.refusal is None
    assert result.records[0].state == 'open'
    assert result.records[0].supersedes == (original.id, dropped.records[0].id)


def test_verified_completion_discharges_the_required_return():
    records = operation_records()
    result = surface(records, {'move': 'tick', 'thing': records[1].id}, 'Stop irrigation finished')
    assert result.refusal is None
    assert result.records[0].state == 'done'


def test_target_alias_after_a_prior_edit_is_still_protected():
    records = operation_records()
    target = records[1]
    current = replace(target, id='brn_20260929_060000_aaaa1111', supersedes=(target.id,))
    source = replace(records[0], data={**records[0].data, 'planning': {'operation': {
        **records[0].data['planning']['operation'], 'followup_ref': current.id}}})
    action = {'move': 'end', 'thing': current.id, 'ending': 'dropped', 'reason': 'No longer doing this'}
    result = surface([source, target, current], action, canonical_json(action))
    assert result.refusal is not None
    assert not result.records
