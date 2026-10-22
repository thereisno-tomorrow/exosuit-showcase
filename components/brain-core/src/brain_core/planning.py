"""Pure confirmed operation, identified portion and exclusive resource planning."""
from datetime import UTC, date, datetime, time, timedelta
from typing import Mapping
import re
from hashlib import sha256


def _instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Planning timestamps require an explicit time zone')
    return result


def _day(value):
    if not isinstance(value,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        raise ValueError('Planning days must be YYYY-MM-DD')
    return date.fromisoformat(value)


def validate_planning(value):
    if not isinstance(value, Mapping) or set(value) - {'resources', 'portions', 'operation', 'work_sessions', 'held_time', 'shared_blocks', 'windows', 'portion_selections', 'portion_history', 'followup_dispositions'}:
        raise ValueError('Unknown planning fields')
    for field in ('resources', 'portions', 'work_sessions', 'held_time', 'shared_blocks', 'windows', 'portion_selections', 'portion_history', 'followup_dispositions'):
        if field in value and not isinstance(value[field], list):
            raise ValueError('Planning collections must be lists')
    for resource in value.get('resources', []):
        if not isinstance(resource, Mapping) or set(resource) != {'resource', 'start', 'end'} or not isinstance(resource['resource'], str) or not resource['resource'].strip():
            raise ValueError('Resource occupancy needs its name, start and end')
        if _instant(resource['start']) >= _instant(resource['end']):
            raise ValueError('Resource occupancy must end after it starts')
    ids=set()
    quantities={}
    for portion in value.get('portions', []):
        if not isinstance(portion, Mapping) or set(portion) - {'id', 'quantity', 'ready', 'checks', 'window_start', 'window_end', 'done_quantity'}:
            raise ValueError('Unknown portion fields')
        if not isinstance(portion.get('id'),str) or not portion['id'].strip() or portion['id'] in ids:
            raise ValueError('Portions need distinct identities')
        ids.add(portion['id'])
        quantities[portion['id']]=portion.get('quantity')
        if type(portion.get('quantity')) is not int or portion['quantity'] <= 0 or type(portion.get('ready')) is not bool:
            raise ValueError('Portions need positive whole quantities and explicit readiness')
        done = portion.get('done_quantity', 0)
        if type(done) is not int or not 0 <= done <= portion['quantity']:
            raise ValueError('Portion completion must be between zero and its identified quantity')
        checks=portion.get('checks')
        if not isinstance(checks,list) or any(not isinstance(check,str) or not check.strip() for check in checks) or portion['ready'] and not checks:
            raise ValueError('Ready portions require confirmed checks')
        for field in ('window_start','window_end'):
            if field in portion:
                _instant(portion[field])
        if 'window_start' in portion and 'window_end' in portion and _instant(portion['window_start']) >= _instant(portion['window_end']):
            raise ValueError('Portion window must end after it starts')
    for session in value.get('work_sessions', []):
        if not isinstance(session, Mapping) or set(session) != {'day', 'minutes', 'kind'} or session['kind'] != 'timebox' or type(session['minutes']) is not int or session['minutes'] <= 0:
            raise ValueError('A work session needs an explicit day and positive timebox minutes')
        _day(session['day'])
    for field in ('held_time', 'windows'):
        for window in value.get(field, []):
            if not isinstance(window, Mapping) or set(window) != {'start', 'end'} or _instant(window['start']) >= _instant(window['end']):
                raise ValueError('Time windows need zoned start and later end')
    block_ids=set()
    for block in value.get('shared_blocks', []):
        if not isinstance(block, Mapping) or set(block) != {'id', 'kind', 'minutes', 'task_refs'} or block['kind'] not in {'travel', 'setup'} or not isinstance(block['id'], str) or not block['id'].strip() or block['id'] in block_ids:
            raise ValueError('Shared blocks need a distinct identity and travel/setup kind')
        block_ids.add(block['id'])
        if type(block['minutes']) is not int or block['minutes'] <= 0 or not isinstance(block['task_refs'], list) or not block['task_refs'] or len(set(block['task_refs'])) != len(block['task_refs']) or any(not isinstance(ref,str) or not re.fullmatch(r'brn_\d{8}_\d{6}_[0-9a-f]{4,16}',ref) for ref in block['task_refs']):
            raise ValueError('Shared blocks need positive minutes and distinct exact task refs')
    for selection in value.get('portion_selections', []):
        if not isinstance(selection, Mapping) or set(selection) != {'day', 'portions'} or not isinstance(selection['portions'], list):
            raise ValueError('Portion selections need their day and exact quantity snapshots')
        _day(selection['day'])
        selected=set()
        for portion in selection['portions']:
            if not isinstance(portion,Mapping) or set(portion) != {'id','quantity'} or portion['id'] not in ids or portion['id'] in selected or type(portion['quantity']) is not int or not 0 < portion['quantity'] <= quantities[portion['id']]:
                raise ValueError('A portion selection needs existing distinct ids and positive quantities')
            selected.add(portion['id'])
    for outcome in value.get('portion_history', []):
        if not isinstance(outcome,Mapping) or set(outcome)-{'day','id','done_quantity','text','corrected'} or outcome.get('id') not in ids or type(outcome.get('done_quantity')) is not int or not 0 <= outcome['done_quantity'] <= quantities[outcome['id']] or type(outcome.get('corrected',False)) is not bool:
            raise ValueError('Portion history needs an identified dated completion quantity')
        _day(outcome['day'])
        if 'text' in outcome and (not isinstance(outcome['text'],str) or not outcome['text'].strip()):
            raise ValueError('Portion history text must be nonempty')
    operation=value.get('operation')
    if operation is not None:
        if not isinstance(operation,Mapping) or set(operation)-{'followup_ref','delay_minutes','approved','actual_start'}:
            raise ValueError('Unknown operation fields')
        if not isinstance(operation.get('followup_ref'),str) or not re.fullmatch(r'brn_\d{8}_\d{6}_[0-9a-f]{4,16}',operation['followup_ref']):
            raise ValueError('Operation needs the exact follow-up record')
        if type(operation.get('delay_minutes')) is not int or operation['delay_minutes'] < 0 or type(operation.get('approved')) is not bool:
            raise ValueError('Operation needs explicit approval and relative whole minutes')
        if 'actual_start' in operation:
            _instant(operation['actual_start'])
            if not operation['approved']:
                raise ValueError('Actual startup requires upfront operation approval')


    history = value.get('followup_dispositions', [])
    if history and (not operation or not operation.get('actual_start')):
        raise ValueError('Follow-up dispositions require confirmed physical startup')
    current = operation.get('followup_ref') if operation else None
    visited = {current}
    previous_at = _instant(operation['actual_start']) if history else None
    for entry in history:
        kind = entry.get('kind') if isinstance(entry, Mapping) else None
        fields = {'kind', 'from_ref', 'reason', 'change_id', 'recorded_at', 'evidence'}
        if kind == 'transfer':
            fields |= {'to_ref', 'at', 'assignee', 'approved'}
        if not isinstance(kind, str) or kind not in {'transfer', 'no_longer_required'} or set(entry) != fields:
            raise ValueError('Unknown follow-up disposition history fields')
        if current is None or entry['from_ref'] != current:
            raise ValueError('Follow-up disposition history must retain its exact chain')
        for key in ('reason', 'change_id'):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError('Follow-up history needs its reason and change identity')
        recorded_at = _instant(entry['recorded_at'])
        if recorded_at < previous_at:
            raise ValueError('Follow-up history timestamps cannot precede confirmed startup or prior history')
        previous_at = recorded_at
        evidence = entry['evidence']
        if not isinstance(evidence, Mapping) or dict(evidence) != {
                'source_type': 'session_message', 'locator': entry['change_id'],
                'content_sha256': sha256(entry['reason'].encode('utf-8')).hexdigest(),
                'excerpt': entry['reason']}:
            raise ValueError('Follow-up history requires exact quoted owner evidence')
        if kind == 'transfer':
            target = entry['to_ref']
            if not isinstance(target, str) or not re.fullmatch(r'brn_\d{8}_\d{6}_[0-9a-f]{4,16}', target) or target in visited:
                raise ValueError('Follow-up transfer history cannot cycle or repeat targets')
            if entry['approved'] is not True or not isinstance(entry['assignee'], str) or not entry['assignee'].strip():
                raise ValueError('Follow-up transfer requires explicitly agreed responsibility')
            if _instant(entry['at']) != _instant(operation['actual_start']) + timedelta(minutes=operation['delay_minutes']):
                raise ValueError('Follow-up transfer retains its original due instant')
            visited.add(target)
            current = target
        else:
            current = None


def active_followup_ref(source):
    """Fold canonical accepted history without changing immutable startup fields."""
    spec = source.data.get('planning', {}) if source else {}
    operation = spec.get('operation')
    if not operation:
        return None
    history = spec.get('followup_dispositions', [])
    if history:
        return history[-1].get('to_ref') if history[-1]['kind'] == 'transfer' else None
    return operation['followup_ref']


def ready_portions(record, now):
    """Only identified ready portions with confirmed checks inside their own windows."""
    if record.state in {"done", "dropped", "no_longer_needed"} or record.data.get("material_kind"):
        return ()
    result=[]
    for portion in record.data.get('planning',{}).get('portions',[]):
        remaining=portion['quantity']-portion.get('done_quantity',0)
        if remaining and portion['ready'] and portion['checks'] and ('window_start' not in portion or _instant(portion['window_start']) <= now) and ('window_end' not in portion or now < _instant(portion['window_end'])):
            result.append({**portion, 'remaining_quantity':remaining})
    return tuple(result)


def followup_triggered(source, followup_ref):
    """Only this operation's approved, confirmed actual start releases its follow-up."""
    operation = source.data.get('planning', {}).get('operation', {}) if source else {}
    return bool(active_followup_ref(source) == followup_ref
                and operation.get('approved') and operation.get('actual_start'))


def triggered_followup_sources(index, followup_ref):
    """Fresh inverse lookup; callers supply the canonical current work index."""
    return tuple(source for source in index.values()
                 if followup_triggered(source, followup_ref))


def required_followup(record, index):
    operation=record.data.get('planning',{}).get('operation')
    target = active_followup_ref(record)
    if not operation or target is None:
        return None
    actual=operation.get('actual_start')
    return {'item_ref':target,'approved':operation['approved'], 'conditional':not bool(actual), 'at':(_instant(actual)+timedelta(minutes=operation['delay_minutes'])).isoformat() if actual else None, 'missing':target not in index}


def resource_conflicts(records):
    """Occupancy is real even when the work item is ended, and can span midnight."""
    groups = {}
    position = 0
    for record in records:
        for occupancy in record.data.get('planning', {}).get('resources', []):
            groups.setdefault(occupancy['resource'], []).append((position, record.id, occupancy))
            position += 1
    conflicts = []
    for resource, occupants in groups.items():
        if len(occupants) < 2:
            continue
        parsed = [(position, ref, _instant(occupancy['start']), _instant(occupancy['end']))
                  for position, ref, occupancy in occupants]
        for i, (left_position, left_ref, left_start, left_end) in enumerate(parsed):
            for right_position, right_ref, right_start, right_end in parsed[i + 1:]:
                if left_start < right_end and right_start < left_end:
                    conflicts.append((left_position, right_position, {
                        'resource': resource, 'left': left_ref, 'right': right_ref,
                        'start': max(left_start, right_start).isoformat(),
                        'end': min(left_end, right_end).isoformat(),
                    }))
    # Keep the original flat left/right pair order across interleaved resources.
    conflicts.sort(key=lambda conflict: conflict[:2])
    return tuple(conflict for _, _, conflict in conflicts)


def selected_portions(record, day, now):
    """Approved snapshots never include a later quantity or newly ready identity."""
    ready={p['id']:p for p in ready_portions(record,now)}
    result=[]
    for selection in record.data.get('planning',{}).get('portion_selections',[]):
        if selection['day']==day:
            for approved in selection['portions']:
                if approved['id'] in ready:
                    current=ready[approved['id']]
                    result.append({**current,'selected_quantity':min(approved['quantity'],current['remaining_quantity'])})
    return tuple(result)


def day_effort(records, day, *, local_zone=None):
    """Explicit timeboxes, held unassigned time and shared travel/setup, counted once."""
    result={'session_minutes':0,'held_minutes':0,'shared_minutes':0}
    shared={}
    holds=set()
    for record in records:
        spec=record.data.get('planning',{})
        result['session_minutes']+=sum(s['minutes'] for s in spec.get('work_sessions',[]) if s['day']==day)
        for hold in spec.get('held_time',[]):
            key=(hold['start'],hold['end'])
            if key in holds:
                continue
            holds.add(key)
            start,end=_instant(hold['start']),_instant(hold['end'])
            zone=local_zone or start.tzinfo
            midnight=datetime.combine(date.fromisoformat(day),time(),tzinfo=zone).astimezone(UTC)
            next_midnight=datetime.combine(date.fromisoformat(day)+timedelta(days=1),time(),tzinfo=zone).astimezone(UTC)
            overlap=(min(end.astimezone(UTC),next_midnight)-max(start.astimezone(UTC),midnight)).total_seconds()/60
            result['held_minutes']+=max(0,int(overlap))
        for block in spec.get('shared_blocks',[]):
            if block['id'] in shared and shared[block['id']] != block:
                raise ValueError('Conflicting definitions for a shared travel/setup block')
            shared[block['id']]=block
    result['shared_minutes']=sum(block['minutes'] for block in shared.values())
    return result


def in_work_window(record, start, end):
    windows=record.data.get('planning',{}).get('windows',[])
    return not windows or any(_instant(w['start']) <= start and end <= _instant(w['end']) for w in windows)
