"""Per-person agendas (Ciel and Brain Home PRD §4.9; build plan slice 6): pure functions, no IO.

"What do I need to raise with Sam?" lists everything to raise with Sam, Nic's promises to Sam and
what he's waiting on from Sam, each most important first by step F's ranking. It is worked out
from Brain Home each time and never kept as a list of its own (PRD §8). After the conversation,
Nic says so, and an agent closes the raised topics with his words: each is done, checked by what
he said.

A person is named the way Nic's records name them: ``raise_with``, ``owed_to``,
``whose`` or ``needs.from`` through the reader (including old ``waiting_on`` links),
compared ignoring case and extra spaces. A first name finds a full one
too ("Sam" finds "Sam Wong"), and when more than one person answers to it, the agenda lists nothing
and ``ask_which`` names each of them, for the agent to ask Nic which he means; the second call passes
that name with ``exact`` (Nic, 29 Sep 2026).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from brain_core.records import RECORD_SCHEMA_V2
from brain_core.review import ItemRecord, OpenNote, current_items
from brain_core.timewords import today_in_singapore
from brain_core.work import dates_of, first_id, index_things, status_of


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def same_person(name: str, other: object) -> bool:
    return isinstance(other, str) and _key(name) == _key(other)


def answers_to(name: str, other: object) -> bool:
    """``other`` is ``name`` itself, or ``name`` is one word and ``other``'s first name."""

    if not isinstance(other, str) or not other.split():
        return False
    asked = _key(name)
    return asked == _key(other) or (" " not in asked and _key(other).split()[0] == asked)


@dataclass(frozen=True, slots=True)
class AgendaLine:
    """One thing on a person's agenda: ``part`` is ``to_raise``, ``promised`` or ``waiting_on``."""

    part: str
    record_id: str
    title: str
    state: str
    record_sha256: str
    reasons: tuple[str, ...] = ()
    fields_used: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Agenda:
    """``person`` is the name the records use when one person answered; ``ask_which`` lists each
    name when several did, and then every part is empty."""

    person: str
    to_raise: tuple[AgendaLine, ...]
    promised: tuple[AgendaLine, ...]
    waiting_on: tuple[AgendaLine, ...]
    ask_which: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.to_raise or self.promised or self.waiting_on)


def _accepted(entry: ItemRecord) -> bool:
    record = entry.record
    return record.schema == RECORD_SCHEMA_V2 and record.kind == "item" and record.data.get("level") == "accepted"


def agenda(
    items: Iterable[ItemRecord],
    person: str,
    now: datetime,
    *,
    notes: Iterable[OpenNote] = (),
    focus_projects: frozenset[str] = frozenset(),
    ranking: str | None = None,
    exact: bool = False,
) -> Agenda:
    """``person``'s agenda through the sole work reader, newest versions only (G4).
    To raise: ``raise_with``. Promised: ``owed_to``. Waiting on: their parts
    (``whose``), ``needs.from`` and mapped legacy ``waiting_on`` links.
    Original record hashes remain pinned; effective projections are read-only.
    Each part most important first where up, with W7's ordering for waits. Unless
    ``exact``, a first name finds a full one; several people answering to it fill ``ask_which``."""

    # Imported here: orientation imports review, which this module also reads.
    from brain_core.orientation import rank

    today = today_in_singapore(now)
    everything = tuple(items)
    index = index_things(entry.record for entry in everything)
    live = [entry for entry in current_items(everything) if _accepted(entry)
            and status_of(first_id(entry.record), index, now) not in ("ended", "parked")]

    def data_of(entry: ItemRecord) -> Mapping[str, Any]:
        return index[first_id(entry.record)].data

    def waiting_people(entry: ItemRecord) -> tuple[object, ...]:
        data = data_of(entry)
        return (data.get("whose"), (data.get("needs") or {}).get("from"))
    if not exact:
        named: dict[str, str] = {}
        for entry in live:
            data = data_of(entry)
            for other in (data.get("raise_with"), data.get("owed_to"), *waiting_people(entry)):
                if answers_to(person, other):
                    named.setdefault(_key(other), " ".join(other.split()))
        if len(named) > 1:
            return Agenda(person=person.strip(), to_raise=(), promised=(), waiting_on=(),
                          ask_which=tuple(sorted(named.values(), key=str.casefold)))
        if named:
            person = next(iter(named.values()))
    to_raise = [entry for entry in live if entry.record.state == "open" and same_person(person, data_of(entry).get("raise_with"))]
    promised = [
        entry for entry in live
        if same_person(person, data_of(entry).get("owed_to"))
    ]
    waiting = [
        entry for entry in live
        if any(same_person(person, other) for other in waiting_people(entry))
    ]
    extra = {"ranking": ranking} if ranking is not None else {}
    ranked = rank(
        list({entry.record.id: entry for entry in (*to_raise, *promised, *waiting)}.values()), today,
        focus_projects=focus_projects, notes=tuple(notes), now=now, **extra,
    )
    order = {candidate.ref: index for index, candidate in enumerate(ranked)}
    reasons = {candidate.ref: candidate.rules for candidate in ranked}

    def lines(part: str, entries: list[ItemRecord]) -> tuple[AgendaLine, ...]:
        return tuple(
            AgendaLine(
                part=part,
                record_id=entry.record.id,
                title=entry.record.title,
                state=entry.record.state,
                record_sha256=entry.sha256,
                reasons=reasons.get(first_id(entry.record), ()),
                fields_used={
                    "raise_with": data_of(entry).get("raise_with"),
                    "owed_to": data_of(entry).get("owed_to"),
                    "whose": data_of(entry).get("whose"),
                    "needs": dict(data_of(entry).get("needs") or {}),
                    "dates": dates_of(first_id(entry.record), index),
                    "timing": dict(entry.record.data.get("timing") or {}),
                    "links": [dict(link) for link in entry.record.links],
                },
            )
            for entry in sorted(entries, key=lambda entry: (
                order.get(first_id(entry.record), len(order)), entry.record.updated_at, entry.record.id,
            ))
        )

    return Agenda(
        person=person.strip(),
        to_raise=lines("to_raise", to_raise),
        promised=lines("promised", promised),
        waiting_on=lines("waiting_on", waiting),
    )
