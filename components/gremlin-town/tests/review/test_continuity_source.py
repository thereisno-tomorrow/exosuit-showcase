"""Starting orientation's running note through GitHubContinuityStarter (step F spec §6).

Uses a trivial fake satisfying `continuity_source.ContinuityPublisher` structurally, not a
fake GitHub HTTP transport - `GitHubBrainHomeStorage.publish_record` itself (conflict
handling, lost-reply reconciliation) is already proven in Brain Core's own
`tests/unit/test_github_storage.py`. These tests prove only this module's own idempotency
and error-handling logic: check-then-create, never overwrite, never swallow a real failure.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from brain_core.github_storage import GitHubConflict, GitHubPublicationUncertain, GitHubRecordFile, GitHubRecordRead, GitHubRevision
from brain_core.records import RECORD_SCHEMA_V1, Record, parse_record

from gremlin_town.review.continuity_source import ContinuityWriteFailed, GitHubContinuityStarter

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
REVISION = GitHubRevision(repository="trusted/brain-home", branch="accepted", commit_sha="c" * 40, tree_sha="t" * 40)
TASK_ID = "brn_20260926_120000_aabb"


def continuity_record(record_id: str, *, session_id: str, next_step: str, state: str = "open") -> Record:
    return Record(
        schema=RECORD_SCHEMA_V1,
        id=record_id,
        kind="continuity",
        title=f"Continuity for {session_id}",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        assertion="explicit",
        confidence=1.0,
        sensitivity="private",
        vaults=(),
        owners=("nic",),
        entities=(),
        evidence=(),
        supersedes=(),
        dedupe_key=f"continuity:{session_id}",
        data={"session_id": session_id, "intent": "fixture", "next_step": next_step},
        body="",
    )


class Published:
    def __init__(self, status: str) -> None:
        self.status = status


class FakeStorage:
    """Satisfies `continuity_source.ContinuityPublisher` structurally: no HTTP, no GitHub API."""

    def __init__(self, existing: tuple[Record, ...] = ()) -> None:
        self.records: list[Record] = list(existing)
        self.publish_calls: list[bytes] = []
        self.publish_result: object = Published("created")
        self.publish_error: Exception | None = None
        self.limits: list[tuple[int, int | None]] = []

    def snapshot_records(
        self, *, kinds: frozenset[str], max_records: int = 512, max_bytes: int | None = None,
    ) -> GitHubRecordRead:
        self.limits.append((max_records, max_bytes))
        matching = tuple(
            GitHubRecordFile(f"records/{r.kind}/2026/09/{r.id}.md", b"", "blob-sha", r)
            for r in self.records
            if r.kind in kinds
        )
        return GitHubRecordRead(REVISION, matching)

    def read_records(self, paths, *, archived_ok: bool = False) -> GitHubRecordRead:
        paths = set(paths)
        matching = tuple(
            GitHubRecordFile(f"records/{r.kind}/2026/09/{r.id}.md", b"", "blob-sha", r)
            for r in self.records
            if f"records/{r.kind}/2026/09/{r.id}.md" in paths
        )
        return GitHubRecordRead(REVISION, matching)

    def publish_record(self, *, record_bytes: bytes) -> object:
        self.publish_calls.append(record_bytes)
        if self.publish_error is not None:
            raise self.publish_error
        # A real publish makes the record visible to the next read; simulate that so an
        # idempotency check right after a real (non-overridden) call behaves correctly.
        self.records.append(parse_record(record_bytes.decode("utf-8"), "fixture publish"))
        return self.publish_result


# --- Creates a new note when none exists ---------------------------------------------


def test_start_creates_a_new_note_when_none_exists():
    storage = FakeStorage()
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="drop the pricing into slide 6")

    assert len(storage.publish_calls) == 1
    rendered = storage.publish_calls[0].decode("utf-8")
    assert "item-brn_task" in rendered
    assert "drop the pricing into slide 6" in rendered
    assert "kind: continuity" in rendered


# --- Idempotent: an existing open note is never overwritten ----------------------------


def test_start_is_idempotent_when_an_open_note_already_exists():
    existing = continuity_record("brn_existing", session_id="item-brn_task", next_step="already started")
    storage = FakeStorage((existing,))
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    result = starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="a different first step")

    assert storage.publish_calls == []
    assert result is existing


def test_start_ignores_a_closed_note_with_the_same_session_id():
    closed = continuity_record("brn_old", session_id="item-brn_task", next_step="done long ago", state="closed")
    storage = FakeStorage((closed,))
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="start again")

    assert len(storage.publish_calls) == 1


# --- A concurrent race is still idempotent, not an error --------------------------------


def test_start_treats_a_concurrent_record_conflict_as_the_same_idempotent_success():
    storage = FakeStorage()
    winner = continuity_record("brn_winner", session_id="item-brn_task", next_step="the other caller's step")

    def publish_record(*, record_bytes: bytes) -> object:
        storage.publish_calls.append(record_bytes)
        storage.records.append(winner)  # the concurrent caller's write landed first
        raise GitHubConflict("github_record_conflict", "different content at that path")

    storage.publish_record = publish_record  # type: ignore[method-assign]
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    result = starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="my own first step")

    assert result is winner


# --- Real failures are never swallowed (step A's guarantee) -----------------------------


def test_start_raises_when_the_record_conflict_leaves_no_resolvable_note():
    storage = FakeStorage()
    storage.publish_error = GitHubConflict("github_record_conflict", "different content, no note found either")
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    with pytest.raises(ContinuityWriteFailed):
        starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="a first step")


def test_start_raises_on_an_unrelated_conflict_without_reinterpreting_it():
    storage = FakeStorage()
    storage.publish_error = GitHubConflict("github_revision_conflict", "branch advanced")
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    with pytest.raises(ContinuityWriteFailed):
        starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="a first step")


def test_start_propagates_an_uncertain_publish_without_swallowing_it():
    storage = FakeStorage()
    storage.publish_error = GitHubPublicationUncertain("github_publication_uncertain", "lost reply, no reconciled commit")
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    with pytest.raises(GitHubPublicationUncertain):
        starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="a first step")


def test_start_raises_on_an_unexpected_publish_status():
    storage = FakeStorage()
    storage.publish_result = Published("mystery-status")
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)

    with pytest.raises(ContinuityWriteFailed):
        starter.start(session_id="item-brn_task", actor="chatgpt-web", next_step="a first step")


# --- End to end: respond("yes") actually starts the note, not just a no-op -------------


def test_respond_yes_actually_publishes_the_note_through_the_wired_adapter():
    """W14: a tier B yes logs its answer without invoking either personal writer."""

    from unittest.mock import Mock, patch
    from brain_core.records import RECORD_SCHEMA_V2
    from gremlin_town.review.brain_source import brain_items_by_path_reader, brain_items_reader
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.orient import OrientationContext, OrientationSources, respond, what_next
    from gremlin_town.review.settings import OrientationSettings, ReviewSettings
    from gremlin_town.review.why_log import WhyLog

    task = Record(
        schema=RECORD_SCHEMA_V2, id=TASK_ID, kind="item", title="send the deck", state="open",
        created_at=NOW, updated_at=NOW, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None,
        data={"level": "accepted", "size": "task", "timing": {"due": "2026-09-26"}}, body="", source=None, links=(),
    )
    storage = FakeStorage((task,))
    why_log = WhyLog(":memory:")
    starter = GitHubContinuityStarter(storage, now=lambda: NOW)
    work_door = Mock(side_effect=AssertionError("tier B must not invoke the work door"))
    context = OrientationContext(
        orientation_settings=OrientationSettings.from_table({"enabled": True}, review_enabled=True),
        review_settings=ReviewSettings.from_table({"enabled": True, "why_log_file": ":memory:"}),
        why_log=why_log,
        sources=OrientationSources(
            review=ReviewSources(
                brain_items=brain_items_reader(storage), brain_items_by_path=brain_items_by_path_reader(storage)
            )
        ),
        continuity=starter,
        work_door=work_door,
    )

    answer = what_next(context, NOW)
    (before,) = why_log.full_reason(answer["surfacing_id"])
    reads_before = list(storage.limits)
    with patch.object(starter, "start", wraps=starter.start) as start:
        result = respond(context, NOW, actor="chatgpt-web", surfacing_id=answer["surfacing_id"], response="yes")
        assert result == {"outcome": "recorded", "response": "yes"}
        (row,) = why_log.full_reason(answer["surfacing_id"])
        assert (row.item_ref, row.channel, row.response, row.response_words, row.responded_at) == (
            TASK_ID, "orient", "yes", None, NOW)
        assert (row.record_path, row.record_sha256) == (before.record_path, before.record_sha256)

        # Replay updates the same answer, with no personal note or work publication.
        again = respond(context, NOW, actor="chatgpt-web", surfacing_id=answer["surfacing_id"], response="yes")
        assert again == result
        assert why_log.full_reason(answer["surfacing_id"]) == (row,)
        start.assert_not_called()
    work_door.assert_not_called()
    assert storage.records == [task]
    assert not any(record.kind == "continuity" for record in storage.records)
    assert storage.publish_calls == []
    assert storage.limits == reads_before
    why_log.close()


# --- N12b/N12c (Ciel slice 2) ------------------------------------------------------------


def test_start_names_the_task_takes_an_empty_first_step_and_stamps_the_owner():
    storage = FakeStorage()
    starter = GitHubContinuityStarter(storage, now=lambda: NOW, owner_id="brain-home", owner_locator="vault://brain-home")

    starter.start(session_id=f"item-{TASK_ID}", actor="chatgpt-web", next_step="", task_id=TASK_ID, intent="Call the bank")

    note = parse_record(storage.publish_calls[0].decode("utf-8"), "published")
    assert note.data["task_id"] == TASK_ID
    assert note.data["next_step"] == ""
    assert note.data["intent"] == "Call the bank"
    # Stamped like Brain Home's own continuity tools, so agents' bc_continuity_list sees it.
    assert note.owners == ("brain-home",)
    assert note.vaults == ("vault://brain-home",)


def test_an_open_note_already_linked_to_the_task_counts_whatever_it_is_called():
    linked = continuity_record("brn_20260926_115900_aa01", session_id="deck-for-sam", next_step="Ask Sam")
    linked = replace(linked, data={**linked.data, "task_id": TASK_ID})
    handed_off = continuity_record("brn_20260926_115900_aa02", session_id="gt-req_0123", next_step="Waiting")
    handed_off = replace(handed_off, data={**handed_off.data, "task_id": TASK_ID})

    storage = FakeStorage((handed_off, linked))
    result = GitHubContinuityStarter(storage, now=lambda: NOW).start(
        session_id=f"item-{TASK_ID}", actor="chatgpt-web", next_step="", task_id=TASK_ID,
    )
    assert result is linked
    assert storage.publish_calls == []

    # A handed-off request's note (gt-...) is another agent's work, never this task's own note.
    only_handed_off = FakeStorage((handed_off,))
    GitHubContinuityStarter(only_handed_off, now=lambda: NOW).start(
        session_id=f"item-{TASK_ID}", actor="chatgpt-web", next_step="", task_id=TASK_ID,
    )
    assert len(only_handed_off.publish_calls) == 1


def test_a_note_whose_newest_version_is_closed_is_not_still_open():
    """Brain Home keeps every version of a note: the older, open versions of a closed note must
    not make a later yes think the note is still open (fixed 28 Sep)."""

    first = continuity_record("brn_20260926_110000_aa01", session_id=f"item-{TASK_ID}", next_step="step one")
    closed = replace(
        continuity_record("brn_20260926_113000_aa02", session_id=f"item-{TASK_ID}", next_step="step one", state="closed"),
        supersedes=("brn_20260926_110000_aa01",),
    )
    storage = FakeStorage((first, closed))
    GitHubContinuityStarter(storage, now=lambda: NOW).start(
        session_id=f"item-{TASK_ID}", actor="chatgpt-web", next_step="start again",
    )
    assert len(storage.publish_calls) == 1
