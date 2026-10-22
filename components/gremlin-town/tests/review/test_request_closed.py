"""A request Nic was waiting on comes back once when it closes (step E §12, ROA-21).

Every case runs a real in-process hub (`GremlinTownService`, `OutboxWorker`, `SQLiteStore`,
`Policy`), the real 06:00 review, the real moments poll and `runtime.py`'s own request readers,
on a clock the test moves by hand. Nothing reaches Telegram, Diablo or the live hub.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import ItemRecord, build_review, render_review

from gremlin_town.contracts import (
    Actor,
    CheckOutcome,
    CheckRequest,
    CreateRequest,
    MessageKind,
    Packet,
    ReplyRequest,
    ReportAction,
    ReportRequest,
    RequestMode,
)
from gremlin_town.domain import Participant, Policy
from gremlin_town.review.collect import ReviewSources, SourceUnavailable, collect_moments_requests, collect_review_inputs
from gremlin_town.review.poll import poll_once
from gremlin_town.review.run import ReviewContext, run_moments, run_review
from gremlin_town.review.settings import ReviewSettings
from gremlin_town.review.why_log import Surfacing, WhyLog
from gremlin_town.runtime import _review_open_requests_reader, _review_request_status_reader, build_runtime
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.transport import FakeTransport
from gremlin_town.workers import OutboxWorker
from tests.review.test_api_routes import write_config

AT_0600 = datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT, Sat 26 Sep 2026
AT_0800 = AT_0600 + timedelta(hours=2)
AT_0900 = AT_0600 + timedelta(hours=3)
AT_1000 = AT_0600 + timedelta(hours=4)
AT_1100 = AT_0600 + timedelta(hours=5)
AT_1400 = AT_0600 + timedelta(hours=8)
NEXT_0600 = AT_0600 + timedelta(days=1)


def enabled_settings(why_log_file: Path) -> ReviewSettings:
    return ReviewSettings.from_table({
        "enabled": True,
        "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa",
        "project": "vault_home",
        "why_log_file": str(why_log_file),
    })


def packet(kind: MessageKind, text: str, objective: str = "Draft the deck") -> Packet:
    return Packet(
        schema_version=1, kind=kind, session_reference=None, objective=objective, context=text,
        constraints={}, sources=(), next_action="Draft it", expected_reply="The draft", sensitivity="private",
    )


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Hub:
    """Nic's agent (chatgpt-web) hands work to fleet-boss, which returns it; the review sender
    (exosuit-review) reads every request in the project and delivers through Diablo."""

    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock(AT_0600 - timedelta(days=1))
        self.policy = Policy(
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
                ("vault_home", "chatgpt-web", "fleet-boss", "private"),
                ("vault_home", "fleet-boss", "chatgpt-web", "private"),
                ("vault_home", "exosuit-review", "hermes-wa", "private"),
                ("vault_home", "hermes-wa", "exosuit-review", "private"),
                ("vault_home", "exosuit-review", "chatgpt-web", "private"),
            }),
        )
        store = SQLiteStore(tmp_path / "gremlin.db", now=self.clock)
        provider = FakeTransport()
        self.service = GremlinTownService(store, self.policy, provider=provider, now=self.clock)
        self.worker = OutboxWorker(store, self.policy, provider, now=self.clock)

    def at(self, now: datetime) -> Hub:
        self.clock.now = now
        return self

    def drain(self) -> None:
        while self.worker.dispatch_once() is not None:
            pass

    def item(self, actor_id: str, request_id: str) -> dict:
        items = self.service.requests_list(Actor(actor_id=actor_id), {}).items
        return next(item for item in items if item["request_id"] == request_id)

    def hand_off(self, key: str) -> str:
        created = self.service.requests_create(
            Actor(actor_id="chatgpt-web"),
            CreateRequest(
                client_operation_id=f"handoff-{key}", mode=RequestMode.ACTION_REQUESTED, project="vault_home",
                recipient="fleet-boss", packet=packet(MessageKind.REQUEST, f"Draft {key}", objective=f"Draft the {key}"),
            ),
        )
        self.drain()
        return created.request_id

    def return_result(self, request_id: str, *, by: str = "fleet-boss", proof: str | None = None) -> None:
        self.drain()  # the request's own first message must be accepted before any reply
        revision = self.item(by, request_id)["revision"]
        started = self.service.requests_report(
            Actor(actor_id=by), ReportRequest(f"start-{request_id}", request_id, revision, ReportAction.START)
        )
        self.service.requests_reply(
            Actor(actor_id=by),
            ReplyRequest(
                f"result-{request_id}", request_id, started.revision, MessageKind.RESULT,
                packet(MessageKind.RESULT, "Here it is"),
                proof=proof,
            ),
        )
        self.drain()
        assert self.item(by, request_id)["work_state"] == "result_returned"

    def check(self, request_id: str, outcome: CheckOutcome = CheckOutcome.ACCEPTED) -> None:
        self.service.requests_check(
            Actor(actor_id="chatgpt-web"),
            CheckRequest(
                client_operation_id=f"check-{request_id}-{outcome.value}", request_id=request_id,
                expected_revision=self.item("chatgpt-web", request_id)["revision"], outcome=outcome,
                evidence="Read the draft myself",
            ),
        )

    def sources(self, **overrides) -> ReviewSources:
        """The runtime's own adapters, exactly as `build_runtime` wires them."""

        readers = {
            "open_requests": _review_open_requests_reader(self.service, self.policy, "exosuit-review"),
            "request_status": _review_request_status_reader(self.service, self.policy, "exosuit-review"),
        }
        return ReviewSources(**{**readers, **overrides})


class Recording:
    """The real service, keeping (recipient, text) for every review or moment it is asked to send."""

    def __init__(self, service: GremlinTownService) -> None:
        self._service = service
        self.sent: list[tuple[str, str]] = []

    def requests_create(self, actor: Actor, command: CreateRequest):
        self.sent.append((command.recipient, command.packet.context))
        return self._service.requests_create(actor, command)

    def events_list(self, actor: Actor, filters: dict, cursor: str | None = None):
        return self._service.events_list(actor, filters, cursor)

    def texts_to(self, recipient: str) -> list[str]:
        return [text for to, text in self.sent if to == recipient]


def context_for(hub: Hub, tmp_path: Path, **overrides) -> tuple[ReviewContext, Recording, WhyLog]:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    # Explicit synthetic empty pre-move INPUT for these fake post-move captures.
    why_log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    recording = Recording(hub.service)
    context = ReviewContext(
        settings=enabled_settings(tmp_path / "why.sqlite3"),
        why_log=why_log,
        sources=hub.sources(**overrides),
        service=recording,
    )
    return context, recording, why_log


def moment_rows(why_log: WhyLog, request_id: str):
    return [row for row in why_log.full_reason(request_id) if row.channel == "moment"]


def work01_waiting_history(why_log: WhyLog, request_id: str, *, state="open", objective="Draft the deck"):
    """A literal work-01 row, for unchanged daytime-return compatibility cases."""
    why_log.record_surfacings([Surfacing(
        surfaced_at=AT_0600, channel="review", item_ref=request_id,
        record_sha256="", record_state=state, rule="waiting_on_request",
        fields_used={"handed_to": "fleet-boss", "objective": objective},
    )])


def work01_review_text(context: ReviewContext) -> str:
    """Keep the original pure review's waiting wording and cap under test."""
    return render_review(build_review(collect_review_inputs(
        context.sources, context.why_log.history(), AT_0600,
    )))


def test_a_proved_result_returns_in_next_review_once_even_if_already_returned_in_daytime(tmp_path):
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    context, recording, why_log = context_for(hub, tmp_path)
    run_review(context, hub.at(AT_0600).clock.now)
    assert "## Waiting on" not in recording.texts_to("hermes-wa")[-1]
    # Keep the daytime compatibility case: work-01 had shown this request.
    # Work-02's morning no longer enumerates ordinary waiting requests.
    work01_waiting_history(why_log, request_id)
    hub.at(AT_0900).return_result(request_id, proof="https://example.test/deck")
    run_moments(context, AT_1000, "pc_on")
    assert 'back, with proof: "https://example.test/deck"' in recording.texts_to("hermes-wa")[-1]
    run_review(context, hub.at(NEXT_0600).clock.now)
    assert 'back, with proof: "https://example.test/deck"' in recording.texts_to("hermes-wa")[-1]
    run_review(context, hub.at(NEXT_0600 + timedelta(days=1)).clock.now)
    assert "back, with proof" not in recording.texts_to("hermes-wa")[-1]
    assert len([row for row in why_log.full_reason(request_id)
                if row.rule == "request_returned_proof" and row.channel == "review"]) == 1


def test_an_unresolving_proof_id_stays_open_and_reads_unverified(tmp_path):
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    context, recording, _ = context_for(hub, tmp_path)
    hub.at(AT_0900).return_result(request_id, proof="op_missing")
    run_review(context, AT_1000)
    assert 'back, proof unverified: "op_missing"' in recording.texts_to("hermes-wa")[-1]
    assert hub.service.requests_list(Actor("exosuit-review"), {"open": True}).items


def test_a_request_shown_in_waiting_on_then_checked_comes_back_once_as_done(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    context, recording, why_log = context_for(hub, tmp_path)

    # Work-02 omits ordinary waiting; work-01's pure review still lists it.
    assert run_review(context, hub.at(AT_0600).clock.now)["outcome"] == "sent"
    (review_text,) = recording.texts_to("hermes-wa")
    assert "## Waiting on" not in review_text
    assert "Draft the deck — with fleet-boss since" in work01_review_text(context).split("## Waiting on")[1]
    assert request_id not in review_text  # named by its objective, not its ID
    work01_waiting_history(why_log, request_id)

    # 09:00: fleet-boss returns the result and Nic's agent checks and accepts it.
    hub.at(AT_0900).return_result(request_id)
    hub.check(request_id)

    # 10:00: the poll's next task_finished signal brings it back, worded as done.
    results = poll_once(context, AT_1000)
    assert [result["outcome"] for result in results] == ["sent", "nothing"]
    assert results[0]["request_ids"] == (request_id,)
    assert results[0]["item_ids"] == ()
    assert recording.texts_to("hermes-wa")[1:] == ["1. Draft the deck — done (was with fleet-boss)"]

    # Logged before sending, once, with the state it came back in (§7).
    (row,) = moment_rows(why_log, request_id)
    assert row.rule == "request_done"
    assert row.signal == "task_finished"
    assert row.fields_used == {
        "signal": "task_finished", "work_state": "result_returned", "check": "accepted", "handed_to": "fleet-boss",
        "objective": "Draft the deck",
    }
    assert [entry.record_state for entry in why_log.history() if entry.channel == "moment"] == ["done"]
    why_log.close()


def test_a_request_nic_was_never_shown_is_never_read_or_reported(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    hub.at(AT_0900).return_result(request_id)
    hub.check(request_id)

    def never(request_ids: tuple[str, ...]):
        raise AssertionError("no request was shown, so none may be read")

    context, recording, why_log = context_for(hub, tmp_path, request_status=never)
    results = poll_once(context, AT_1000)
    assert [result["outcome"] for result in results] == ["nothing", "nothing"]
    assert recording.sent == []
    assert moment_rows(why_log, request_id) == []
    why_log.close()


def test_a_request_handed_off_after_the_review_is_not_reported_when_it_closes(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    shown = hub.hand_off("deck")
    real_reader = _review_request_status_reader(hub.service, hub.policy, "exosuit-review")
    asked: list[tuple[str, ...]] = []

    def spy(request_ids: tuple[str, ...]):
        asked.append(request_ids)
        return real_reader(request_ids)

    context, recording, why_log = context_for(hub, tmp_path, request_status=spy)
    run_review(context, hub.at(AT_0600).clock.now)
    assert "## Waiting on" not in recording.texts_to("hermes-wa")[-1]
    work01_waiting_history(why_log, shown)

    # 08:00, after the review: a second hand-off, returned and accepted by 09:00.
    unseen = hub.at(AT_0800).hand_off("budget")
    hub.at(AT_0900).return_result(unseen)
    hub.check(unseen)

    results = poll_once(context, AT_1000)
    assert [result["outcome"] for result in results] == ["nothing", "nothing"]
    assert asked == [(shown,), (shown,)]  # only ever the request Nic saw
    assert len(recording.texts_to("hermes-wa")) == 1  # the review, nothing since
    assert moment_rows(why_log, unseen) == []
    why_log.close()


def test_a_returned_result_reads_returned_not_yet_checked_then_done_once_each(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    context, recording, why_log = context_for(hub, tmp_path)
    run_review(context, hub.at(AT_0600).clock.now)
    assert "## Waiting on" not in recording.texts_to("hermes-wa")[-1]
    work01_waiting_history(why_log, request_id)

    # 09:00: the result is back; nobody has checked it.
    hub.at(AT_0900).return_result(request_id)
    first = poll_once(context, AT_1000)
    assert [result["outcome"] for result in first] == ["sent"]
    returned_text = recording.texts_to("hermes-wa")[-1]
    assert returned_text == "1. Draft the deck — returned, not yet checked (with fleet-boss)"
    assert "done" not in returned_text
    # result_returned's actor is the outbox, never a routable agent: Diablo only (§4).
    assert {entry["recipient"] for entry in first[0]["deliveries"]} == {"hermes-wa"}

    # Still unchecked at 11:00: it came back in this state already, so it stays quiet.
    assert run_moments(context, AT_1100, "pc_on") == {"outcome": "nothing", "signal": "pc_on"}

    # Nic's agent accepts it; the check's own signal brings it back once, now as done, to
    # Diablo and to the agent that checked it (D24).
    hub.at(AT_1400 - timedelta(minutes=30)).check(request_id)
    second = poll_once(context, AT_1400)
    assert [result["outcome"] for result in second] == ["sent"]
    assert {entry["recipient"] for entry in second[0]["deliveries"]} == {"hermes-wa", "chatgpt-web"}
    assert recording.texts_to("hermes-wa")[-1] == "1. Draft the deck — done (was with fleet-boss)"
    assert recording.texts_to("chatgpt-web") == ["1. Draft the deck — done (was with fleet-boss)"]
    assert [row.rule for row in moment_rows(why_log, request_id)] == ["request_returned_unchecked", "request_done"]
    why_log.close()


def test_a_result_already_back_is_labelled_in_the_review_and_not_repeated_during_the_day(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    hub.at(AT_0600 - timedelta(hours=8)).return_result(request_id)  # back last night, unchecked
    context, recording, why_log = context_for(hub, tmp_path)

    run_review(context, hub.at(AT_0600).clock.now)
    (review_text,) = recording.texts_to("hermes-wa")
    assert "## Waiting on" not in review_text
    assert "Draft the deck — returned, not yet checked (with fleet-boss since yesterday)" in work01_review_text(context)
    assert "done" not in review_text
    work01_waiting_history(why_log, request_id, state="returned_unchecked")

    # Nothing changed since the review said so, so the day's signals stay quiet about it...
    assert [result["outcome"] for result in poll_once(context, AT_1000)] == ["nothing"]
    assert run_moments(context, AT_1100, "pc_on") == {"outcome": "nothing", "signal": "pc_on"}

    # ...until someone other than fleet-boss accepts it: then it comes back once, as done.
    hub.at(AT_1400 - timedelta(minutes=30)).check(request_id)
    (result,) = poll_once(context, AT_1400)
    assert result["request_ids"] == (request_id,)
    assert recording.texts_to("hermes-wa")[-1] == "1. Draft the deck — done (was with fleet-boss)"
    why_log.close()


def test_replays_and_later_signals_send_nothing_twice(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    context, recording, why_log = context_for(hub, tmp_path)
    run_review(context, hub.at(AT_0600).clock.now)
    assert "## Waiting on" not in recording.texts_to("hermes-wa")[-1]
    first_legacy_text = work01_review_text(context)
    work01_waiting_history(why_log, request_id)
    hub.at(AT_0900).return_result(request_id)
    hub.check(request_id)

    first = run_moments(context, AT_1000, "task_finished", signal_id="task_finished-7")
    assert first["request_ids"] == (request_id,)
    sent_so_far = len(recording.sent)

    # The same occurrence replayed, the poll's own events, and every later signal: nothing.
    assert run_moments(context, AT_1000, "task_finished", signal_id="task_finished-7")["outcome"] == "nothing"
    assert [result["outcome"] for result in poll_once(context, AT_1000)] == ["nothing", "nothing"]
    assert poll_once(context, AT_1100) == ()
    for signal in ("pc_on", "task_finished", "calendar_gap"):
        assert run_moments(context, AT_1400, signal) == {"outcome": "nothing", "signal": signal}
    assert len(recording.sent) == sent_so_far

    # Next morning it is closed, so the review no longer lists it, and it never comes back.
    hub.at(NEXT_0600)
    assert run_review(context, NEXT_0600)["outcome"] == "sent"
    assert "Draft the deck" in first_legacy_text  # listed in work-01
    assert "Draft the deck" not in recording.texts_to("hermes-wa")[-1]
    assert run_moments(context, NEXT_0600 + timedelta(hours=3), "pc_on")["outcome"] == "nothing"
    assert len(moment_rows(why_log, request_id)) == 1
    why_log.close()


def test_a_picked_item_and_a_closed_request_come_back_in_one_message(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    record = Record(
        schema=RECORD_SCHEMA_V2, id="brn_form", kind="item", title="File the form", state="open",
        created_at=AT_0600, updated_at=AT_0600, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None,
        data={"level": "accepted", "size": "task"}, body="", source=None, links=(),
    )
    item = ItemRecord(record=record, path="records/item/2026/09/brn_form.md")
    context, recording, why_log = context_for(hub, tmp_path, brain_items=lambda: (item,))

    run_review(context, hub.at(AT_0600).clock.now)
    assert "## Waiting on" not in recording.texts_to("hermes-wa")[-1]
    work01_waiting_history(why_log, request_id)
    why_log.record_surfacings([
        Surfacing(
            surfaced_at=AT_0600, channel="review", item_ref="brn_form", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])
    hub.at(AT_0900).return_result(request_id)
    hub.check(request_id)

    result = run_moments(context, AT_1000, "pc_on")
    assert result["item_ids"] == ("brn_form",)
    assert result["request_ids"] == (request_id,)
    # The genuine ended wait and checked request share a message; neither uses retired first-step wording.
    assert recording.texts_to("hermes-wa")[-1] == "\n".join([
        "1. File the form — no longer waiting",
        "2. Draft the deck — done (was with fleet-boss)",
    ])
    why_log.close()


@pytest.mark.parametrize("damien", ["returns the message ID", "blocks it"])
def test_the_reviews_own_deliveries_never_show_in_waiting_on_or_come_back(tmp_path: Path, damien: str) -> None:
    """Every delivery stays open: Diablo reports its Telegram message ID as a result nobody
    checks, or, as in the 28 Sep dry run, marks it blocked. Listed, a returned one would then
    echo back as a return every day."""

    hub = Hub(tmp_path)
    context, recording, why_log = context_for(hub, tmp_path)
    first = run_review(context, hub.at(AT_0600).clock.now)
    hub.at(AT_0600 + timedelta(minutes=5))
    if damien == "blocks it":
        hub.drain()
        revision = hub.item("hermes-wa", first["request_id"])["revision"]
        hub.service.requests_report(
            Actor(actor_id="hermes-wa"),
            ReportRequest("block-review", first["request_id"], revision, ReportAction.BLOCK, reason="no send tool"),
        )
        assert hub.item("exosuit-review", first["request_id"])["work_state"] == "blocked"
    else:
        hub.return_result(first["request_id"], by="hermes-wa")
        assert hub.item("exosuit-review", first["request_id"])["check"] == "none"  # open, per step B

    hub.at(NEXT_0600)
    assert run_review(context, NEXT_0600)["outcome"] == "sent"
    assert "Waiting on" not in recording.texts_to("hermes-wa")[-1]
    assert run_moments(context, NEXT_0600 + timedelta(hours=3), "pc_on")["outcome"] == "nothing"
    why_log.close()


def test_a_request_still_being_worked_on_or_rejected_does_not_come_back(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    in_progress = hub.hand_off("deck")
    rejected = hub.hand_off("budget")
    context, recording, why_log = context_for(hub, tmp_path)
    run_review(context, hub.at(AT_0600).clock.now)

    hub.at(AT_0900).return_result(rejected)
    hub.check(rejected, CheckOutcome.REJECTED)
    revision = hub.item("fleet-boss", in_progress)["revision"]
    hub.service.requests_report(
        Actor(actor_id="fleet-boss"), ReportRequest("start-deck", in_progress, revision, ReportAction.START)
    )

    assert [result["outcome"] for result in poll_once(context, AT_1000)] == ["nothing"]
    assert len(recording.texts_to("hermes-wa")) == 1  # the review only
    why_log.close()


def test_collect_reads_only_the_requests_shown_and_not_yet_told_done() -> None:
    from brain_core.review import HistoryRow

    def row(item_ref: str, channel: str, rule: str, state: str | None = None) -> HistoryRow:
        return HistoryRow(
            item_ref=item_ref, channel=channel, rule=rule, record_sha256="", record_state=state, surfaced_at=AT_0600,
        )

    def never(request_ids: tuple[str, ...]):
        raise AssertionError("must not read with nothing shown")

    assert collect_moments_requests(ReviewSources(request_status=never), ()) == ((), None)

    asked: list[tuple[str, ...]] = []
    history = (
        row("req_b", "review", "waiting_on_request"),
        row("req_a", "review", "waiting_on_request"),
        row("req_done", "review", "waiting_on_request"),
        row("req_done", "moment", "request_done", "done"),
    )
    collect_moments_requests(ReviewSources(request_status=lambda ids: asked.append(ids) or ()), history)
    assert asked == [("req_a", "req_b")]

    assert collect_moments_requests(ReviewSources(), history) == ((), "not_configured")

    def broken(request_ids: tuple[str, ...]):
        raise SourceUnavailable("hub_unavailable")

    assert collect_moments_requests(ReviewSources(request_status=broken), history) == ((), "hub_unavailable")


def test_a_broken_request_reader_never_blocks_a_picked_item(tmp_path: Path, caplog) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    record = Record(
        schema=RECORD_SCHEMA_V2, id="brn_form", kind="item", title="File the form", state="open",
        created_at=AT_0600, updated_at=AT_0600, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None,
        data={"level": "accepted", "size": "task"}, body="", source=None, links=(),
    )
    item = ItemRecord(record=record, path="records/item/2026/09/brn_form.md")

    def broken(request_ids: tuple[str, ...]):
        raise RuntimeError("database is locked")

    context, recording, why_log = context_for(
        hub, tmp_path, brain_items=lambda: (item,), request_status=broken
    )
    run_review(context, hub.at(AT_0600).clock.now)
    work01_waiting_history(why_log, request_id)
    why_log.record_surfacings([
        Surfacing(
            surfaced_at=AT_0600, channel="review", item_ref="brn_form", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])

    result = run_moments(context, AT_1000, "pc_on")
    assert result["item_ids"] == ("brn_form",)
    assert result["request_ids"] == ()
    assert "couldn't read the requests Nic is waiting on" in caplog.text
    assert recording.texts_to("hermes-wa")[-1] == "1. File the form — no longer waiting"
    why_log.close()


def test_unfinished_pick_stays_quiet_alongside_once_only_quoted_result_proof(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    request_id = hub.hand_off("deck")
    record = Record(
        schema=RECORD_SCHEMA_V2, id="brn_unfinished", kind="item", title="File the form", state="open",
        created_at=AT_0600, updated_at=AT_0600, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None,
        data={"level": "accepted", "size": "task"}, body="", source=None, links=(),
    )
    item = ItemRecord(record, "records/item/2026/09/brn_unfinished.md")
    context, recording, why_log = context_for(hub, tmp_path, brain_items=lambda: (item,))
    work01_waiting_history(why_log, request_id)
    why_log.record_surfacings([Surfacing(
        surfaced_at=AT_0600, channel="review", item_ref=item.record.id, record_sha256=item.sha256,
        record_state="open", rule="pick_or_park", fields_used={}, record_path=item.path,
    )])
    why_log.record_response(item_ref=item.record.id, response="picked_today", response_words="doing it", responded_at=AT_0600)
    hub.at(AT_0900).return_result(request_id, proof="https://example.test/deck")
    result = run_moments(context, AT_1000, "pc_on")
    assert result["item_ids"] == () and result["request_ids"] == (request_id,)
    assert recording.texts_to("hermes-wa") == ['1. Draft the deck — back, with proof: "https://example.test/deck" (with fleet-boss)']
    assert len(moment_rows(why_log, request_id)) == 1
    assert run_moments(context, AT_1000, "task_finished")["outcome"] == "nothing"
    assert len(recording.texts_to("hermes-wa")) == 1
    why_log.close()


def test_disabled_review_never_reads_request_state(tmp_path: Path) -> None:
    def never(request_ids: tuple[str, ...]):
        raise AssertionError("must not be called while [review] is disabled")

    context = ReviewContext(
        settings=ReviewSettings.from_table(None), why_log=None,
        sources=ReviewSources(request_status=never), service=Recording(Hub(tmp_path).service),
    )
    assert run_moments(context, AT_1000, "task_finished") == {"outcome": "disabled"}
    assert poll_once(context, AT_1000) == ()


def test_request_status_reader_returns_returned_requests_with_their_check(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    working = hub.hand_off("deck")
    unchecked = hub.hand_off("budget")
    accepted = hub.hand_off("plan")
    hub.at(AT_0900).return_result(unchecked)
    hub.return_result(accepted)
    hub.check(accepted)

    read = _review_request_status_reader(hub.service, hub.policy, "exosuit-review")
    assert read is not None
    statuses = {
        status.id: (status.work_state, status.check, status.handed_to, status.objective)
        for status in read((working, unchecked, accepted))
    }
    assert statuses == {
        unchecked: ("result_returned", "none", "fleet-boss", "Draft the budget"),
        accepted: ("result_returned", "accepted", "fleet-boss", "Draft the plan"),
    }
    # Only the requests asked about.
    assert [status.id for status in read((unchecked,))] == [unchecked]
    assert _review_request_status_reader(hub.service, hub.policy, "not-enrolled") is None


def test_open_requests_reader_names_requests_by_objective_newest_first_without_its_own_deliveries(
    tmp_path: Path,
) -> None:
    hub = Hub(tmp_path)
    older = hub.at(AT_0600 - timedelta(days=2)).hand_off("deck")  # 06:00 SGT, Thu 24 Sep
    newer = hub.at(AT_0600 - timedelta(hours=10)).hand_off("budget")  # 20:00 SGT, Fri 25 Sep
    context, recording, why_log = context_for(hub, tmp_path)
    run_review(context, hub.at(AT_0600).clock.now)  # its own delivery to Diablo is an open request too

    read = _review_open_requests_reader(hub.service, hub.policy, "exosuit-review")
    assert read is not None
    # `since` is the Singapore date: the older one was updated at 22:00 UTC on Wed 23 Sep.
    assert [(entry.id, entry.objective, entry.handed_to, entry.since.isoformat()) for entry in read()] == [
        (newer, "Draft the budget", "fleet-boss", "2026-09-25"),
        (older, "Draft the deck", "fleet-boss", "2026-09-24"),
    ]
    assert [(entry.work_state, entry.check, entry.state) for entry in read()] == [("requested", "none", "open")] * 2
    assert _review_open_requests_reader(hub.service, hub.policy, "not-enrolled") is None
    why_log.close()


def test_waiting_on_lists_ten_requests_by_objective_newest_first_then_a_count(tmp_path: Path) -> None:
    """The first live dry run (28 Sep) listed 56 raw request IDs. Proposed - Nic to confirm: at
    most 10, newest first, each by its objective, then "N more; ask to see them"."""

    hub = Hub(tmp_path)
    request_ids = [
        hub.at(AT_0600 - timedelta(days=1) + timedelta(minutes=n)).hand_off(f"task {n:02d}") for n in range(1, 13)
    ]
    context, recording, why_log = context_for(hub, tmp_path)
    run_review(context, hub.at(AT_0600).clock.now)

    (review_text,) = recording.texts_to("hermes-wa")
    assert "## Waiting on" not in review_text
    waiting_on = work01_review_text(context).split("## Waiting on\n")[1].split("\n## ")[0]
    assert waiting_on.splitlines() == [
        *(
            f"{number}. Draft the task {n:02d} — with fleet-boss since yesterday"
            for number, n in enumerate(range(12, 2, -1), start=1)
        ),
        "11. 2 more; ask to see them",
    ]
    for n, request_id in enumerate(request_ids, start=1):
        if n > 2:
            work01_waiting_history(why_log, request_id, objective=f"Draft the task {n:02d}")

    # The oldest was only counted, never shown, so its closing is not reported; the newest was
    # shown, so it comes back once as done.
    oldest, newest = request_ids[0], request_ids[-1]
    for request_id in (oldest, newest):
        hub.at(AT_0900).return_result(request_id)
        hub.check(request_id)
    sent = [result for result in poll_once(context, AT_1000) if result["outcome"] == "sent"]
    assert [result["request_ids"] for result in sent] == [(newest,)]
    assert recording.texts_to("hermes-wa")[-1] == "1. Draft the task 12 — done (was with fleet-boss)"
    why_log.close()


@pytest.mark.parametrize("review_enabled", [True, False])
def test_runtime_wires_the_request_reader_only_while_review_is_on(tmp_path: Path, review_enabled: bool) -> None:
    runtime = build_runtime(write_config(tmp_path, review_enabled=review_enabled), provider=FakeTransport(), start_worker=False)
    try:
        reader = runtime.review_sources.request_status
        if review_enabled:
            assert reader is not None
            assert reader(("req_unknown",)) == ()
        else:
            assert reader is None
    finally:
        runtime.close()
