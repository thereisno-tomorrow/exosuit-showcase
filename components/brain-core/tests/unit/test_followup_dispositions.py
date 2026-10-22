"""Governed changes to physical-operation returns preserve their accepted history."""
from copy import deepcopy
from dataclasses import replace
import pytest
from brain_core import moves, planning, work
from brain_core.contracts import canonical_json
from test_moves import item, NOW, run
from test_morning_extensions import surface
from test_followup_obligation_guards import operation_records


def records():
    return [*operation_records(), item('Stop pump with timer', number=4,
        data={'timing': {'duration_minutes': 5, 'padded_minutes': 5, 'estimated_by': 'nic'}})]


def request(items, **changes):
    return {'kind': 'transfer', 'from_ref': items[1].id, 'to_ref': items[-1].id,
            'at': '2026-09-30T14:00:00+08:00', 'assignee': 'Nic', 'approved': True,
            'reason': 'Nic agreed to stop the pump with the replacement timer', **changes}


def apply(items, spec):
    from hashlib import sha256
    from brain_core.contracts import stable_hash
    message = canonical_json(spec)
    change = {'operation_id': 'morning-extension', 'message': message, 'said_by': 'owner',
              'came_by': 'direct', 'moves': [{'move': 'organise', 'thing': items[0].id,
                                            'followup_disposition': spec}]}
    context = moves.ExecutionContext(raw_records={r.id: r for r in items},
        write_index=work.write_index(items),
        planned_ids={(0, 0): 'brn_20260930_060000_' + sha256(message.encode()).hexdigest()[:16]},
        operation_hash=stable_hash(change), allowed_code_callers=frozenset({'system:review-card'}),
        authenticated_owner_tap=True)
    return moves.apply_change(work.index_things(items), {**change, '_context': context},
                              actor='system:review-card', now=NOW)


def transfer(items):
    out = apply(items, request(items))
    assert out.refusal is None, out.refusal
    return out.records[0]


def test_transfer_preserves_start_and_projects_new_required_return():
    items = records()
    updated = transfer(items)
    assert updated.data['planning']['operation'] == items[0].data['planning']['operation']
    history = updated.data['planning']['followup_dispositions']
    assert len(history) == 1 and history[0]['change_id'] == 'morning-extension'
    assert history[0]['recorded_at'] == NOW.isoformat()
    assert history[0]['evidence']['excerpt'] == request(items)['reason']
    index = work.index_things([*items, updated])
    followup = planning.required_followup(index[items[0].id], index)
    assert followup['item_ref'] == items[-1].id and followup['at'] == request(items)['at']
    assert planning.followup_triggered(index[items[0].id], items[-1].id)
    assert not planning.followup_triggered(index[items[0].id], items[1].id)
    assert work.is_up(items[-1].id, index, NOW)
    old_drop = surface([*items, updated], {'move': 'end', 'thing': items[1].id,
        'ending': 'dropped', 'reason': 'Replaced with agreed timer'}, 'Replaced with agreed timer')
    assert old_drop.refusal is None
    replacement_drop = surface([*items, updated], {'move': 'end', 'thing': items[-1].id,
        'ending': 'dropped', 'reason': 'No longer doing this'}, 'No longer doing this')
    assert replacement_drop.refusal and not replacement_drop.records


def test_explicit_discharge_preserves_task_states_and_start():
    items = records()
    spec = {'kind': 'no_longer_required', 'from_ref': items[1].id,
            'reason': 'Pump was physically disconnected and no return is needed'}
    out = apply(items, spec)
    assert out.refusal is None, out.refusal
    updated = out.records[0]
    index = work.index_things([*items, updated])
    assert planning.required_followup(updated, index) is None
    assert not planning.followup_triggered(updated, items[1].id)
    assert updated.data['planning']['operation'] == items[0].data['planning']['operation']
    assert updated.state == items[0].state and index[items[1].id].state == 'open'
    dropped = surface([*items, updated], {'move': 'end', 'thing': items[0].id,
        'ending': 'dropped', 'reason': 'Pump was physically disconnected'}, 'Pump was physically disconnected')
    assert dropped.refusal is None


@pytest.mark.parametrize('change', [
    {'from_ref': 'brn_20260929_060000_0000000000000099'}, {'approved': False},
    {'assignee': ''}, {'reason': ''}, {'at': '2026-09-30T14:01:00+08:00'},
    {'to_ref': 'brn_20260929_060000_0000000000000099'},
])
def test_invalid_transfer_refuses_atomically(change):
    items = records()
    out = apply(items, request(items, **change))
    assert out.refusal and not out.records


@pytest.mark.parametrize('kind', ['closed', 'unsized', 'blocked', 'conflict', 'cycle', 'material', 'unstarted', 'completed'])
def test_ineligible_or_conflicting_transition_refuses(kind):
    items = records()
    if kind == 'closed':
        items[-1] = replace(items[-1], state='dropped', data={**items[-1].data, 'reason': 'Plans changed'})
    elif kind == 'unsized':
        items[-1] = item(items[-1].title, number=4)
    elif kind == 'blocked':
        items[-1] = replace(items[-1], data={**items[-1].data, 'after': [items[1].id]})
    elif kind == 'conflict':
        second = item('Start second pump', number=5, data={'planning': {'operation': {
            **items[0].data['planning']['operation'], 'followup_ref': items[-1].id}}})
        items.insert(2, second)
    elif kind == 'cycle':
        items[-1] = replace(items[-1], data={**items[-1].data, 'planning': {'operation': {
            **items[0].data['planning']['operation'], 'followup_ref': items[0].id}}})
    elif kind == 'material':
        items[-1] = replace(items[-1], data={**items[-1].data, 'material_kind': 'information'})
    elif kind == 'unstarted':
        data = deepcopy(items[0].data)
        data['planning']['operation'].pop('actual_start')
        items[0] = replace(items[0], data=data)
    elif kind == 'completed':
        items[1] = replace(items[1], state='done', data={**items[1].data, 'check': {'locator': 'fixture', 'checked_by': 'nic'}})
    out = apply(items, request(items))
    assert out.refusal and not out.records


def test_stale_chain_and_cycle_back_are_refused():
    items = records()
    updated = transfer(items)
    current = [updated, *items[1:]]
    assert apply(current, request(items)).refusal
    spec = request(items, from_ref=items[-1].id, to_ref=items[1].id)
    assert apply(current, spec).refusal


def test_generic_planning_cannot_create_or_rewrite_dispositions():
    items = records()
    updated = transfer(items)
    original_spec = deepcopy(items[0].data['planning'])
    out = surface([updated, *items[1:]], {'move': 'organise', 'thing': items[0].id,
        'planning': original_spec}, canonical_json(original_spec))
    assert out.refusal is None
    assert out.records[0].data['planning']['followup_dispositions'] == updated.data['planning']['followup_dispositions']
    changed = deepcopy(updated.data['planning'])
    changed['followup_dispositions'][0]['reason'] = 'Invented replacement reason'
    out = surface([updated, *items[1:]], {'move': 'organise', 'thing': items[0].id,
        'planning': changed}, canonical_json(changed))
    assert out.refusal and not out.records
    out = surface(items, {'move': 'organise', 'thing': items[0].id,
        'planning': updated.data['planning']}, canonical_json(updated.data['planning']))
    assert out.refusal and not out.records


def test_undo_cannot_remove_accepted_disposition():
    items = records()
    updated = transfer(items)
    out = run([*items, updated], 'Undo changed pump return',
        [{'move': 'undo', 'change': 'morning-extension', 'words': 'Undo changed pump return'}],
        undo_changes={'morning-extension': [updated.id]})
    assert out.refusal and not out.records


def test_public_move_cannot_supply_private_disposition():
    items = records()
    spec = request(items)
    out = run(items, canonical_json(spec), [{'move': 'organise', 'thing': items[0].id,
                                            'followup_disposition': spec}])
    assert out.refusal and not out.records


def test_transfer_reloads_across_midnight_and_normalizes_record_aliases():
    from datetime import timedelta
    from brain_core.records import parse_record, render_record
    items = records()
    updated = parse_record(render_record(transfer(items)), 'fixture')
    target = items[-1]
    fresh_target = replace(target, id='brn_20260930_060000_abc123456789', supersedes=(target.id,))
    index = work.index_things([*items, updated, fresh_target])
    assert planning.required_followup(index[items[0].id], index)['item_ref'] == target.id
    assert planning.followup_triggered(index[items[0].id], target.id)
    assert work.is_up(target.id, index, NOW + timedelta(days=1))
    assert not work.has_unfinished_prerequisites(target.id, index)
    assert len(updated.data['planning']['followup_dispositions']) == 1


@pytest.mark.parametrize('kind', ['transfer', 'no_longer_required'])
def test_exact_retry_returns_one_identical_canonical_disposition(kind):
    from hashlib import sha256
    from brain_core.contracts import stable_hash
    items = records()
    spec = request(items) if kind == 'transfer' else {
        'kind': kind, 'from_ref': items[1].id, 'reason': 'Pump disconnected'}
    message = canonical_json(spec)
    change = {'operation_id': 'retry-disposition', 'message': message, 'said_by': 'owner',
              'came_by': 'direct', 'moves': [{'move': 'organise', 'thing': items[0].id,
                                            'followup_disposition': spec}]}
    planned = {(0, 0): 'brn_20260930_060000_' + sha256(message.encode()).hexdigest()[:16]}
    def invoke(all_records):
        context = moves.ExecutionContext(raw_records={r.id: r for r in all_records},
            write_index=work.write_index(all_records), planned_ids=planned,
            operation_hash=stable_hash(change), allowed_code_callers=frozenset({'system:review-card'}),
            authenticated_owner_tap=True)
        return moves.apply_change(work.index_things(all_records), {**change, '_context': context},
                                  actor='system:review-card', now=NOW)
    first = invoke(items)
    assert first.refusal is None
    second = invoke([*items, *first.records])
    assert second.refusal is None and second.reply['status'] == 'duplicate'
    assert second.records[0].sha256 == first.records[0].sha256
    assert len(second.records[0].data['planning']['followup_dispositions']) == 1


def test_reason_with_quotes_is_preserved_exactly():
    items = records()
    spec = request(items, reason='Nic agreed: "use timer B"')
    out = apply(items, spec)
    assert out.refusal is None
    assert out.records[0].data['planning']['followup_dispositions'][0]['evidence']['excerpt'] == spec['reason']


def test_replacement_cannot_bypass_resource_conflicts():
    items = records()
    occupancy = {'resource': 'timer', 'start': '2026-09-30T13:00:00+08:00', 'end': '2026-09-30T16:00:00+08:00'}
    items[-1] = replace(items[-1], data={**items[-1].data, 'planning': {'resources': [occupancy]}})
    other = item('Timer already occupied', number=5, data={'planning': {'resources': [occupancy]}})
    items.insert(2, other)
    out = apply(items, request(items))
    assert out.refusal and not out.records


def test_transfer_releases_its_source_dependency_but_not_unrelated_dependencies():
    items = records()
    items[-1] = replace(items[-1], data={**items[-1].data, 'after': [items[0].id]})
    updated = transfer(items)
    index = work.index_things([*items, updated])
    assert work.is_up(items[-1].id, index, NOW)
    assert not work.has_unfinished_prerequisites(items[-1].id, index)


def test_history_parser_rejects_broken_chain_or_unquoted_evidence():
    items = records()
    spec = deepcopy(transfer(items).data['planning'])
    spec['followup_dispositions'][0]['evidence']['excerpt'] = 'Invented evidence'
    with pytest.raises(ValueError, match='quoted owner evidence'):
        planning.validate_planning(spec)
    spec = deepcopy(transfer(items).data['planning'])
    spec['followup_dispositions'][0]['from_ref'] = items[-1].id
    with pytest.raises(ValueError, match='exact chain'):
        planning.validate_planning(spec)


def test_other_authenticated_surface_cannot_use_review_disposition():
    items = records()
    spec = request(items)
    out = surface(items, {'move': 'organise', 'thing': items[0].id,
                          'followup_disposition': spec}, canonical_json(spec))
    assert out.refusal and not out.records


@pytest.mark.parametrize('constraint', ['window', 'planned_later', 'parent_wait'])
def test_transfer_preserves_due_time_window_and_inherited_readiness(constraint):
    items = records()
    target = items[-1]
    if constraint == 'window':
        items[-1] = replace(target, data={**target.data, 'planning': {'windows': [
            {'start': '2026-09-30T16:00:00+08:00', 'end': '2026-09-30T17:00:00+08:00'}]}})
    elif constraint == 'planned_later':
        items[-1] = replace(target, data={**target.data, 'dates': {'planned': {
            'day': '2026-10-01', 'confirmed': True, 'words': 'tomorrow', 'read_by': 'nic', 'read_on': '2026-09-30'}}})
    else:
        parent = item('Safety crew project', number=6, data={'called': 'project',
            'needs': {'from': 'Safety crew', 'what': 'approval'}})
        items[-1] = replace(target, data={**target.data, 'parent': parent.id})
        items.insert(2, parent)
    out = apply(items, request(items))
    assert out.refusal and not out.records


@pytest.mark.parametrize('malformed', [{'kind': []}, {'kind': {}}, {'reason': None}, {'assignee': []}, {'at': None}, {'to_ref': []}])
def test_malformed_private_request_refuses_without_crashing(malformed):
    items = records()
    out = apply(items, request(items, **malformed))
    assert out.refusal and not out.records
