"""Pure work-model migration plans over a pinned local record tree (plan W18).

Planners never write, publish, archive, fetch credentials, or call a model.
Hosts publish complete things in batches of at most 25 records, followed by
one explicit archive operation. History and archive bytes remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Mapping

from brain_core.records import WORK_FIELDS, Record, first_id, parse_record, record_from_mapping
from brain_core.language import check_change
from brain_core import work, timewords


@dataclass(frozen=True, slots=True)
class WholeThing:
    """An indivisible set of new versions/parts naming lasting first IDs."""

    first_ids: tuple[str, ...]
    records: tuple[Record, ...]


@dataclass(frozen=True, slots=True)
class MoveBatch:
    """Whole things to pass together to W2's public publish_records seam."""

    things: tuple[WholeThing, ...]
    records: tuple[Record, ...]


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """Exact named source/destination and byte digest; never a deletion."""

    record_id: str
    current_path: str
    archive_path: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class MoveRow:
    """Before/after and the one reader's answers for an audited/general thing."""

    first_id: str
    before: Record
    after: Record
    group: str
    reader: Mapping[str, Any]
    questions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MovePlan:
    """Deterministic writes, archive paths, questions and projected reader tree.

    The host supplies the pinned Git head; it is not inferred from file paths.
    `records` includes history and planned versions, excludes whole archives.
    `borderline` and `stake_calls` retain unanswered questions without guessing.
    """

    direction: str
    now: datetime
    batches: tuple[MoveBatch, ...]
    archive: tuple[ArchiveEntry, ...]
    rows: tuple[MoveRow, ...]
    records: tuple[Record, ...]
    borderline: tuple[Mapping[str, Any], ...]
    stake_calls: tuple[Mapping[str, Any], ...]


def plan_move_over(
    root: str | Path,
    rules: Mapping[str, Any],
    answers: Mapping[str, Any],
    now: datetime,
) -> MovePlan:
    """Plan 9.2/9.3 conversions and 9.4 whole archives without writing.

    Rules are wheel-shipped data citing source words. Answers preserve verbatim
    replies; only explicit yes confirms a stake or archives a borderline thing.
    IDs/paths depend on the input tree and explicit `now`, so resume is stable.
    Closed things, routines and ideas remain byte-for-byte untouched.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    originals, paths = _read(root)
    raw = _current(originals)
    finish_lines = _finish_lines(answers, raw)
    mapped = work.write_index(originals)
    aliases = {ref: first_id(r) for r in originals for ref in (r.id, *r.supersedes)}
    entries = {aliases.get(row["id"], row["id"]): row for row in rules["rows"]}
    borderline = tuple(dict(row, answer=_answer(answers, "archive", row["id"])) for row in rules["borderline"])
    archive_ids = set(rules["test_data_ids"])
    for row in borderline:
        if row["answer"].get("yes") is True:
            archive_ids.add(row["id"])
    archive_keys = {aliases.get(ref, ref) for ref in archive_ids}
    archive = _archives(root, originals, paths, lambda r: first_id(r) in archive_keys)
    calls = []
    changes: dict[str, Record] = {}
    officer_parts: dict[str, Record] = {}
    pending = []
    for key, old in raw.items():
        if key in archive_keys or old.state in {"done", "dropped", "no_longer_needed"} or old.data.get("repeat") or old.data.get("level") == "possible" or WORK_FIELDS.intersection(old.data):
            if key in finish_lines:
                raise ValueError(f"Finish line answer is not for a thing being moved: {key}")
            continue
        rule = entries.get(key, {})
        if rule.get("unchanged"):
            if key in finish_lines:
                raise ValueError(f"Finish line answer is not for a thing being moved: {key}")
            pending.append((key, old, old, "audited", tuple(rule.get("questions", ()))))
            continue
        value = deepcopy(mapped[key].as_dict())
        data = value["data"]
        unmapped_timing = [field for field in (data.get("timing") or {})
                           if field in {"due", "at", "window", "on_event", "on_return", "start_by", "resolved_from"}]
        if unmapped_timing:
            fields = ", ".join(f"data.timing.{field}" for field in unmapped_timing)
            raise ValueError(f"Cannot migrate unmapped legacy timing fields {fields}: {old.id}; keep the old shape unchanged")
        data.pop("first_step", None)
        data.pop("next_action", None)
        data.pop("importance", None)
        data["review_marks"] = [m for m in data.get("review_marks", ()) if m.get("part") != "next_action"]
        if not data["review_marks"]:
            data.pop("review_marks")
        data["size"] = "task"
        if rule:
            for field in ("called", "parent", "after"):
                data.pop(field, None)
            data.update(deepcopy(rule.get("fields", {})))
            if rule.get("title"):
                quote = rule["title_quote"]
                if quote not in rule["quoted_message"] or rule["title"].casefold() != quote.casefold():
                    raise ValueError("Migration title must be the cited exact phrase")
                value["title"] = rule["title"]
                value.setdefault("evidence", []).append(dict(
                    source_type="repository_quote", locator=rule["title_source"],
                    content_sha256=hashlib.sha256(rule["quoted_message"].encode()).hexdigest(),
                    excerpt=rule["quoted_message"]))
        value["links"] = [dict(link, target=aliases.get(link["target"], link["target"])) for link in value.get("links", ()) if link["type"] not in {"part_of", "blocked_by", "waiting_on"}]
        timing = old.data.get("timing") or {}
        data["timing"] = {k: v for k, v in timing.items() if k in {"duration_minutes", "padded_minutes", "estimated_by"} and not rule.get("remove_length")}
        if rule.get("no_dates"):
            data.pop("dates", None)
        elif timing.get("due") and "expected" not in data and not data.get("needs"):
            dates = data.setdefault("dates", {})
            due_kind = "appointment" if timing.get("at") else "deadline"
            if due_kind not in dates:
                raise ValueError(f"Missing mapped timing.due entry data.dates.{due_kind}: {old.id}")
            date_value = deepcopy(dates[due_kind])
            phrase = (timing.get("resolved_from") or {}).get("words")
            if not phrase:
                raise ValueError(f"Missing stored date words: {old.id}")
            date_value.update(words=phrase, day=timing["due"])
            kind = rule.get("date_kind")
            if kind is None:
                reading = timewords.read_when(phrase, today=datetime.fromisoformat(date_value["read_on"]).date())
                kind = reading.kind if reading else None
                if kind not in {"planned", "deadline", "appointment", "not_before"}:
                    raise ValueError(f"Unresolved date kind: {old.id}")
            if rule.get("event"):
                date_value["event"] = rule["event"]
            if kind != due_kind and kind in dates and dates[kind] != date_value:
                raise ValueError(f"Conflicting mapped data.dates.{kind} while re-kinding timing.due: {old.id}")
            if kind != "deadline" and "start_by" in dates:
                raise ValueError(f"Cannot re-kind timing.due to {kind} with data.timing.start_by but no deadline: {old.id}")
            dates.pop(due_kind)
            dates[kind] = date_value
            if rule.get("planned_on_event"):
                if kind != "not_before" or not date_value.get("day") or not date_value.get("event"):
                    raise ValueError(f"Event plan needs a dated event wait: {old.id}")
                dates["planned"] = deepcopy(date_value)
        if value["state"] == "blocked":
            if not data.get("after") and not rule:
                raise ValueError(f"Blocked record has no recorded predecessor: {old.id}")
            value["state"] = "open"
        questions = tuple(rule.get("questions", ()))
        if not rule and old.data.get("size") == "goal":
            questions += ("Is this a goal or project? Its name?",)
            data.pop("called", None)
        if value["state"] == "waiting" and not data.get("needs") and not data.get("whose"):
            raise ValueError(f"Waiting record has no recorded person: {old.id}")
        finish = finish_lines.get(key)
        if finish:
            data["finish_line"] = finish["text"]
            questions = tuple(q for q in questions if "finish line" not in q.casefold())
            data["review_marks"] = [m for m in data.get("review_marks", ())
                                    if not (m["mark"] == "question" and "finish line" in m.get("text", "").casefold())]
            if not data["review_marks"]:
                data.pop("review_marks")
        for question in questions:
            data.setdefault("review_marks", []).append({"mark": "question", "text": question})
        owner_words = rule.get("owner_words")
        locator = rule.get("source", "work-model plan 9.2")
        if rule.get("started"):
            data["started_at"] = rule["started_at"]
        if rule.get("state"):
            value["state"] = rule["state"]
        if rule.get("stake"):
            reply = _answer(answers, "stakes", key)
            calls.append(dict(first_id=key, proposal=rule["stake"], answer=reply))
            if reply.get("yes") is True and not isinstance(reply.get("words"), str):
                raise ValueError("Confirmed stake requires verbatim reply words")
            stake = dict(rule["stake"], confirmed=reply.get("yes") is True, proposed_by="system:work-move-over")
            data["stake"] = stake
            if stake["confirmed"]:
                owner_words = reply["words"]
                locator = "answers:stakes:" + key
        _source(value, "system:work-move-over", owner_words, locator)
        if finish:
            _source(value, "system:work-move-over", finish["words"], "answers:finish_lines:" + key)
        new = _version(value, old, now, "over")
        check_change(old, new, actor="system:work-move-over", move_over=True)
        changes[key] = new
        pending.append((key, old, new, "audited" if rule else "moved by the general rules", questions))
        if rule.get("officer"):
            words = rule["officer"]["words"]
            if words not in old.source["words"]:
                raise ValueError("Officer quote absent from cited source")
            child = deepcopy(old.as_dict())
            source_day = timewords.personal_today(old.updated_at).isoformat()
            child.update(title=rule["officer"].get("title", "Contact coordinator"), state="done", supersedes=[], links=[], dedupe_key=None,
                         data=dict(level="accepted", size="task", parent=key, order=1,
                                   dates=dict(done=dict(words=words, read_by="code", read_on=source_day, day=source_day)),
                                   check=dict(locator=rule["source"], checked_by="owner"), reason=words),
                         body=words + "\nActual tick time unknown; version timestamps are migration timestamps."
                              "\nDone day comes from source words and time.")
            _source(child, "system:work-move-over", words, rule["source"])
            part = _version(child, None, now, "officer:" + key)
            check_change(None, part, actor="system:work-move-over")
            officer_parts[key] = part
    projected = tuple(r for r in originals if first_id(r) not in archive_keys) + tuple(changes.values()) + tuple(officer_parts.values())
    index = work.index_things(projected)
    rows = tuple(MoveRow(key, old, new, group, _reader(key, index, now), questions) for key, old, new, group, questions in pending)
    batches = _batches(changes, officer_parts)
    return MovePlan("over", now, batches, archive, rows, projected, borderline, tuple(calls))


def plan_move_back(root: str | Path) -> MovePlan:
    """Plan 10.6's mechanical inverse for every current new-shape thing.

    Copy source/evidence with system:work-move-back, validate through the frozen
    parser, and name current plus historical new-shape paths for W2's checked
    archive seam. Routines are never converted. The deterministic plan clock is
    derived from the input records; inverse writes do not call check_change.
    """
    originals, paths = _read(root)
    raw = _current(originals)
    now = max((r.updated_at for r in originals), default=datetime(1970, 1, 1, tzinfo=UTC)) + timedelta(seconds=1)
    changes = {}
    for key, old in raw.items():
        if not WORK_FIELDS.intersection(old.data):
            continue
        if old.data.get("repeat"):
            raise ValueError("Routine unexpectedly has new-shape fields")
        value = deepcopy(old.as_dict())
        data = value["data"]
        facts = deepcopy(data)
        for field in WORK_FIELDS:
            data.pop(field, None)
        data["size"] = "goal" if facts.get("called") else "task"
        links = value.setdefault("links", [])
        if facts.get("parent"):
            links.append(dict(type="part_of", target=facts["parent"]))
        for target in facts.get("after", ()):
            links.append(dict(type="blocked_by", target=target))
        if old.state == "open" and any(raw.get(target) and raw[target].state not in {"done", "dropped", "no_longer_needed"} for target in facts.get("after", ())):
            value["state"] = "blocked"
        person = facts.get("whose") or (facts.get("needs") or {}).get("from")
        if person:
            links.append(dict(type="waiting_on", target=person))
            if old.state == "open":
                value["state"] = "waiting"
        dates = facts.get("dates") or {}
        timing = data.get("timing") or {}
        chosen = facts.get("expected") or (facts.get("needs") or {}).get("expected") or dates.get("deadline") or dates.get("appointment") or dates.get("planned")
        if chosen:
            if chosen.get("day"):
                timing["due"] = chosen["day"]
            elif chosen.get("start") and chosen.get("end"):
                timing["window"] = dict(start=chosen["start"], end=chosen["end"])
            if chosen.get("at"):
                timing["at"] = chosen["at"]
            timing["resolved_from"] = dict(today=chosen["read_on"], words=chosen["words"])
        if dates.get("start_by"):
            start = dates["start_by"]
            timing["start_by"] = start["day"]
            timing.setdefault("resolved_from", dict(today=start["read_on"], words=start["words"]))
        data["timing"] = timing or None
        action = facts.get("next_action")
        if action and not action["done"]:
            data["first_step"] = action["text"]
        value["source"]["recorded_by"] = "system:work-move-back"
        changes[key] = _version(value, old, now, "back")
    named = {ref for r in raw.values() for ref in (r.id, *r.supersedes)}
    archive = _archives(root, originals, paths, lambda r: r.id in named and bool(WORK_FIELDS.intersection(r.data)))
    archived = {entry.record_id for entry in archive}
    projected = tuple(r for r in originals if r.id not in archived) + tuple(changes.values())
    return MovePlan("back", now, _batches(changes, {}), archive, (), projected, (), ())


def _read(root):
    records, paths = [], {}
    base = Path(root)
    for path in sorted((base / "records" / "item").rglob("*.md")):
        r = parse_record(path.read_text(encoding="utf-8"), str(path))
        if r.id in paths:
            raise ValueError("Duplicate record ID")
        records.append(r)
        paths[r.id] = path.relative_to(base).as_posix()
    return tuple(records), paths


def _current(records):
    replaced = {ref for r in records for ref in r.supersedes}
    current = {}
    for r in records:
        if r.id not in replaced:
            key = first_id(r)
            if key in current:
                raise ValueError("Forked record history")
            current[key] = r
    return dict(sorted(current.items()))


def _answer(answers, category, key):
    value = answers.get(category, {}).get(key, {})
    if not isinstance(value, Mapping) or ("yes" in value and not isinstance(value["yes"], bool)):
        raise ValueError("Answers require {yes: boolean, words: verbatim reply}")
    return dict(value)


def _finish_lines(answers, current):
    replies = answers.get("finish_lines", {})
    if not isinstance(replies, Mapping):
        raise ValueError("Answers finish_lines must be a map keyed by first ID")
    result = {}
    for key, reply in replies.items():
        if key not in current:
            raise ValueError(f"Unknown finish line first ID: {key}")
        if not isinstance(reply, Mapping):
            raise ValueError(f"Finish line answer requires text and verbatim words: {key}")
        text, words = reply.get("text"), reply.get("words")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 256:
            raise ValueError(f"Finish line text must be 1 to 256 characters: {key}")
        text = text.strip()
        if not isinstance(words, str) or text not in words:
            raise ValueError(f"Finish line text must occur verbatim in reply words: {key}")
        result[key] = dict(text=text, words=words)
    return result


def _source(value, actor, words, locator):
    if words is None:
        old_words = value["source"].get("words", "")
        date_words = [d["words"] for d in (value["data"].get("dates") or {}).values()]
        words = "Migration of stored facts: " + " ".join([old_words, *date_words])
        value["source"] = dict(recorded_by=actor, said_by="agent", speaker=actor, came_by="system", words=words)
    else:
        value["source"] = dict(recorded_by=actor, said_by="owner", came_by="direct", words=words)
    value.setdefault("evidence", []).append(dict(source_type="work_migration", locator=locator,
                                  content_sha256=hashlib.sha256(words.encode()).hexdigest(), excerpt=words))


def _version(value, old, now, salt):
    stamp = now.astimezone(UTC)
    seed = f"system:work-move-{salt}:{old.id if old else value['title']}:{stamp.isoformat()}"
    value.update(id=f"brn_{stamp:%Y%m%d_%H%M%S}_{hashlib.sha256(seed.encode()).hexdigest()[:16]}",
                 created_at=stamp.isoformat(), updated_at=stamp.isoformat(),
                 supersedes=[*old.supersedes, old.id] if old else [])
    return record_from_mapping(value, "workmove")


def _archives(root, records, paths, predicate):
    return tuple(ArchiveEntry(r.id, paths[r.id], "archive/" + paths[r.id],
                              hashlib.sha256((Path(root) / paths[r.id]).read_bytes()).hexdigest())
                 for r in records if predicate(r))


def _batches(changes, parts):
    # Connected structural components are whole things; no parent/child split.
    groups = [{key} for key in changes]
    for key, r in changes.items():
        parent = r.data.get("parent") or next((link["target"] for link in r.links if link["type"] == "part_of"), None)
        if parent in changes:
            left = next(g for g in groups if key in g)
            right = next(g for g in groups if parent in g)
            if left is not right:
                left.update(right)
                groups.remove(right)
    things = []
    for group in sorted(groups, key=lambda g: min(g)):
        rows = tuple(changes[key] for key in sorted(group)) + tuple(parts[key] for key in sorted(group) if key in parts)
        if len(rows) > 25:
            raise ValueError("A whole thing exceeds the 25-record publish limit")
        things.append(WholeThing(tuple(sorted(group)), rows))
    batches, held = [], []
    for thing in things:
        if sum(len(t.records) for t in held) + len(thing.records) > 25:
            batches.append(MoveBatch(tuple(held), tuple(r for t in held for r in t.records)))
            held = []
        held.append(thing)
    if held:
        batches.append(MoveBatch(tuple(held), tuple(r for t in held for r in t.records)))
    return tuple(batches)


def _reader(key, index, now):
    return dict(kind=work.kind_of(key, index), status=work.status_of(key, index, now),
                next=work.next_of(key, index, now), progress=work.progress_of(key, index),
                needs=work.needs_nic(key, index, now))
