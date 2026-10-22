"""The 06:00 review's clarifying questions about thin tasks (step E spec §3.1; ROA-20).

Brain Core picks at most 3 thin tasks (proven in its own `tests/unit/test_review_clarify.py`);
this proves GT's side against a real `GremlinTownService`: the picks travel as data in the
review request's `constraints.clarify` beside the verbatim review, they are logged before
anything is sent, The organizer's answers are read from Brain Home facts, and with `[review]` off
nothing is read, logged or asked.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from brain_core.records import RECORD_SCHEMA_V2, Evidence, Record
from brain_core.review import CLARIFY_RULE, ItemRecord

from gremlin_town.contracts import Actor, CreateRequest
from gremlin_town.domain import Participant, Policy
from gremlin_town.review.brain_source import brain_facts_reader
from gremlin_town.review.collect import ReviewSources, SourceUnavailable, collect_review_inputs
from gremlin_town.review.deliver import deliver
from gremlin_town.review.run import ReviewContext, run_review
from gremlin_town.review.settings import ReviewSettings
from gremlin_town.review.why_log import WhyLog
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore

NOW = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT on Sun 27 Sep 2026
DEFAULT_NEXT_ACTION = "Post the text to Nic's Telegram, verbatim"
DEFAULT_EXPECTED_REPLY = "The Telegram message ID once posted"


def _record(
    id: str, kind: str, title: str, *, state: str, data: dict[str, Any], body: str = "",
    words: str | None = None, links: tuple[dict[str, Any], ...] = (), created_at: datetime,
) -> Record:
    source: dict[str, Any] = {"said_by": "owner", "came_by": "direct", "recorded_by": "hermes-wa"}
    evidence: tuple[Evidence, ...] = ()
    if words is not None:
        source["words"] = words
        evidence = (Evidence(source_type="fixture", locator="fixture:1", content_sha256="a" * 64, excerpt=words),)
    return Record(
        schema=RECORD_SCHEMA_V2, id=id, kind=kind, title=title, state=state,
        created_at=created_at, updated_at=created_at, assertion=None, confidence=None,
        sensitivity="private", vaults=(), owners=(), entities=(), evidence=evidence, supersedes=(),
        dedupe_key=None, data=data, body=body, source=source, links=links,
    )


def item(id: str, title: str, *, days_old: int, body: str = "") -> ItemRecord:
    # A sorted task (PRD §4.1: it has a block type), so "To sort" stays out of these tests.
    data = {"level": "accepted", "size": "task", "owed_to": None, "repeat": None, "timing": {},
            "reason": None, "check": None, "parked_until": None, "block": "admin"}
    record = _record(id, "item", title, state="open", data=data, body=body,
                     created_at=NOW - timedelta(days=days_old))
    return ItemRecord(record=record, path=f"records/item/2026/09/{id}.md")


def answer(id: str, about: str, words: str) -> ItemRecord:
    record = _record(
        id, "fact", "Call the bank", state="current",
        data={"observed_at": NOW.isoformat(), "valid_until": None}, body=words, words=words,
        links=({"type": "about", "target": about},), created_at=NOW - timedelta(hours=12),
    )
    return ItemRecord(record=record, path=f"records/fact/2026/09/{id}.md")


THIN = (
    item("brn_bank", "Call the bank", days_old=9, body="I need to call the bank"),
    item("brn_fix", "Fix it", days_old=7),
    item("brn_form", "Sort out the form", days_old=5),
    item("brn_tommy", "Email Tommy", days_old=2),
)
CLEAR = item("brn_deck", "Send the deck", days_old=12, body="the Q3 deck, to Sam and Priya")


def enabled_settings(why_log_file: str) -> ReviewSettings:
    return ReviewSettings.from_table({
        "enabled": True,
        "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa",
        "project": "vault_home",
        "why_log_file": why_log_file,
    })


@pytest.fixture
def real_service(tmp_path: Path) -> GremlinTownService:
    policy = Policy(
        participants=(
            Participant("exosuit-review", "Exosuit review", "hub@example.test",
                        frozenset({"vault_home"}), frozenset({"private"}), read_all_requests=True),
            Participant("hermes-wa", "Diablo", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
        ),
        routes=frozenset({("vault_home", "exosuit-review", "hermes-wa", "private")}),
    )
    store = SQLiteStore(tmp_path / "gremlin.db", now=lambda: NOW)
    return GremlinTownService(store, policy, now=lambda: NOW)


class _Capturing:
    """Delegates to the real service, keeping each exact `CreateRequest` and the why-log
    rows that already existed when it was sent."""

    def __init__(self, inner: GremlinTownService, why_log: WhyLog) -> None:
        self._inner = inner
        self._why_log = why_log
        self.created: list[CreateRequest] = []
        self.logged_before: list[tuple[str, ...]] = []

    def requests_create(self, actor: Actor, command: CreateRequest):
        self.logged_before.append(tuple(row.item_ref for row in self._why_log.history()))
        self.created.append(command)
        return self._inner.requests_create(actor, command)


def _context(tmp_path: Path, service: Any, why_log: WhyLog, **sources: Any) -> ReviewContext:
    # Explicit synthetic empty pre-move INPUT for the new morning host.
    why_log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    # An empty calendar and no open requests, so "Couldn't see" only lists what a test breaks.
    readers: dict[str, Any] = {"calendar_events": lambda start, end: (), "open_requests": lambda: ()}
    return ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")),
        why_log=why_log,
        sources=ReviewSources(**{**readers, **sources}),
        service=service,
    )


def test_the_review_request_carries_the_picks_as_data_beside_the_verbatim_review(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: (*THIN, CLEAR), brain_facts=lambda: ())

    result = run_review(context, NOW)
    assert result["outcome"] == "sent" and result["clarify_count"] == 0
    packet = capturing.created[0].packet
    assert "clarify" not in packet.constraints and "to_sort" not in packet.constraints
    assert "## New" in packet.context and "New, nothing needed" in packet.context
    assert all(record.record.title in packet.context for record in (*THIN, CLEAR))
    assert "gremlin-town-review" in packet.next_action and '"start"' in packet.next_action
    assert "What's the first step" not in packet.context
    snapshot = real_service.requests_read(Actor(actor_id="hermes-wa"), result["request_id"])
    wire = json.loads(snapshot.content[0].body)
    assert wire["packet"]["constraints"] == packet.constraints
    assert wire["packet"]["context"] == packet.context
    why_log.close()


def test_picks_are_logged_before_sending_under_their_own_reference(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: THIN, brain_facts=lambda: ())
    run_review(context, NOW)

    assert {record.record.id for record in THIN} <= set(capturing.logged_before[0])
    assert not any(row.item_ref.startswith("clarify:") for row in why_log.history())
    rows = [row for row in why_log.history() if row.rule == "new_nothing_needed"]
    assert {(row.item_ref, row.record_sha256, row.record_path) for row in rows} == {
        (record.record.id, record.sha256, record.path) for record in THIN
    }
    why_log.record_response(item_ref="brn_fix", response="picked_today", response_words="pick it", responded_at=NOW)
    responded = [row for row in why_log.history() if row.response == "picked_today"]
    assert [(row.item_ref, row.rule) for row in responded] == [("brn_fix", "your_day")]
    why_log.close()


def test_with_no_thin_task_the_request_is_exactly_as_before(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: (CLEAR,), brain_facts=lambda: ())
    result = run_review(context, NOW)

    packet = capturing.created[0].packet
    assert result["clarify_count"] == 0
    assert "clarify" not in packet.constraints
    assert "New, nothing needed" in packet.context and CLEAR.record.title in packet.context
    assert not [row for row in why_log.history() if row.rule == CLARIFY_RULE]
    why_log.close()


class _Guard:
    def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("must not be called while [review] is disabled")

    def requests_create(self, actor: Actor, command: CreateRequest) -> object:
        raise AssertionError("must not send while [review] is disabled")


def test_nothing_is_read_logged_or_asked_while_review_is_off() -> None:
    guard = _Guard()
    context = ReviewContext(
        settings=ReviewSettings.from_table(None),
        why_log=None,
        sources=ReviewSources(brain_items=guard, brain_facts=guard, calendar_events=guard, open_requests=guard),
        service=guard,
    )
    assert run_review(context, NOW) == {"outcome": "disabled"}


def test_a_task_asked_about_yesterday_is_not_asked_again_today(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: THIN, brain_facts=lambda: ())

    assert run_review(context, NOW)["clarify_count"] == 0
    assert "New, nothing needed" in capturing.created[0].packet.context
    assert run_review(context, NOW + timedelta(days=1))["clarify_count"] == 0
    assert "New, nothing needed" not in capturing.created[1].packet.context
    assert all(record.record.title in capturing.created[1].packet.context for record in THIN)
    assert run_review(context, NOW + timedelta(days=2))["clarify_count"] == 0
    assert all("clarify" not in command.packet.constraints for command in capturing.created)
    why_log.close()


def test_an_answer_shows_in_the_review_and_stops_the_question(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    reply = answer("brn_reply", about="brn_bank", words="DBS, about the card fee they charged twice")
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: THIN, brain_facts=lambda: (reply,))
    run_review(context, NOW)

    packet = capturing.created[0].packet
    assert "Call the bank" in packet.context
    assert "DBS, about the card fee" not in packet.context
    assert "clarify" not in packet.constraints
    assert not any(row.rule == CLARIFY_RULE for row in why_log.history())
    # Work-01's pure builder retains its original fact-answer behavior.
    from brain_core.review import build_review, render_review
    legacy = build_review(collect_review_inputs(context.sources, (), NOW))
    assert 'you said: "DBS, about the card fee they charged twice"' in render_review(legacy)
    why_log.close()


def test_a_failing_fact_reader_asks_nothing_shows_no_answers_and_says_so(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    def broken() -> tuple[ItemRecord, ...]:
        raise SourceUnavailable("github_scan_limit")

    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: THIN, brain_facts=broken)
    assert run_review(context, NOW)["clarify_count"] == 0

    packet = capturing.created[0].packet
    assert "clarify" not in packet.constraints
    assert "brain facts" not in packet.context and "you said" not in packet.context
    assert "Call the bank" in packet.context
    why_log.close()


def test_an_unconfigured_fact_reader_asks_nothing_and_adds_no_line() -> None:
    inputs = collect_review_inputs(ReviewSources(brain_items=lambda: THIN), (), NOW)
    assert inputs.facts is None
    assert "brain facts" not in inputs.source_status

    read = collect_review_inputs(ReviewSources(brain_items=lambda: THIN, brain_facts=lambda: ()), (), NOW)
    assert read.facts == ()
    assert "brain facts" not in read.source_status


def test_the_fact_reader_reads_only_facts_from_brain_home() -> None:
    class _Snapshot:
        def __init__(self) -> None:
            self.kinds: list[frozenset[str]] = []

        def snapshot_records(self, *, kinds: frozenset[str], max_records: int = 512, max_bytes: int | None = None):
            self.kinds.append(kinds)
            reply = answer("brn_reply", about="brn_bank", words="DBS")

            class _File:
                path = reply.path
                record = reply.record

            class _Read:
                records = (_File(),)

            return _Read()

        def read_records(self, paths):  # pragma: no cover - not used by this reader
            raise AssertionError

    source = _Snapshot()
    facts = brain_facts_reader(source)()
    assert source.kinds == [frozenset({"fact"})]
    assert [(fact.record.id, fact.path) for fact in facts] == [("brn_reply", "records/fact/2026/09/brn_reply.md")]


def test_deliver_passes_constraints_through_and_defaults_to_empty() -> None:
    created: list[CreateRequest] = []

    class _Service:
        def requests_create(self, actor: Actor, command: CreateRequest):
            created.append(command)
            return None

    common = dict(
        sender_participant="exosuit-review", delivery_participant="hermes-wa", project="vault_home",
        objective="Post today's 06:00 review to Nic's Telegram, verbatim", text="## Your day: pick 3 to 7",
    )
    deliver(_Service(), client_operation_id="review-a", **common)
    deliver(_Service(), client_operation_id="review-b", constraints={"clarify": []}, **common)
    assert [command.packet.constraints for command in created] == [{}, {"clarify": []}]


# --- N12c (Ciel slice 2): a started task with no first step is asked for one ---------------


def test_a_started_task_with_no_first_step_is_asked_in_fixed_words_and_logged_under_its_rule(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    from brain_core.review import CLARIFY_FIRST_STEP_RULE, OpenNote

    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    started_note = OpenNote(session_id="deck-for-sam", next_step="", updated_at=NOW - timedelta(days=1), task_id="brn_deck")
    context = _context(
        tmp_path, capturing, why_log,
        brain_items=lambda: (CLEAR, THIN[0]), brain_facts=lambda: (), open_notes=lambda: (started_note,),
    )
    result = run_review(context, NOW)
    assert result["clarify_count"] == 0
    packet = capturing.created[0].packet
    assert "clarify" not in packet.constraints and "What's the first step" not in packet.context
    assert not any(row.item_ref.startswith("clarify:") for row in why_log.history())
    # The original work-01 API still owns N12c's note-based question.
    from brain_core.review import build_review
    old = build_review(collect_review_inputs(context.sources, (), NOW))
    assert any(task.rule == CLARIFY_FIRST_STEP_RULE for task in old.clarify)
    assert run_review(context, NOW + timedelta(days=1))["clarify_count"] == 0
    why_log.close()


def test_unreadable_notes_ask_nothing_about_first_steps_and_say_so(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)

    def broken():
        raise SourceUnavailable("github_scan_limit")

    context = _context(tmp_path, capturing, why_log, brain_items=lambda: (CLEAR,), brain_facts=lambda: (), open_notes=broken)
    assert run_review(context, NOW)["clarify_count"] == 0
    assert "notes: github_scan_limit" not in capturing.created[0].packet.context
    assert "What's the first step" not in capturing.created[0].packet.context
    why_log.close()


# --- "To sort" beside the review (Ciel and Brain Home PRD §4.1; build plan slice 4) -----------------


def _unsorted(id: str, title: str, *, days_old: int, **data: Any) -> ItemRecord:
    fields = {"level": "accepted", "size": "task", "owed_to": None, "repeat": None, "timing": {},
              "reason": None, "check": None, "parked_until": None, **data}
    record = _record(id, "item", title, state="open", data=fields, body="", created_at=NOW - timedelta(days=days_old))
    return ItemRecord(record=record, path=f"records/item/2026/09/{id}.md")


def test_the_review_request_carries_what_to_sort_for_damien_to_pre_sort_and_ask(
    tmp_path: Path, real_service: GremlinTownService
) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    bulbs = _unsorted("brn_bulbs", "Buy light bulbs", days_old=30)
    reply = _unsorted("brn_jo", "Reply to Jo's text", days_old=2, block="admin",
                      timing={"duration_minutes": 2, "padded_minutes": 3, "estimated_by": "hermes-wa"})
    reply = replace(reply, record=replace(reply.record, data={**reply.record.data, "level": "suggested"},
                                          source={**reply.record.source, "said_by": "agent", "speaker": "hermes-wa"}))
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: (bulbs, reply))

    result = run_review(context, NOW)
    assert result["outcome"] == "sent" and result["to_sort_count"] == 0
    packet = capturing.created[0].packet
    assert "## To sort" not in packet.context and "to_sort" not in packet.constraints
    assert "Buy light bulbs" in packet.context and "New, nothing needed" in packet.context
    assert "gremlin-town-review" in packet.next_action
    # The original pure builder still returns its sort payload.
    from brain_core.review import build_review
    legacy = build_review(collect_review_inputs(context.sources, (), NOW))
    assert {row.record_id for row in legacy.to_sort} == {"brn_bulbs", "brn_jo"}
    why_log.close()


def test_questions_and_sorting_share_one_separate_message(tmp_path: Path, real_service: GremlinTownService) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    capturing = _Capturing(real_service, why_log)
    bulbs = _unsorted("brn_bulbs", "Buy light bulbs", days_old=30)
    context = _context(tmp_path, capturing, why_log, brain_items=lambda: (*THIN, bulbs), brain_facts=lambda: ())
    run_review(context, NOW)
    packet = capturing.created[0].packet
    assert "clarify" not in packet.constraints and "to_sort" not in packet.constraints
    assert "## New" in packet.context and "Buy light bulbs" in packet.context
    assert "Call the bank" in packet.context
    assert "What's the first step" not in packet.context
    why_log.close()
