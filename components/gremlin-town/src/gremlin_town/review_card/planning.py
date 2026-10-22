"""Conservative card confirmation over canonical workload and resource facts."""
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from brain_core.planning import (resource_conflicts, ready_portions, required_followup,
                                 selected_portions, day_effort, in_work_window)


def planning_summary(index, *, selected=(), day_view=None, capacity_minutes=None, now):
    day_view = day_view or {}
    day = now.date().isoformat()
    included = set(selected)
    included.update(ref for ref, record in index.items() if selected_portions(record, day, now)
                    or any(s["day"] == day for s in record.data.get("planning", {}).get("work_sessions", ())))
    included.update(ref for ref, record in index.items() if record.data.get("dates", {}).get("planned", {}).get("day") == day and record.state not in {"done", "dropped", "no_longer_needed"})
    for card in day_view.get("today", ()):
        if card.get("state") not in {"done", "dropped", "no_longer_needed"}:
            included.add(card["ref"])
    for slot in day_view.get("timeline", ()):
        if slot.get("ref") in index:
            included.add(slot["ref"])
    followups, issues, notices = [], [], []
    # An actually started operation remains an obligation even when the startup
    # task is no longer picked, planned today, or shown in the schedule.
    operation_refs = set(included)
    operation_refs.update(ref for ref, record in index.items()
                          if record.data.get("planning", {}).get("operation", {}).get("actual_start"))
    for ref in sorted(operation_refs):
        if ref not in index:
            issues.append("Couldn't establish one of the selected tasks. Refresh the review.")
            continue
        followup = required_followup(index[ref], index)
        if followup:
            target = index.get(followup["item_ref"])
            if target is not None and target.state == "done":
                continue
            followups.append(followup)
            if followup["missing"] or target is None or target.state in {"dropped", "no_longer_needed"}:
                issues.append("The required return is unavailable or closed. Restore it or explicitly resolve the obligation; it remains required.")
            elif not followup["approved"]:
                issues.append("Before starting, agree who will do the required follow-up.")
            else:
                target = index[followup["item_ref"]]
                if followup["conditional"]:
                    included.add(followup["item_ref"])
                    notices.append("The follow-up is agreed. Its time will be counted from when the work actually starts.")
                else:
                    at = datetime.fromisoformat(followup["at"]).astimezone(now.tzinfo)
                    label = f"Required: {target.title} at {at.strftime('%d %b %H:%M %z')} (from the actual start)."
                    if at.date() <= now.date():
                        included.add(followup["item_ref"])
                        placements = [slot for slot in day_view.get("timeline", ())
                                      if slot.get("ref") == followup["item_ref"]]
                        if not placements:
                            notices.append(label + " It is not yet on the schedule.")
                        elif not any(datetime.fromisoformat(slot["start"]).astimezone(timezone.utc) == at.astimezone(timezone.utc) for slot in placements):
                            issues.append(label + " The schedule shows a different time. Check it before starting.")
                        if at.astimezone(timezone.utc) < now.astimezone(timezone.utc) and not placements:
                            issues.append(f"{target.title} still needs doing, and its agreed time has passed.")
                        minutes = target.data.get("timing", {}).get("duration_minutes") or target.data.get("rough_minutes")
                        if minutes:
                            until = at.astimezone(timezone.utc) + timedelta(minutes=minutes)
                            for slot in day_view.get("timeline", ()):
                                if slot.get("ref") != followup["item_ref"] and datetime.fromisoformat(slot["start"]) < until and at < datetime.fromisoformat(slot["end"]):
                                    issues.append(f"{target.title} at {at.strftime('%H:%M')} conflicts with another scheduled commitment.")
                                    break
                    else:
                        notices.append(label + " It remains required on that local day.")
    required = 0
    personal = []
    approved_portions = {}
    for ref in included:
        if ref not in index:
            continue
        record = index[ref]
        if record.state in {"done", "dropped", "no_longer_needed"}:
            continue
        if record.data.get("whose") or record.data.get("material_kind") in {"information", "reflection", "idea"}:
            continue
        personal.append(record)
        spec = record.data.get("planning", {})
        portions = selected_portions(record, day, now)
        if spec.get("portions"):
            approved_portions[ref] = portions
            if not portions:
                notices.append("Choose which ready parts or batches you will do today.")
            else:
                notices.append("Only the chosen quantities are saved for today. Set a work session to check how much time they need.")
        placements = [slot for slot in day_view.get("timeline", ()) if slot.get("ref") == ref]
        if spec.get("windows"):
            if not placements:
                notices.append("Some work has a time window but is not yet on the schedule.")
            for slot in placements:
                if not in_work_window(record, datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])):
                    issues.append("A task is scheduled outside the time when it can happen.")
        if record.data.get("dates", {}).get("appointment"):
            continue  # Capacity is flexible time remaining after fixed commitments.
        sessions = [s for s in spec.get("work_sessions", ()) if s["day"] == day]
        if sessions:
            required += sum(s["minutes"] for s in sessions)
            continue
        if spec.get("portions"):
            continue  # A whole-task estimate is not an estimate for chosen quantities.
        minutes = record.data.get("timing", {}).get("duration_minutes") or record.data.get("rough_minutes")
        if not minutes:
            notices.append("Some work still needs a time estimate.")
        else:
            required += minutes
    # Shared travel/setup is additional effort, counted once across included work.
    shared_records = [SimpleNamespace(data={"planning": {"shared_blocks": [b for b in r.data.get("planning", {}).get("shared_blocks", ())
                         if included.intersection(b["task_refs"])]}}) for r in personal]
    required += day_effort(shared_records, day)["shared_minutes"]
    start = datetime.combine(now.date(), time(), now.tzinfo)
    end = datetime.combine(now.date() + timedelta(days=1), time(), now.tzinfo)
    # Unassigned holds count even when no focus task was selected. Compare instants
    # in UTC so a daylight-saving transition does not invent available minutes.
    holds = set()
    for record in index.values():
        for hold in record.data.get("planning", {}).get("held_time", ()):
            left, right = datetime.fromisoformat(hold["start"]), datetime.fromisoformat(hold["end"])
            key = (left.astimezone(timezone.utc), right.astimezone(timezone.utc))
            holds.add(key)
    merged = []
    for left, right in sorted(holds):
        left, right = max(left, start.astimezone(timezone.utc)), min(right, end.astimezone(timezone.utc))
        if left >= right:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    held_minutes = sum(int((right-left).total_seconds()/60) for left, right in merged)
    required += held_minutes
    if capacity_minutes is None:
        notices.append("Your available time is not set yet. Your choices can be saved, but we cannot tell whether they fit.")
    elif required > capacity_minutes:
        issues.append(f"The whole day includes about {required} minutes of work and held time in {capacity_minutes} minutes left after fixed commitments. Choose less or a shorter work session.")
    conflicts = tuple(conflict for conflict in resource_conflicts(index.values())
                      if datetime.fromisoformat(conflict["end"]) > now
                      and datetime.fromisoformat(conflict["start"]) < end)
    for conflict in conflicts:
        issues.append(f"{conflict['resource']} is booked for overlapping work. Change one booking before starting.")
    portions = {ref: ready_portions(record, now) for ref, record in index.items()
                if record.data.get("planning", {}).get("portions")}
    return dict(issues=issues, notices=list(dict.fromkeys(notices)), required_minutes=required,
                held_minutes=held_minutes, included_refs=sorted(included), selected_portions=approved_portions,
                conditional_followups=followups, ready_portions=portions,
                resource_conflicts=conflicts, provisional=bool(notices or issues))


def day_approval(view, state):
    """Full owner-readable approval intent and proofs, without owning record truth."""
    import hashlib
    import json
    from datetime import date
    from zoneinfo import ZoneInfo
    index, day = view.get('index', {}), state['day']
    local_start = datetime.combine(date.fromisoformat(day), time(), ZoneInfo(state.get('timezone', 'Asia/Singapore')))
    local_end = local_start + timedelta(days=1)
    def on_day(entry):
        return datetime.fromisoformat(entry['start']) < local_end and datetime.fromisoformat(entry['end']) > local_start
    focus = sorted(card['ref'] for card in view.get('candidates', ()) if card['picked'])
    planning = view.get('planning', {})
    included = set(planning.get('included_refs', ())) | set(focus)
    from .selectors import proof
    proofs = {ref: proof(view['details'][ref]) for ref in sorted(index)}
    lines = ["Focus choices are separate from planned dates. Removing focus does not remove a planned date."]
    counts = {'focus_tasks': len(focus), 'portion_selections': 0, 'sessions': 0, 'already_planned': 0}
    for ref, record in sorted(index.items(), key=lambda pair: (pair[1].title.casefold(), pair[0])):
        spec = record.data.get('planning', {})
        portions = planning.get('selected_portions', {}).get(ref, ())
        sessions = [s for s in spec.get('work_sessions', ()) if s['day'] == day]
        planned = record.data.get('dates', {}).get('planned', {}).get('day') == day and record.state not in {'done', 'dropped', 'no_longer_needed'}
        holds = [h for h in spec.get('held_time', ()) if on_day(h)]
        bookings = [r for r in spec.get('resources', ()) if on_day(r)]
        if ref not in included and not (sessions or portions or planned or holds or bookings):
            continue
        counts['portion_selections'] += len(portions)
        counts['sessions'] += len(sessions)
        counts['already_planned'] += bool(planned)
        categories = []
        if ref in focus:
            categories.append('Selected focus')
        if planned:
            categories.append('Already planned for ' + day + ('; remains planned without focus' if ref not in focus else ''))
        if portions:
            categories.append('Selected portions: ' + ', '.join(str(p['id']) + ' × ' + str(p.get('selected_quantity', p['quantity'])) for p in portions))
        if sessions:
            categories.append('Work sessions: ' + ', '.join(str(s['minutes']) + ' min' for s in sessions) + ' (session time, not whole-task effort)')
        minutes = record.data.get('timing', {}).get('duration_minutes') or record.data.get('rough_minutes')
        if ref in included and not portions and not sessions:
            categories.append('Whole-task estimate: ' + (str(minutes) + ' min' if minutes else 'estimate unknown'))
        if holds:
            categories.append('Held time: ' + '; '.join(h['start'] + ' to ' + h['end'] for h in holds))
        if bookings:
            categories.append('Resource bookings: ' + '; '.join(r['resource'] + ': ' + r['start'] + ' to ' + r['end'] for r in bookings))
        if spec.get('windows'):
            categories.append('Allowed windows: ' + '; '.join(w['start'] + ' to ' + w['end'] for w in spec['windows']))
        if spec.get('shared_blocks'):
            categories.append('Shared effort (counted once): ' + '; '.join(str(b['id']) + ': ' + str(b['minutes']) + ' min ' + b['kind'] for b in spec['shared_blocks']))
        body = getattr(record, 'body', '')
        lines.append(record.title + ('\n' + body if body else '') + '\n' + '\n'.join(categories))
    for followup in planning.get('conditional_followups', ()):
        target = index.get(followup['item_ref'])
        lines.append('Required return: ' + (target.title if target else 'Unavailable task') + '\n' +
                     ('Time counted from actual start' if followup.get('conditional') else 'Due: ' + datetime.fromisoformat(followup['at']).astimezone(local_start.tzinfo).strftime('%d %b %Y %H:%M %Z')) +
                     ('\nAgreed responsibility' if followup.get('approved') else '\nResponsibility needs agreement'))
    lines.extend(planning.get('issues', ()))
    lines.extend(planning.get('notices', ()))
    text = ('Focus tasks: {focus_tasks} · portion selections: {portion_selections} · work sessions: {sessions} · already planned tasks: {already_planned}'.format(**counts) +
            '\nKnown effort and held time: ' + str(planning.get('required_minutes', 0)) + ' min. Unknown estimates are excluded; this is not a confirmed schedule.\n\n' + '\n\n'.join(lines))
    result = dict(day=day, focus=focus, eligible=[card['ref'] for card in view.get('candidates', ())],
                  counts=counts, text=text, proofs=proofs, capacity_minutes=state.get('capacity_minutes'))
    result['sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return result
