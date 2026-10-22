"""Per-person agendas (Ciel and Brain Home PRD §4.9; acceptance case 14)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brain_core.agenda import agenda, answers_to, same_person
from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import ItemRecord, RANKING_POINTS, RANKING_RULES
from brain_core.timewords import SINGAPORE

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=SINGAPORE)


def item(
    id_suffix: str,
    title: str,
    *,
    state: str = "open",
    level: str = "accepted",
    saved: datetime = NOW - timedelta(days=1),
    links: tuple[dict[str, str], ...] = (),
    supersedes: tuple[str, ...] = (),
    **data: object,
) -> ItemRecord:
    record = Record(
        schema=RECORD_SCHEMA_V2, id=f"brn_20261004_000000_{id_suffix}", kind="item", title=title, state=state,
        created_at=saved, updated_at=saved, assertion=None, confidence=None, sensitivity="private", vaults=(),
        owners=(), entities=(), evidence=(), supersedes=supersedes, dedupe_key=None,
        data={"level": level, "size": "task", **data}, body="", source={"said_by": "owner"}, links=links,
    )
    return ItemRecord(record=record, path=f"records/item/2026/10/{record.id}.md")


def test_what_to_raise_with_sam_what_was_promised_to_him_and_what_is_waited_on_from_him():
    """Acceptance case 14: before a call, the topics, Nic's promises to Sam and what he's waiting on
    from Sam, each most important first."""

    topic = item("a1", "The Q3 budget", raise_with="Sam")
    urgent_topic = item("a2", "The lease renewal", raise_with="sam ", timing={"due": "2026-10-05"})
    promise = item("a3", "Send Sam the deck", owed_to="Sam", timing={"due": "2026-10-06"})
    waiting = item("a4", "Sam's pricing", state="waiting", links=({"type": "waiting_on", "target": "Sam"},))
    others = (
        item("b1", "Raise with Priya", raise_with="Priya"),
        item("b2", "Done topic", raise_with="Sam", state="done", check={"locator": "said", "checked_by": "nic"}),
        item("b3", "Suggested topic", raise_with="Sam", level="suggested"),
        item("b4", "Promise to Priya", owed_to="Priya"),
    )
    result = agenda((topic, urgent_topic, promise, waiting, *others), "Sam", NOW)
    assert result.person == "Sam"
    assert [line.title for line in result.to_raise] == ["The lease renewal", "The Q3 budget"]
    assert [line.title for line in result.promised] == ["Send Sam the deck"]
    assert [line.title for line in result.waiting_on] == ["Sam's pricing"]
    assert result.to_raise[0].reasons[0] == "due today"
    assert result.to_raise[0].record_sha256 == urgent_topic.sha256
    assert not result.is_empty


def test_only_the_newest_version_counts_and_an_unknown_person_has_an_empty_agenda():
    old = item("c1", "The Q3 budget", raise_with="Sam", saved=NOW - timedelta(days=3))
    moved = item("c2", "The Q3 budget", raise_with="Priya", supersedes=(old.record.id,))
    assert agenda((old, moved), "Sam", NOW).is_empty
    assert [line.title for line in agenda((old, moved), "Priya", NOW).to_raise] == ["The Q3 budget"]
    assert agenda((old, moved), "Jo", NOW).is_empty


@pytest.mark.parametrize("ranking", [RANKING_POINTS, RANKING_RULES])
def test_due_successor_keeps_rank_reasons_and_raw_record_evidence(ranking, monkeypatch):
    from brain_core import orientation

    old = item("c3", "Old lease", raise_with="Sam", owed_to="Sam", saved=NOW - timedelta(days=3))
    urgent = item(
        "c4", "Lease due today", raise_with="Sam", owed_to="Sam",
        supersedes=(old.record.id,), timing={"due": "2026-10-05"},
    )
    ordinary = item("c5", "Ordinary topic", raise_with="Sam", owed_to="Sam")
    raw_record = urgent.record
    raw_hash = urgent.sha256
    raw_path = urgent.path
    captured = []
    ranked = []
    original_rank = orientation.rank

    def capture_rank(candidates, *args, **kwargs):
        captured.extend(candidates)
        result = original_rank(candidates, *args, **kwargs)
        ranked.extend(result)
        return result

    monkeypatch.setattr(orientation, "rank", capture_rank)
    result = agenda((ordinary, old, urgent), "Sam", NOW, ranking=ranking)

    for lines in (result.to_raise, result.promised):
        assert [line.record_id for line in lines] == [urgent.record.id, ordinary.record.id]
        assert lines[0].title == "Lease due today"
        assert lines[0].reasons[0] == "due today"
        assert lines[0].record_sha256 == raw_hash
        assert lines[0].fields_used["timing"] == {"due": "2026-10-05"}
        assert lines[0].fields_used["dates"]["deadline"]["day"] == lines[0].fields_used["timing"]["due"]
    assert next(entry for entry in captured if entry.record.id == urgent.record.id) is urgent
    assert all(entry is not old for entry in captured)
    assert ranked[0].ref == old.record.id
    assert ranked[0].item is urgent
    assert ranked[0].item.path == raw_path
    assert urgent.record is raw_record
    assert urgent.sha256 == raw_hash
    assert urgent.path == raw_path


@pytest.mark.parametrize("ranking", [RANKING_POINTS, RANKING_RULES])
def test_legacy_waiting_successor_keeps_unranked_fallback(ranking):
    links = ({"type": "waiting_on", "target": "Sam"},)
    old = item("c6", "Old wait", state="waiting", links=links, owed_to="Sam",
               saved=NOW - timedelta(days=4))
    successor = item("c7", "Current wait", state="waiting", links=links, owed_to="Sam",
                     supersedes=(old.record.id,), saved=NOW - timedelta(days=1),
                     timing={"due": "2026-10-05"})
    earlier = item("c8", "Earlier wait", state="waiting", links=links, owed_to="Sam",
                   saved=NOW - timedelta(days=2))
    result = agenda((successor, old, earlier), "Sam", NOW, ranking=ranking)

    for lines in (result.waiting_on, result.promised):
        assert [line.record_id for line in lines] == [earlier.record.id, successor.record.id]
        assert [line.reasons for line in lines] == [(), ()]
        assert [line.state for line in lines] == ["waiting", "waiting"]
        assert [line.record_sha256 for line in lines] == [earlier.sha256, successor.sha256]


def test_names_match_ignoring_case_and_surrounding_spaces_only():
    assert same_person("Sam", " sam ")
    assert not same_person("Sam", "Samantha")
    assert not same_person("Sam", None)


def test_a_first_name_finds_a_full_name_but_not_the_other_way():
    """Nic, 29 Sep 2026: "also match first names but ask me if theres multiple"."""

    assert answers_to("Sam", "Sam Wong")
    assert answers_to("sam", "  SAM   wong ")
    assert answers_to("Sam Wong", "sam  wong")
    assert not answers_to("Sam Wong", "Sam")
    assert not answers_to("Sam", "Samantha Lee")
    assert not answers_to("Sam", "Wong Sam")
    assert not answers_to("Sam", "   ")
    topic = item("d1", "The Q3 budget", raise_with="Sam Wong")
    promise = item("d2", "Send the deck", owed_to="sam wong")
    result = agenda((topic, promise), "Sam", NOW)
    assert result.person == "Sam Wong" and result.ask_which == ()
    assert [line.title for line in result.to_raise] == ["The Q3 budget"]
    assert [line.title for line in result.promised] == ["Send the deck"]


def test_when_more_than_one_person_answers_to_the_name_the_agenda_asks_which():
    wong = item("e1", "The Q3 budget", raise_with="Sam Wong")
    tan = item("e2", "Tan's pricing", state="waiting", links=({"type": "waiting_on", "target": "Sam Tan"},))
    bare = item("e3", "The lease", raise_with="Sam")
    asked = agenda((wong, tan, bare), "sam", NOW)
    assert asked.ask_which == ("Sam", "Sam Tan", "Sam Wong")
    assert asked.is_empty
    # The second call passes the name Nic chose, exactly.
    assert [line.title for line in agenda((wong, tan, bare), "Sam", NOW, exact=True).to_raise] == ["The lease"]
    chosen = agenda((wong, tan, bare), "Sam Tan", NOW)
    assert chosen.ask_which == () and [line.title for line in chosen.waiting_on] == ["Tan's pricing"]


def test_a_finished_item_about_another_sam_does_not_make_it_ask():
    wong = item("f1", "The Q3 budget", raise_with="Sam Wong")
    done = item("f2", "Old topic", raise_with="Sam Tan", state="done", check={"locator": "said", "checked_by": "nic"})
    result = agenda((wong, done), "Sam", NOW)
    assert result.ask_which == () and result.person == "Sam Wong"


def test_rowans_parts_and_needs_from_her_keep_original_audit_hashes():
    parent = item("1000", "Parent", dates={"deadline": {"day": "2026-10-06"}},
                  stake={"level": "changes_life", "confirmed": True})
    part = item("1001", "Rowan's part", parent=parent.record.id, whose="Rowan Tan")
    need = item("1002", "Need her answer", needs={"from": "rowan tan", "what": "approval"})
    promise = item("1003", "Promised photos", owed_to="Rowan Tan")
    topic = item("1004", "Raise the plan", raise_with="Rowan Tan")
    legacy = item("1005", "Legacy wait", state="waiting", links=({"type": "waiting_on", "target": "Rowan Tan"},))
    entries = (parent, part, need, promise, topic, legacy)
    before = [entry.record.as_dict() for entry in entries]
    answer = agenda(entries, "Rowan", NOW)
    assert answer.person == "Rowan Tan" and not answer.ask_which
    assert {line.title for line in answer.waiting_on} == {"Rowan's part", "Need her answer", "Legacy wait"}
    assert [line.title for line in answer.promised] == ["Promised photos"]
    assert [line.title for line in answer.to_raise] == ["Raise the plan"]
    line = next(line for line in answer.waiting_on if line.title == "Rowan's part")
    assert line.record_sha256 == part.sha256
    assert line.fields_used["dates"] == {"deadline": {"day": "2026-10-06"}}
    assert [entry.record.as_dict() for entry in entries] == before


def test_first_name_ambiguity_includes_whose_and_needs_from_and_excludes_ended_parts():
    part = item("2001", "Part", whose="Rowan Tan")
    need = item("2002", "Need", needs={"from": "Rowan Wong"})
    ended = item("2003", "Done", state="done", whose="Rowan Lim", check={"locator": "said", "checked_by": "nic"})
    asked = agenda((part, need, ended), "Rowan", NOW)
    assert asked.is_empty and asked.ask_which == ("Rowan Tan", "Rowan Wong")
    chosen = agenda((part, need, ended), "Rowan Wong", NOW, exact=True)
    assert [line.title for line in chosen.waiting_on] == ["Need"]


def test_parent_ended_or_parked_removes_its_persons_part_from_agenda():
    for state in ("done", "parked"):
        parent = item("3001", "Parent", state=state, check={"locator": "said", "checked_by": "nic"})
        part = item("3002", "Hidden part", parent=parent.record.id, whose="Rowan")
        assert agenda((parent, part), "Rowan", NOW).is_empty
