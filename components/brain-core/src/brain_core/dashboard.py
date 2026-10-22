"""Pure dashboard projection over the canonical work reader. No storage or IO.

Columns, ranks, next steps and progress are decided here; browsers only render.
The supplied entries are already authorized by the host. No forecast is a fact
about completion, and a forecast must still agree with current work.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Iterable

from brain_core import work
from brain_core.orientation import rank
from brain_core.presentation import day_words, stake_words
from brain_core.review import ItemRecord, needs_review_length
from brain_core.schedule import SPLIT_TASK_NOTICE, eligible_at, needs_split, split_notice_required
from brain_core.timewords import SINGAPORE, personal_today

COLUMNS = (("today", "Today"), ("now", "Can do now"), ("waiting", "Waiting"),
           ("parked", "Parked"), ("review", "For your review"))


def project_work(entries: Iterable[ItemRecord], now: datetime, *, picked_today=frozenset(),
                 focus_projects=frozenset(), history=(), ranking="points", today=None,
                 prepared_index=None, detail_refs=None, include_ranking=True) -> dict[str, Any]:
    """One fresh, deterministic set of cards and full details, without forecasts."""
    if now.tzinfo is None:
        raise ValueError("dashboard requires an aware clock")
    entries = tuple(entries)
    index = work.index_things(entry.record for entry in entries) if prepared_index is None else prepared_index
    raw = {entry.record.id: entry for entry in entries}
    today = personal_today(now) if today is None else today
    ranked = rank(entries, today, picked_today=picked_today, focus_projects=focus_projects,
                  now=now, ranking=ranking, prepared_index=index) if include_ranking else ()
    order = {candidate.ref: pos for pos, candidate in enumerate(ranked)}
    reasons = {candidate.ref: candidate.rules for candidate in ranked}

    def descendants(ref):
        children = sorted((key for key, r in index.items() if r.data.get("parent") == ref),
                          key=lambda key: (index[key].data.get("order", 0), key))
        return [child for key in children for child in (key, *descendants(key))]

    def is_today(ref):
        planned = work.dates_of(ref, index).get("planned") or {}
        first = planned.get("day") or planned.get("start")
        last = planned.get("end") or first
        return ref in picked_today or bool(first and first <= today.isoformat() <= last)

    details = {}
    selected = set(index) if detail_refs is None else set(detail_refs).intersection(index)
    if detail_refs is not None:
        for ref in tuple(selected):
            selected.update(descendants(ref))
    for ref, record in index.items():
        if ref not in selected:
            continue
        status = work.explain(ref, index, now)
        next_item = work.next_of(ref, index, now)
        dates = work.dates_of(ref, index)
        progress = work.progress_of(ref, index)
        needs = work.needs_nic(ref, index, now, history)
        own_today = is_today(ref)
        family = descendants(ref)
        if status["status"] == "parked":
            column = "parked"
        elif (record.data.get("level") != "accepted" or record.data.get("review_marks")
              or any(n["kind"] in {"needs_you", "nothing_next"} for n in needs)):
            column = "review"
        elif status["status"] == "waiting":
            column = "waiting"
        elif own_today or any(is_today(key) for key in family):
            column = "today"
        else:
            column = "now"
        plan = dates.get("planned") or {}
        plan_day = plan.get("day") or plan.get("start")
        facts = []
        if plan_day:
            facts.append("For " + day_words(plan_day))
        if status["status"] in {"waiting", "parked"}:
            facts.append(status["reason"])
        timing = record.data.get("timing") or {}
        length = work.length_of(ref, index)
        if length:
            facts.append(f"{length} min")
        deadline = dates.get("deadline") or {}
        due_day = deadline.get("day") or deadline.get("end")
        if due_day:
            facts.append("Due " + day_words(due_day) + (f" at {deadline['at']}" if deadline.get("at") else ""))
        parent = index.get(record.data.get("parent"))
        stake = record.data.get("stake") or {}
        stake_text = (stake.get("at_stake") or stake_words(stake.get("level", ""))) if stake.get("confirmed") else ""
        source = raw[record.id]
        details[ref] = dict(
            ref=ref, record_id=record.id, record_sha256=source.sha256, path=source.path,
            title=record.title, state=record.state, kind=work.kind_of(ref, index),
            column=column, column_label=dict(COLUMNS)[column], status=status["status"],
            next=next_item, progress=progress, reasons=list(reasons.get(next_item["item_ref"], ())),
            labels=list(record.data.get("labels") or ()), facts=facts, dates=dates,
            timing=dict(timing), length_minutes=length, stake=stake_text,
            parent={"ref": work.first_id(parent), "title": parent.title} if parent else None,
            notes=record.body, steps=[], for_today=own_today,
            needs_length=needs_review_length(record, index, now),
            due="today" if due_day == today.isoformat() else "tomorrow" if due_day == (today + timedelta(days=1)).isoformat() else None,
            schedule=[], schedule_label="", late=False,
        )
    for ref, card in details.items():
        card["steps"] = [details[key] for key in sorted(index,
                          key=lambda key: (index[key].data.get("order", 0), key))
                         if index[key].data.get("parent") == ref]
    # Every non-ended top-level thing appears once. Nested steps live in details;
    # their rank determines the containing card's rank without a second ranker.
    cards = [card for ref, card in details.items()
             if index[ref].data.get("parent") not in index and card["status"] != "ended"]
    cards.sort(key=lambda card: (min((order.get(key, len(order)) for key in
                                    [card["ref"], *descendants(card["ref"])]), default=len(order)),
                                index[card["ref"]].updated_at, card["ref"]))
    deadline_cards = [card for ref, card in details.items()
                      if work.dates_of(ref, index, own=True).get("deadline")
                      and card["status"] not in {"ended", "parked"}]
    deadline_cards.sort(key=lambda card: (order.get(card["ref"], len(order)), card["ref"]))
    return dict(index=index, details=details, cards=cards, today=today,
                due_today=[card for card in deadline_cards if card["due"] == "today"],
                due_tomorrow=[card for card in deadline_cards if card["due"] == "tomorrow"],
                ranked_refs=[ref for ref in order if eligible_at(ref, index, now)],
                columns=[dict(key=key, label=label, items=[card for card in cards if card["column"] == key])
                         for key, label in COLUMNS])


def attach_schedule(projection, blocks, now: datetime, *, schedule_mode=True):
    """Join forecasts to fresh records; stale/deleted/private/blocked tasks vanish.

    ScheduleBlock is a structural input: item_ref/start/end/completed fields.
    The host decides whether the snapshot is fresh before passing any blocks.
    """
    index, details, today = projection["index"], projection["details"], projection["today"]
    accepted = []
    for block in sorted(blocks, key=lambda block: (block.start, block.end, block.item_ref)):
        if (block.completed or block.part_completed or block.end <= block.start
                or block.item_ref not in index
                or not (index[block.item_ref].data.get("timing") or {}).get("duration_minutes")
                or needs_split(index[block.item_ref])
                or not eligible_at(block.item_ref, index, block.start, forecast=block.start > now)):
            continue
        card = details[block.item_ref]
        start, end = block.start.astimezone(SINGAPORE), block.end.astimezone(SINGAPORE)
        deadline = card["dates"].get("deadline") or {}
        due_day = deadline.get("day") or deadline.get("end")
        due = (datetime.combine(datetime.fromisoformat(due_day).date(),
                               time.fromisoformat(deadline.get("at") or "23:59:59"), SINGAPORE)
               if due_day else None)
        late = bool(due and end > due)
        day = "" if start.date() == today else "tomorrow " if start.date() == today + timedelta(days=1) else day_words(start.date()) + " "
        label = f"Scheduled {day}{start:%H:%M}" + (", after it's due" if late else f" · {int((end-start).total_seconds() // 60)} min")
        kind = (index[block.item_ref].data.get("timing") or {}).get("block", "admin")
        slot = dict(ref=block.item_ref, title=card["title"], parent=card["parent"],
                    start=start.isoformat(), end=end.isoformat(), kind=kind,
                    type="task", late=late, label=label)
        card["schedule"].append(slot)
        card["late"] = card["late"] or late
        accepted.append(slot)
    for card in details.values():
        # A container shows its next step's actual time, not an invented time of its own.
        schedule = card["schedule"] or details.get(card["next"]["item_ref"], {}).get("schedule", [])
        target = card["next"]["item_ref"] if card["steps"] else card["ref"]
        split = bool(schedule_mode and target in index
                     and card["status"] not in {"ended", "parked"}
                     and split_notice_required(index[target], index))
        card["schedule_issue"] = "needs_split" if split else None
        card["schedule_label"] = SPLIT_TASK_NOTICE if split else schedule[0]["label"] if schedule else (
            "Not scheduled yet · needs a length" if card["needs_length"] else "")
        if schedule:
            card["late"] = any(slot["late"] for slot in schedule)
    return accepted
