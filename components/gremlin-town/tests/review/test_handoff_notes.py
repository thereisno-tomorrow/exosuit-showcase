"""The hub keeps the "where I stopped" note for handed-off work (Ciel rebuild slice 2, N12a; step
B, B6; rebuild plan gap G3).

Proven against a real `GremlinTownService`/`SQLiteStore`/`OutboxWorker` for the request's life
and a real `GitHubBrainHomeStorage` over Brain Core's own fake GitHub for the notes, so the real
publish path - revision checks, confirmed statuses - is what these tests exercise. No agent calls
a continuity tool anywhere here: every note write is the hub's own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest

from brain_core.github_storage import GitHubBrainHomeStorage, GitHubConflict, GitHubObjectCache
from brain_core.records import Record, render_record

from gremlin_town.contracts import (
    Actor, CheckOutcome, CheckRequest, CreateRequest, MessageKind, Packet, ProviderReference, ReplyRequest,
    ReportAction, ReportRequest, RequestMode,
)
from gremlin_town.domain import Participant, Policy
from gremlin_town.mcp.brain_runtime import (
    BrainOperation, ContinuityListRequest, ContinuityRecoverRequest, OwnerAuthorization, OwnerSelector,
)
from gremlin_town.mcp.github_continuity import GitHubContinuityProvider
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.handoff_notes import (
    CURSOR_KIND, HUB_ACTOR, RESULT_RETURNED_STEP, HandoffNoteWriteFailed, HandoffNoteWriter, change_for_event,
)
from gremlin_town.review.poll import poll_once
from gremlin_town.review.run import ReviewContext
from gremlin_town.review.settings import ReviewSettings, ReviewSettingsError
from gremlin_town.review.why_log import WhyLog
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore
from gremlin_town.workers import OutboxWorker
from tests.mcp.test_brain_composition import LANE_B

NOW = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)  # 10:00 SGT
TASK_ID = "brn_20260928_010000_abcd1234"
OWNER_ID, OWNER_LOCATOR = "brain-home", "vault://brain-home"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def tick(self, minutes: int = 5) -> datetime:
        self.now += timedelta(minutes=minutes)
        return self.now


class FakeProvider:
    def __init__(self) -> None:
        self.count = 0

    def send(self, envelope):
        self.count += 1
        return ProviderReference(f"message-{self.count}", "thread-handoff", NOW, envelope.parent_message_id)


class Hub:
    """A real GT hub and a real Brain Home storage, with the handoff-note keeper switched on."""

    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        policy = Policy(
            participants=(
                Participant("chatgpt-web", "ChatGPT", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
                Participant("fleet-boss", "Fleet Boss", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
                Participant(
                    "exosuit-review", "Exosuit review", "hub@example.test", frozenset({"vault_home"}),
                    frozenset({"private"}), read_all_requests=True,
                ),
                Participant("hermes-wa", "Diablo", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
            ),
            routes=frozenset({
                ("vault_home", "chatgpt-web", "fleet-boss", "private"),
                ("vault_home", "fleet-boss", "chatgpt-web", "private"),
                ("vault_home", "exosuit-review", "hermes-wa", "private"),
                ("vault_home", "hermes-wa", "exosuit-review", "private"),
            }),
        )
        self.store = SQLiteStore(tmp_path / "gt.db", now=self.clock)
        self.service = GremlinTownService(self.store, policy, now=self.clock)
        self.worker = OutboxWorker(self.store, policy, FakeProvider(), now=self.clock)
        self.github = LANE_B.FakeGitHub()
        self.storage = GitHubBrainHomeStorage(
            repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=self.github,
            object_cache=GitHubObjectCache(),
        )
        self.writer = HandoffNoteWriter(self.storage, owner_id=OWNER_ID, owner_locator=OWNER_LOCATOR)
        self.why_log = WhyLog(tmp_path / "why.sqlite3")
        self.context = ReviewContext(
            settings=ReviewSettings.from_table({
                "enabled": True, "project": "vault_home", "why_log_file": str(tmp_path / "why.sqlite3"),
                "handoff_notes": True,
            }),
            why_log=self.why_log,
            sources=ReviewSources(),
            service=self.service,
            handoff_notes=self.writer,
        )
        self._keys = 0

    def close(self) -> None:
        self.why_log.close()
        self.store.close()

    def key(self) -> str:
        self._keys += 1
        return f"op-{self._keys}"

    def poll(self) -> dict:
        self.clock.tick()
        return poll_once(self.context, self.clock())[-1]

    def revision(self, request_id: str) -> int:
        return self.service.requests_read(Actor("exosuit-review"), request_id).revision

    def create(self, *, task_id: str | None = TASK_ID, requester: str = "chatgpt-web", recipient: str = "fleet-boss") -> str:
        self.clock.tick(1)
        receipt = self.service.requests_create(Actor(requester), CreateRequest(
            self.key(), RequestMode.ACTION_REQUESTED, "vault_home", recipient,
            Packet(1, MessageKind.REQUEST, None, "Draft the Q3 deck", "For Sam", {}, (), "Draft it", "The deck", "private"),
            task_id=task_id,
        ))
        self.worker.dispatch_once()
        return receipt.request_id

    def report(self, request_id: str, action: ReportAction, reason: str | None = None, *, actor: str = "fleet-boss") -> None:
        self.clock.tick(1)
        self.service.requests_report(Actor(actor), ReportRequest(
            self.key(), request_id, self.revision(request_id), action, reason,
        ))

    def return_result(self, request_id: str, proof: str | None = None) -> None:
        self.clock.tick(1)
        self.service.requests_reply(Actor("fleet-boss"), ReplyRequest(
            self.key(), request_id, self.revision(request_id), MessageKind.RESULT,
            Packet(1, MessageKind.RESULT, None, "The deck", "Slides 1-12", {}, (), "", "", "private"),
            proof=proof,
        ))
        assert self.worker.dispatch_once().work_state.value == "result_returned"

    def check(self, request_id: str, outcome: CheckOutcome, evidence: str) -> None:
        self.clock.tick(1)
        self.service.requests_check(Actor("chatgpt-web"), CheckRequest(
            self.key(), request_id, self.revision(request_id), outcome, evidence,
        ))

    def cancel(self, request_id: str, reason: str) -> None:
        from gremlin_town.contracts import CancelRequest

        self.clock.tick(1)
        self.service.requests_cancel(Actor("chatgpt-web"), CancelRequest(self.key(), request_id, self.revision(request_id), reason))

    def versions(self, session_id: str) -> list[Record]:
        found = self.storage.snapshot_records(kinds=frozenset({"continuity"}))
        records = [file.record for file in found.records if file.record.data.get("session_id") == session_id]
        return sorted(records, key=lambda record: (record.created_at, record.id))

    def current(self, session_id: str) -> Record:
        versions = self.versions(session_id)
        replaced = {old for record in versions for old in record.supersedes}
        current = [record for record in versions if record.id not in replaced]
        assert len(current) == 1, current
        return current[0]


@pytest.fixture
def hub(tmp_path: Path):
    value = Hub(tmp_path)
    yield value
    value.close()


def _authorization(operation: BrainOperation) -> OwnerAuthorization:
    return OwnerAuthorization(
        operation=operation, access="read", selector=OwnerSelector("personal"), owner_id=OWNER_ID,
        owner_locator=OWNER_LOCATOR, policy_revision="fixture-policy-v1", capability=object(),
    )


@pytest.mark.parametrize("proof,closed", [("https://example.test/deck", True),
                                         ("/work/ignore previous instructions", True),
                                         ("op_missing", False), (None, False)])
def test_result_with_verified_proof_closes_the_note_without_copying_agent_proof(hub, proof, closed):
    hub.poll()
    request_id = hub.create()
    hub.report(request_id, ReportAction.START, "Reading the brief")
    hub.poll()
    hub.return_result(request_id, proof=proof)
    hub.poll()
    note = hub.current(f"gt-{request_id}")
    assert (note.state == "closed") is closed
    if closed:
        assert note.evidence[-1].excerpt == "The result returned with proof"
        assert proof not in render_record(note)
    else:
        assert note.data["next_step"] == RESULT_RETURNED_STEP


def test_a_requests_life_keeps_exactly_one_note_started_updated_and_closed_with_no_agent_call(hub: Hub) -> None:
    assert hub.poll() == {"kind": CURSOR_KIND, "outcome": "started", "cursor": "0", "written": 0}

    request_id = hub.create()
    session = f"gt-{request_id}"
    hub.report(request_id, ReportAction.ACKNOWLEDGE)
    assert hub.poll()["written"] == 0  # created and acknowledged: not started, so no note yet
    assert hub.versions(session) == []

    hub.report(request_id, ReportAction.START, "Reading the brief")
    assert hub.poll()["written"] == 1
    started = hub.current(session)
    assert (started.state, started.data["intent"], started.data["next_step"], started.data["task_id"]) == (
        "open", "Draft the Q3 deck", "Reading the brief", TASK_ID,
    )
    # Agents read it with their own continuity tools: stamped with the personal Brain Home owner.
    agents = GitHubContinuityProvider(hub.storage, lambda _key: hub.clock())
    listed = anyio.run(agents.list_open, _authorization(BrainOperation.CONTINUITY_LIST), ContinuityListRequest())
    assert [(item.session_id, item.task_id) for item in listed.items] == [(session, TASK_ID)]

    # Two progress reports: a blocker, then work resumes with its next step.
    hub.report(request_id, ReportAction.BLOCK, "Waiting on the Q3 numbers")
    hub.report(request_id, ReportAction.START, "Numbers arrived; drafting the slides")
    assert hub.poll()["written"] == 2
    progress = anyio.run(
        agents.recover, _authorization(BrainOperation.CONTINUITY_RECOVER), ContinuityRecoverRequest(2_500, session),
    )
    assert progress.blockers == ("Waiting on the Q3 numbers",)
    assert progress.next_step == "Numbers arrived; drafting the slides"

    hub.return_result(request_id)
    assert hub.poll()["written"] == 1
    assert hub.current(session).data["next_step"] == RESULT_RETURNED_STEP

    hub.check(request_id, CheckOutcome.ACCEPTED, "Slides 1-12 match the brief")
    assert hub.poll()["written"] == 1

    versions = hub.versions(session)
    closed = hub.current(session)
    assert closed.state == "closed"
    assert [record.data["next_step"] for record in versions] == [
        "Reading the brief", "Reading the brief", "Numbers arrived; drafting the slides",
        RESULT_RETURNED_STEP, RESULT_RETURNED_STEP,
    ]
    assert len(versions) == len(hub.github.patch_payloads) == 5  # one note, five confirmed writes
    # The hub wrote every version as itself, never as an agent.
    assert {item.actor for item in closed.evidence} == {HUB_ACTOR}
    assert closed.evidence[-1].excerpt == "chatgpt-web checked the result and accepted it: Slides 1-12 match the brief"
    assert (closed.owners, closed.vaults) == ((OWNER_ID,), (OWNER_LOCATOR,))
    assert anyio.run(agents.list_open, _authorization(BrainOperation.CONTINUITY_LIST), ContinuityListRequest()).items == ()

    # Nothing new: a later poll writes nothing.
    assert hub.poll()["written"] == 0
    assert len(hub.github.patch_payloads) == 5


def test_a_replayed_event_makes_no_second_write(hub: Hub) -> None:
    hub.poll()
    request_id = hub.create()
    hub.report(request_id, ReportAction.START, "Reading the brief")
    hub.report(request_id, ReportAction.BLOCK, "Waiting on the Q3 numbers")
    assert hub.poll()["written"] == 2
    writes = len(hub.github.patch_payloads)

    # A crash after the writes but before the cursor was saved replays the same events.
    hub.why_log.record_run(kind=CURSOR_KIND, started_at=hub.clock(), last_event_cursor="0", outcome="rewound for the test")
    replay = hub.poll()
    assert replay["outcome"] == "checked" and replay["written"] == 0
    assert len(hub.github.patch_payloads) == writes

    # The same holds one event at a time: the writer finds the event's own key and skips it.
    event = hub.service.events_list(Actor("exosuit-review"), {"event_type": "work_block"}).items[0]
    request = hub.service.requests_list(Actor("exosuit-review"), {"request_id": request_id}).items[0]
    assert hub.writer.apply(change_for_event(event, request)) == "already_written"
    assert len(hub.github.patch_payloads) == writes


def test_a_failed_write_is_recorded_and_retried_on_the_next_poll(hub: Hub, caplog) -> None:
    hub.poll()
    request_id = hub.create()
    hub.report(request_id, ReportAction.START, "Reading the brief")

    real_publish = hub.storage.publish_record
    failures = {"left": 1}

    def failing_publish(**kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise GitHubConflict("github_record_conflict", "remote record path contains different content")
        return real_publish(**kwargs)

    hub.storage.publish_record = failing_publish  # type: ignore[method-assign]
    failed = hub.poll()
    assert failed["outcome"] == "failed"
    assert failed["failure"].startswith("work_start at ") and "HandoffNoteWriteFailed" in failed["failure"]
    assert hub.versions(f"gt-{request_id}") == []
    runs = hub.why_log._connection.execute(
        "SELECT last_event_cursor, outcome FROM runs WHERE kind=? ORDER BY id", (CURSOR_KIND,),
    ).fetchall()
    assert runs[-1]["outcome"].startswith("failed: work_start at ")
    assert "the next poll retries it" in caplog.text

    retried = hub.poll()
    assert (retried["outcome"], retried["written"]) == ("checked", 1)
    assert hub.current(f"gt-{request_id}").data["next_step"] == "Reading the brief"


def test_a_branch_that_moved_since_the_read_is_read_again_and_the_write_retried(hub: Hub) -> None:
    hub.poll()
    request_id = hub.create()
    hub.report(request_id, ReportAction.START, "Reading the brief")

    real_publish = hub.storage.publish_record
    raced = {"done": False}

    def racing_publish(**kwargs):
        if not raced["done"]:
            raced["done"] = True
            hub.github.seed({"records/fact/2026/09/unrelated.md": b"someone else's write"})
        return real_publish(**kwargs)

    hub.storage.publish_record = racing_publish  # type: ignore[method-assign]
    assert hub.poll()["written"] == 1
    assert hub.current(f"gt-{request_id}").state == "open"


def test_cancelled_and_declined_requests_close_their_note_and_say_why(hub: Hub) -> None:
    hub.poll()
    cancelled = hub.create()
    hub.report(cancelled, ReportAction.START)
    declined = hub.create(task_id=None)
    hub.report(declined, ReportAction.START, "Looking")
    assert hub.poll()["written"] == 2
    assert hub.current(f"gt-{cancelled}").data["next_step"] == ""  # started with no reason: no step
    assert "task_id" not in hub.current(f"gt-{declined}").data  # a request naming no task

    hub.cancel(cancelled, "Sam no longer needs it")
    hub.report(declined, ReportAction.DECLINE, "Not my area")
    assert hub.poll()["written"] == 2
    assert hub.current(f"gt-{cancelled}").state == "closed"
    assert hub.current(f"gt-{cancelled}").evidence[-1].excerpt == "chatgpt-web cancelled the request: Sam no longer needs it"
    assert hub.current(f"gt-{declined}").evidence[-1].excerpt == "fleet-boss declined the request: Not my area"


def test_a_rejected_check_keeps_the_note_open_and_says_so(hub: Hub) -> None:
    hub.poll()
    request_id = hub.create()
    hub.report(request_id, ReportAction.START)
    hub.return_result(request_id)
    hub.check(request_id, CheckOutcome.REJECTED, "Slide 4 is missing")
    assert hub.poll()["written"] == 3
    note = hub.current(f"gt-{request_id}")
    assert note.state == "open"
    assert note.data["next_step"] == "Result checked and not accepted: Slide 4 is missing"


def test_no_note_for_requests_without_a_start_or_for_the_hubs_own_deliveries(hub: Hub) -> None:
    hub.poll()
    # Returned with no start report: the request itself shows it is open; no note is invented.
    quick = hub.create()
    hub.return_result(quick)
    # The 06:00 review and daytime updates are the hub's own sends, not handed-off work.
    delivery = hub.create(task_id=None, requester="exosuit-review", recipient="hermes-wa")
    hub.report(delivery, ReportAction.START, actor="hermes-wa")
    result = hub.poll()
    assert (result["outcome"], result["written"]) == ("checked", 0)
    assert hub.github.patch_payloads == []


def test_an_agents_own_note_for_the_request_is_kept_not_duplicated(hub: Hub) -> None:
    """B4 still tells an agent to start gt-<request_id> itself; the hub then keeps that same note."""

    from gremlin_town.mcp.brain_runtime import ContinuityStartRequest

    hub.poll()
    request_id = hub.create()
    agents = GitHubContinuityProvider(hub.storage, lambda _key: hub.clock())
    write = OwnerAuthorization(
        operation=BrainOperation.CONTINUITY_START, access="write", selector=OwnerSelector("personal"),
        owner_id=OWNER_ID, owner_locator=OWNER_LOCATOR, policy_revision="fixture-policy-v1", capability=object(),
    )
    anyio.run(agents.start, write, ContinuityStartRequest("agent-start", f"gt-{request_id}", "Draft the deck", ()))
    hub.report(request_id, ReportAction.START, "Drafting now")
    hub.report(request_id, ReportAction.BLOCK, "Waiting on numbers")
    assert hub.poll()["written"] == 2
    note = hub.current(f"gt-{request_id}")
    assert (note.data["intent"], note.data["next_step"], note.data["blockers"]) == (
        "Draft the deck", "Drafting now", ["Waiting on numbers"],
    )
    assert len({record.id for record in hub.versions(f"gt-{request_id}")}) == 3


def test_handoff_notes_is_off_by_default_and_needs_review_enabled() -> None:
    assert ReviewSettings.from_table(None).handoff_notes is False
    assert ReviewSettings.from_table({"enabled": True, "why_log_file": "why.sqlite3"}).handoff_notes is False
    with pytest.raises(ReviewSettingsError, match="requires enabled"):
        ReviewSettings.from_table({"handoff_notes": True})
    with pytest.raises(ReviewSettingsError, match="boolean"):
        ReviewSettings.from_table({"enabled": True, "why_log_file": "why.sqlite3", "handoff_notes": "yes"})


def test_with_handoff_notes_off_the_poll_reads_and_writes_no_note(hub: Hub) -> None:
    from dataclasses import replace

    off = replace(hub.context, settings=replace(hub.context.settings, handoff_notes=False))
    request_id = hub.create()
    hub.report(request_id, ReportAction.START, "Reading the brief")
    hub.clock.tick()
    assert all(result.get("kind") != CURSOR_KIND for result in poll_once(off, hub.clock()))
    assert hub.why_log.latest_cursor(CURSOR_KIND) is None
    assert hub.github.patch_payloads == []


def test_an_unconfirmed_publish_is_a_clear_failure(hub: Hub) -> None:
    class Unconfirmed:
        status = "mystery"

    hub.poll()
    request_id = hub.create()
    hub.report(request_id, ReportAction.START)
    hub.storage.publish_record = lambda **kwargs: Unconfirmed()  # type: ignore[method-assign]
    event = hub.service.events_list(Actor("exosuit-review"), {"event_type": "work_start"}).items[0]
    request = hub.service.requests_list(Actor("exosuit-review"), {"request_id": request_id}).items[0]
    with pytest.raises(HandoffNoteWriteFailed, match="unexpected publish outcome"):
        hub.writer.apply(change_for_event(event, request))


def test_notes_past_the_scan_cap_say_so_plainly_in_the_runs_table_and_the_journal(hub: Hub, caplog) -> None:
    """The storage-limit plan, layer 1: a note write that can't read Brain Home's notes, past the cap, is
    recorded and logged in plain words, as a failure that will keep failing until Brain Home is tidied up."""

    from dataclasses import replace

    hub.poll()
    hub.github.seed({f"records/continuity/2026/09/brn_20260921_040000_{index:08x}.md": b"x" for index in range(2)})
    capped = HandoffNoteWriter(hub.storage, owner_id=OWNER_ID, owner_locator=OWNER_LOCATOR, max_records=1)
    hub.context = replace(hub.context, handoff_notes=capped)
    request_id = hub.create()
    hub.report(request_id, ReportAction.START, "Reading the brief")

    failed = hub.poll()

    sentence = (
        "Brain Home holds 2 note records and the hub reads at most 1 at once. Nothing was saved; what's already "
        "saved is safe. Ask for the storage tidy-up now."
    )
    assert failed["outcome"] == "failed"
    assert failed["failure"].startswith("work_start at ") and failed["failure"].endswith(sentence)
    runs = hub.why_log._connection.execute(
        "SELECT outcome FROM runs WHERE kind=? ORDER BY id", (CURSOR_KIND,),
    ).fetchall()
    assert runs[-1]["outcome"] == f"failed: {failed['failure']}"
    assert "Handoff note write failed and will keep failing until fixed" in caplog.text
    assert sentence in caplog.text
