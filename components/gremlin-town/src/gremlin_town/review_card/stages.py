"""Typed card queues over current canonical records and authorized question refs."""
from datetime import timedelta
from brain_core import work, routines

INBOX = {"new_schedule", "new_nothing_needed", "two_minutes", "needs_length"}
ATTENTION = {"missed", "to_chase", "nothing_next", "did_it_happen", "carried_over", "carried_over_3_times"}


def stage_queues(index, parts, log, day):
    yesterday = day - timedelta(days=1)
    picks = {row.item_ref for row in log.history() if row.response == "picked_today"
             and getattr(row, "fields_used", {}).get("review_day") == yesterday.isoformat()}
    # Legacy pick rows retain their original six-hour fallback only in WhyLog.
    for signal in (signal for offset in (-1, 0, 1) for signal in log.signals_on("day_settled", yesterday + timedelta(days=offset))):
        import json
        value = json.loads(signal.detail or "null")
        if isinstance(value, dict) and value.get("review_day") == yesterday.isoformat():
            picks.update(value.get("picks", ()))
    occurrences = {(work.first_id(version), version.data.get("occurrence", {}).get("day"))
                   for version in getattr(index, "_versions", {}).values()}
    queues = {"yesterday": [], "inbox": [], "attention": []}
    committed = set()
    for ref, record in index.items():
        if record.data.get("material_kind") in {"information", "reflection", "idea"} or record.data.get("material_kind") == "arrangement" and not record.data.get("dates", {}).get("appointment") or record.state in {"done", "dropped", "no_longer_needed"} or work.has_unfinished_prerequisites(ref, index):
            continue
        dates = record.data.get("dates", {})
        due = next((value for key, value in dates.items() if key in {"planned", "appointment"} and value.get("day") == yesterday.isoformat()), None)
        is_routine = routines.is_routine(record)
        occurrence = is_routine and routines.occurs_on(record, yesterday)
        known = work.has_review_outcome(ref, index, yesterday.isoformat())
        if is_routine:
            known = known or (ref, yesterday.isoformat()) in occurrences
        commitment_day = yesterday.isoformat()
        target_stage = "yesterday"
        if not (due or ref in picks or occurrence):
            older = next((value.get("day") for key,value in dates.items() if key in {"planned", "appointment"} and value.get("day", day.isoformat()) < yesterday.isoformat()), None)
            if older:
                commitment_day, target_stage = older, "attention"
                known = work.has_review_outcome(ref, index, older)
        if (due or ref in picks or occurrence or target_stage == "attention") and not known:
            committed.add(ref)
            queues[target_stage].append(dict(reference=f"outcome:{commitment_day}:{ref}", item_ref=ref,
                kind="outcome", answered=False, offered=(), synthetic=True, commitment_day=commitment_day,
                scope="occurrence" if is_routine else "appointment" if dates.get("appointment") else work.kind_of(ref, index),
                text="Commitment for " + commitment_day))
    seen = set()
    for part in (2, 3):
        for line in parts.get(part, ()):
            ref = line.get("item_ref")
            if ref not in index or ref in committed or line.get("answered") or line["reference"] in seen:
                continue
            if index[ref].data.get("material_kind") in {"information", "reflection", "idea", "arrangement"} or work.has_unfinished_prerequisites(ref, index) or index[ref].state in {"done", "dropped", "no_longer_needed"}:
                continue
            kind = line.get("kind")
            dates = index[ref].data.get("dates", {})
            dated = dates.get("appointment" if kind == "did_it_happen" else "planned", {}).get("day")
            if kind in {"carried_over", "did_it_happen"} and dated and work.has_review_outcome(ref, index, dated):
                continue
            if kind == "stalled" and not any(line.get("suggested_" + key) for key in ("name", "title", "label")):
                continue
            stage = "inbox" if kind in INBOX or any(line.get("suggested_" + key) for key in ("name", "title", "label")) or kind not in ATTENTION else "attention"
            queues[stage].append({**line, "scope": work.kind_of(ref, index)})
            seen.add(line["reference"])
    queues["inbox"].sort(key=lambda line: not any(line.get("suggested_" + key) for key in ("name", "title", "label")))
    return queues
