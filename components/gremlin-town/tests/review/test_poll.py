"""The moments polling loop: decides which signal fired, then calls run_moments.

No timer, scheduled task, or service unit is created anywhere here (step E
spec §4); every case uses a fake clock and fakes or a real in-process
GremlinTownService, never a real Telegram or Diablo send.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import CalendarEvent, ItemRecord

from gremlin_town.contracts import Actor, CreateRequest, Page, Receipt
from gremlin_town.domain import Participant, Policy
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.poll import poll_once
from gremlin_town.review.run import ReviewContext
from gremlin_town.review.settings import ReviewSettings
from gremlin_town.review.why_log import Surfacing, WhyLog
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.transport import FakeTransport
from gremlin_town.workers import OutboxWorker

NOW = datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc)  # 12:00 SGT on 2026-09-26


def enabled_settings(why_log_file: str) -> ReviewSettings:
    return ReviewSettings.from_table({
        "enabled": True,
        "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa",
        "project": "vault_home",
        "why_log_file": why_log_file,
    })


def make_item(id: str, title: str) -> ItemRecord:
    record = Record(
        schema=RECORD_SCHEMA_V2, id=id, kind="item", title=title, state="open",
        created_at=NOW, updated_at=NOW, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None,
        data={"level": "accepted", "size": "task"}, body="", source=None, links=(),
    )
    return ItemRecord(record=record, path=f"records/item/2026/09/{id}.md")


class FakeEventsService:
    """A create+events_list double: proves poll.py's own logic, not GT's worker."""

    def __init__(self, pages_by_event_type: dict[str, tuple[dict, ...]]) -> None:
        self._pages = pages_by_event_type
        self.event_calls: list[tuple[str, str | None]] = []
        self.created: list[CreateRequest] = []

    def events_list(self, actor: Actor, filters: dict, cursor: str | None = None) -> Page:
        event_type = filters["event_type"]
        self.event_calls.append((event_type, cursor))
        after = int(cursor) if cursor else 0
        items = tuple(event for event in self._pages.get(event_type, ()) if event["sequence"] > after)
        return Page(items=items, next_cursor=None, as_of=NOW)

    def requests_create(self, actor: Actor, command: CreateRequest) -> Receipt:
        self.created.append(command)
        return Receipt(
            operation_id=f"op_{len(self.created)}", request_id=f"req_{len(self.created)}",
            accepted_at=NOW, operation_state=None, work_state=None, revision=1, actor_id=actor.actor_id,
        )


def picked_today_setup(why_log: WhyLog, item: ItemRecord) -> None:
    why_log.record_surfacings([
        Surfacing(
            surfaced_at=NOW - timedelta(hours=6), channel="review", item_ref=item.record.id,
            record_sha256=item.sha256, record_state="open", rule="pick_or_park", fields_used={},
            record_path=item.path,
        )
    ])
    why_log.record_response(
        item_ref=item.record.id, response="picked_today", response_words="doing it",
        responded_at=NOW - timedelta(hours=6),
    )


def waiting_now_up_setup(why_log: WhyLog, item: ItemRecord) -> None:
    """A genuine work-01 waiting line, now actionable in the full current snapshot."""
    why_log.record_surfacings([Surfacing(
        surfaced_at=NOW - timedelta(hours=6), channel="review", item_ref=item.record.id,
        record_sha256="previous-waiting-hash", record_state="waiting", rule="waiting_on_item",
        fields_used={}, record_path=item.path,
    )])


def test_poll_once_disabled_does_nothing(tmp_path: Path) -> None:
    context = ReviewContext(
        settings=ReviewSettings.from_table(None), why_log=None, sources=ReviewSources(),
        service=FakeEventsService({}),
    )
    assert poll_once(context, NOW) == ()


def test_poll_pc_on_advances_cursor_and_notifies_reporting_actor(tmp_path: Path) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = make_item("brn_admin", "File the form")
    waiting_now_up_setup(why_log, item)
    why_log.record_signal(kind="pc_on", actor="local-codex", at=NOW, detail=None)

    service = FakeEventsService({})
    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)), why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)), service=service,
    )

    results = poll_once(context, NOW)
    assert len(results) == 1
    assert results[0]["outcome"] == "sent"
    recipients = {entry["recipient"] for entry in results[0]["deliveries"]}
    assert recipients == {"hermes-wa", "local-codex"}  # D24: also_notify = the reporting actor

    # A second poll with no new signal finds nothing.
    assert poll_once(context, NOW) == ()
    why_log.close()


def test_poll_task_finished_combines_both_event_types_in_sequence_order(tmp_path: Path) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = make_item("brn_wait", "Sam's pricing")
    from dataclasses import replace
    item = ItemRecord(replace(item.record, id="brn_current", supersedes=("brn_first", "brn_wait")),
                      "records/item/2026/09/brn_current.md")
    # Last surfaced while waiting; now open - an "event happened" candidate (§4).
    why_log.record_surfacings([
        Surfacing(
            surfaced_at=NOW - timedelta(hours=3), channel="review", item_ref="brn_wait",
            record_sha256="stale-sha", record_state="waiting", rule="waiting_on_item", fields_used={},
            record_path=item.path,
        )
    ])

    service = FakeEventsService({
        "result_check_accepted": ({"sequence": 5, "actor_id": "fleet-boss"},),
        "result_returned": ({"sequence": 3, "actor_id": "chatgpt-web"}, {"sequence": 8, "actor_id": "local-codex"}),
    })
    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)), why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)), service=service,
    )

    results = poll_once(context, NOW)
    assert len(results) == 3
    # Processed in sequence order across both event types (3, 5, 8), not per-type.
    assert [entry["signal"] for entry in results] == ["task_finished"] * 3
    # Only the first actually returns the one candidate item: once it has come
    # back today unchanged, the "once only" rule (§4) keeps the next two quiet,
    # even though each comes from a distinct, later GT event.
    assert results[0]["outcome"] == "sent"
    assert {entry["recipient"] for entry in results[0]["deliveries"]} == {"hermes-wa", "chatgpt-web"}
    assert results[1] == {"outcome": "nothing", "signal": "task_finished"}
    assert results[2] == {"outcome": "nothing", "signal": "task_finished"}
    [returned] = [row for row in why_log.history() if row.channel == "moment"]
    assert (returned.item_ref, returned.record_path, returned.record_sha256, returned.record_state) == (
        "brn_first", item.path, item.sha256, "up")

    # Cursor persisted; a second poll with the same fake pages finds nothing new.
    assert poll_once(context, NOW) == ()
    why_log.close()


def test_poll_unfinished_pick_sends_nothing_but_consumes_pc_signal_once(tmp_path: Path) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    item = make_item("brn_unfinished", "File the form")
    picked_today_setup(why_log, item)
    why_log.record_signal(kind="pc_on", actor="local-codex", at=NOW, detail=None)
    service = FakeEventsService({})
    context = ReviewContext(enabled_settings(str(tmp_path / "why.sqlite3")), why_log,
                            ReviewSources(brain_items=lambda: (item,)), service)
    assert poll_once(context, NOW) == ({"outcome": "nothing", "signal": "pc_on"},)
    assert service.created == []
    assert poll_once(context, NOW) == ()
    why_log.close()


def test_poll_calendar_gap_fires_once_per_day(tmp_path: Path) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = make_item("brn_admin", "File the form")
    waiting_now_up_setup(why_log, item)

    service = FakeEventsService({})
    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)), why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,), calendar_events=lambda start, end: ()),
        service=service,
    )

    results = poll_once(context, NOW)
    assert len(results) == 1
    assert results[0]["signal"] == "calendar_gap"
    assert results[0]["outcome"] == "sent"

    # Same Singapore day, later: does not fire again.
    later_same_day = NOW + timedelta(hours=2)
    assert poll_once(context, later_same_day) == ()
    why_log.close()


def test_poll_calendar_gap_no_calendar_source_is_silent(tmp_path: Path) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)), why_log=why_log,
        sources=ReviewSources(), service=FakeEventsService({}),
    )
    assert poll_once(context, NOW) == ()
    why_log.close()


def test_poll_calendar_gap_broken_source_does_not_block_pc_on(tmp_path: Path) -> None:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = make_item("brn_admin", "File the form")
    waiting_now_up_setup(why_log, item)
    why_log.record_signal(kind="pc_on", actor="local-codex", at=NOW, detail=None)

    def broken_calendar(start: datetime, end: datetime) -> tuple[CalendarEvent, ...]:
        raise RuntimeError("token_revoked")

    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)), why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,), calendar_events=broken_calendar),
        service=FakeEventsService({}),
    )
    results = poll_once(context, NOW)
    assert len(results) == 1
    assert results[0]["signal"] == "pc_on"
    why_log.close()


@pytest.fixture
def real_service(tmp_path: Path) -> tuple[GremlinTownService, OutboxWorker]:
    policy = Policy(
        participants=(
            Participant("chatgpt-web", "ChatGPT", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
            Participant("fleet-boss", "Fleet Boss", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
            Participant(
                "exosuit-review", "Exosuit review", "hub@example.test",
                frozenset({"vault_home"}), frozenset({"private"}), read_all_requests=True,
            ),
            Participant("hermes-wa", "Diablo", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
        ),
        routes=frozenset({
            ("vault_home", "exosuit-review", "hermes-wa", "private"),
            ("vault_home", "exosuit-review", "chatgpt-web", "private"),
            ("vault_home", "chatgpt-web", "fleet-boss", "private"),
            ("vault_home", "fleet-boss", "chatgpt-web", "private"),
        }),
    )
    store = SQLiteStore(tmp_path / "gremlin.db", now=lambda: NOW)
    provider = FakeTransport()
    service = GremlinTownService(store, policy, provider=provider, now=lambda: NOW)
    worker = OutboxWorker(store, policy, provider, now=lambda: NOW)
    return service, worker


def test_poll_task_finished_sees_a_real_result_check_accepted_event(
    tmp_path: Path, real_service: tuple[GremlinTownService, OutboxWorker]
) -> None:
    """Proves the wiring against GT's own event log, not just a fake."""

    from gremlin_town.contracts import (
        CheckOutcome, CheckRequest, MessageKind, Packet, ReplyRequest, ReportAction, ReportRequest, RequestMode,
    )

    service, worker = real_service

    def packet(kind: MessageKind, text: str) -> Packet:
        return Packet(
            schema_version=1, kind=kind, session_reference=None, objective="Draft the deck", context=text,
            constraints={}, sources=(), next_action="Draft it", expected_reply="The draft", sensitivity="private",
        )

    created = service.requests_create(
        Actor(actor_id="chatgpt-web"),
        CreateRequest(
            client_operation_id="handoff-1", mode=RequestMode.ACTION_REQUESTED, project="vault_home",
            recipient="fleet-boss", packet=packet(MessageKind.REQUEST, "Draft the deck"),
        ),
    )
    worker.dispatch_once()  # accepts the initial request message (bumps the revision)
    accepted = service.requests_read(Actor(actor_id="fleet-boss"), created.request_id)
    started = service.requests_report(
        Actor(actor_id="fleet-boss"),
        ReportRequest("start-1", created.request_id, accepted.revision, ReportAction.START),
    )
    service.requests_reply(
        Actor(actor_id="fleet-boss"),
        ReplyRequest("result-1", created.request_id, started.revision, MessageKind.RESULT, packet(MessageKind.RESULT, "42")),
    )
    returned = worker.dispatch_once()  # confirms delivery; work_state -> result_returned
    assert returned.work_state.value == "result_returned"

    service.requests_check(
        Actor(actor_id="chatgpt-web"),
        CheckRequest(
            client_operation_id="check-1", request_id=created.request_id, expected_revision=returned.revision,
            outcome=CheckOutcome.ACCEPTED, evidence="Reviewed the deck myself",
        ),
    )

    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    item = make_item("brn_admin", "File the form")
    waiting_now_up_setup(why_log, item)
    context = ReviewContext(
        settings=enabled_settings(str(why_log_path)), why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)), service=service,
    )

    results = poll_once(context, NOW)
    # Two GT events fired: result_returned, then result_check_accepted; the one
    # candidate item already came back on the first, so the "once only" rule
    # (§4) keeps the second quiet even though it is a distinct later event.
    assert len(results) == 2
    assert results[0]["outcome"] == "sent"
    # result_returned's actor is the synthetic "system:outbox" (never a real,
    # routable participant), so its also_notify silently drops and only Diablo gets it.
    assert {entry["recipient"] for entry in results[0]["deliveries"]} == {"hermes-wa"}
    assert results[1] == {"outcome": "nothing", "signal": "task_finished"}
    why_log.close()
