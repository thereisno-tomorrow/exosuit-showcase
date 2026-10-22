"""Fresh contextual choice catalogs; option values are bound in card tokens."""
from brain_core import work
from brain_core.planning import ready_portions, required_followup, active_followup_ref

CLOSED = {'done', 'dropped', 'no_longer_needed'}
PICKER_ACTIONS = {'remove_plan', 'duplicate_choose', 'inspect', 'correction', 'under', 'requirement', 'timebox', 'planning_operation', 'planning_shared', 'portion_select', 'portion_result'}


def fields_for(action, source_ref):
    if action == 'return_change':
        return ['source']
    if action == 'return_replacement':
        return ['to_ref']
    if action in {'inspect', 'duplicate_choose', 'remove_plan'}:
        return ['item']
    if action in {'correction', 'timebox'}:
        return ['item']
    if action == 'under':
        return ['value']
    if action == 'requirement':
        return ([] if source_ref else ['item']) + ['remove', 'add']
    if action == 'planning_operation':
        return ['followup']
    if action == 'planning_shared':
        return ['tasks']
    if action in {'portion_select', 'portion_result'}:
        return ([] if source_ref else ['item']) + ['ids' if action == 'portion_select' else 'id']
    return []


def proof(card):
    return {'record_id': card['record_id'], 'sha256': card['record_sha256']}


def catalog(action, field, view, selected, source_ref):
    details = view.get('details', {})
    ref = selected.get('item') or source_ref
    if field in {'ids', 'id'}:
        portions = details.get(ref, {}).get('planning', {}).get('portions', ())
        if field == 'ids':
            record = view.get('index', {}).get(ref)
            portions = ready_portions(record, view['selection_now']) if record and view.get('selection_now') else view.get('planning', {}).get('ready_portions', {}).get(ref, ())
        return [{'ref': p['id'], 'label': p['id'], 'description': str(p.get('remaining_quantity', p['quantity'])) + ' remaining', 'proof': None} for p in portions]
    cards = list(details.values())
    if action == 'duplicate_choose':
        cards = [card for card in cards if card['ref'] in selected.get('_duplicate_refs', ())]
    if action == 'return_change':
        cards = [card for card in cards if card.get('planning', {}).get('operation', {}).get('actual_start')
                 and (followup := required_followup(view['index'][card['ref']], view['index']))
                 and (not view['index'].get(followup['item_ref']) or view['index'][followup['item_ref']].state != 'done')]
    elif action == 'return_replacement':
        source = view['index'][source_ref]
        planning = source.data.get('planning', {})
        excluded = {source_ref, planning['operation']['followup_ref']}
        excluded.update(entry.get('to_ref') for entry in planning.get('followup_dispositions', ()))
        bindings = {active_followup_ref(record) for ref,record in view['index'].items() if ref != source_ref}
        cards = [card for card in cards if card['ref'] not in excluded | bindings
                 and card.get('state') not in CLOSED and card.get('is_up')
                 and not card.get('prerequisites_unfinished') and not card.get('material_kind')
                 and (view['index'][card['ref']].data.get('timing', {}).get('duration_minutes') or 0) > 0]
    elif field == 'remove':
        record = view.get('index', {}).get(ref)
        requirements = record.data.get('after', ()) if record else details.get(ref, {}).get('after', ())
        cards = [card for card in cards if card['ref'] in requirements]
    elif action == 'correction':
        cards = [card for card in cards if card.get('state') == 'done']
    else:
        cards = [card for card in cards if card.get('state') not in CLOSED]
    if action == 'remove_plan':
        cards = [card for card in cards if card.get('state') == 'open' and card.get('dates', {}).get('planned')]
    if action == 'under':
        cards = [card for card in cards if card['ref'] != source_ref and (work.kind_of(card['ref'], view['index']) == 'project' if 'index' in view else card.get('kind') == 'project')]
    elif field in {'followup', 'tasks'} or action in {'inspect', 'timebox'}:
        cards = [card for card in cards if not card.get('prerequisites_unfinished') and (card.get('is_up', True) if action == 'timebox' else True)]
        if field == 'followup':
            cards = [card for card in cards if card['ref'] != source_ref and card.get('material_kind') not in {'information', 'reflection', 'idea', 'arrangement'}]
    elif field == 'add':
        cards = [card for card in cards if card['ref'] not in {ref, selected.get('remove')}]
    if field == 'item' and action in {'portion_select', 'portion_result'}:
        cards = [card for card in cards if card.get('planning', {}).get('portions') and (has_ready(card['ref'], view) if action == 'portion_select' else True)]
    if field == 'item' and action == 'requirement':
        cards = [card for card in cards if (view.get('index', {}).get(card['ref']).data.get('after') if view.get('index', {}).get(card['ref']) else card.get('after'))]
    cards.sort(key=lambda card: (card['title'].casefold(), card['ref']))
    result = []
    for number, card in enumerate(cards, 1):
        parent = (card.get('parent') or {}).get('title') or 'No project'
        result.append({'ref': card['ref'], 'label': f'{number}. ' + card['title'], 'description': parent + ' \u00b7 ' + card.get('state', card.get('status', 'open')), 'proof': proof(card)})
    return result


def page_for(state, view):
    picker = state['selector']
    field = picker['fields'][picker['field_index']]
    items = catalog(picker['action'], field, view, {**picker['selected'], '_duplicate_refs': picker.get('duplicate_refs', ())}, picker.get('source_ref'))
    offset = min(picker.get('offset', 0), max(0, ((len(items) - 1) // 25) * 25))
    picker['offset'] = offset
    page = items[offset:offset + 25]
    return field, items, page


def wait_context(card, index):
    record = index.get(card.get('ref')) if index else None
    data = record.data if record else card
    needs = data.get('needs') or {}
    if isinstance(needs, str):
        needs = {'what': needs}
    expected = needs.get('expected') or data.get('expected') or {}
    return {'who': needs.get('from') or data.get('whose') or '', 'what': needs.get('what') or '',
            'expected': expected.get('day', '') if isinstance(expected, dict) else str(expected)}


def has_ready(ref, view):
    record = view.get('index', {}).get(ref)
    return bool(ready_portions(record, view['selection_now'])) if record and view.get('selection_now') else bool(view.get('planning', {}).get('ready_portions', {}).get(ref))
