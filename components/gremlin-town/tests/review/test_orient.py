"""`[orientation]`'s orchestration (Ciel slice 4): "where am I?" (`overview`), "what next?"
(`what_next`) and Nic's answer to a pick (`respond`).

Disabled reads and writes nothing (step F spec §2). Enabled, these prove PRD cases 4 (the whole
picture, ranked, recommending nothing; a top pick and two alternatives, important over fun), 5
(a time box, not a plan), 6 (one yes, one tracked item with its first step; one caution), 14
(the notes out of reach) and 17's ranking half (started beats new; stuck marked; finished listed)
at the function level with fakes - the same level `run.py`'s tests prove the 06:00 review at.
Project work (N4; slice 8, part 2): the task-sitrep reader wired, with `[sitreps]` off and on.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from brain_core.orientation import OrientationSignal
from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import CalendarEvent, ItemRecord, OpenNote, OpenRequest
from brain_core.sitreps import ProjectTasks, TaskSitrep

from gremlin_town.contracts import Actor
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.orient import OrientationContext, OrientationSources, overview, respond, what_next
from gremlin_town.review.settings import OrientationSettings, ReviewSettings
from gremlin_town.review.why_log import Surfacing, WhyLog

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)  # 14:00 SGT, Sun 4 Oct 2026
SGT = timezone(timedelta(hours=8))


def test_round2_fixture_next_alternative_keeps_parent_and_raw_audit_pin(tmp_path):
    import importlib.util
    from brain_core import work

    path = Path(__file__).resolve().parents[3] / "brain-core/tests/fixtures/work_model/moved_over.py"
    spec = importlib.util.spec_from_file_location("round2_orient_fixture", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    records = fixture.moved_over_records()
    venue_rental = work.index_things(records)[fixture.VENUE_RENTAL]
    venue_rental = replace(venue_rental, data={**venue_rental.data, "stake": {**venue_rental.data["stake"], "confirmed": True},
                                       "review_marks": []})
    parts = tuple(replace(make_item(id=f"brn_round2_part_{n}", title=title), data={
        "level": "accepted", "parent": fixture.VENUE_RENTAL, "order": n})
        for n, title in enumerate(("a venue appointment", "a inspector's approval", "the co-organizer's signature"), 1))
    top = make_item(id="brn_round2_top", title="Other pinned work", due="2026-10-02")
    top = replace(top, data={**top.data, "stake": {"level": "changes_life", "confirmed": True}})
    ctx, log = context(tmp_path=tmp_path, items=tuple(wrap(record) for record in (*records, venue_rental, *parts, top)))
    try:
        answer = what_next(ctx, NOW.replace(day=2))
        assert answer["top_pick"]["record_id"] == top.id
        alternative = next(row for row in answer["alternatives"] if row["record_id"] == parts[0].id)
        assert alternative["belongs_to"] == venue_rental.title and alternative["progress"]["text"] == "0 of 3"
        assert not {part.id for part in parts[1:]} & {
            row["record_id"] for row in (*answer["alternatives"], *answer["can_wait"])}
        row = log.full_reason(parts[0].id)[-1]
        assert row.record_sha256 == wrap(parts[0]).sha256 and row.record_path == wrap(parts[0]).path
    finally:
        log.close()


def enabled_settings() -> OrientationSettings:
    return OrientationSettings.from_table({"enabled": True}, review_enabled=True)


def review_settings(why_log_file: str) -> ReviewSettings:
    return ReviewSettings.from_table({"enabled": True, "why_log_file": why_log_file})


def make_item(
    *,
    id: str,
    title: str = "Task",
    state: str = "open",
    level: str = "accepted",
    owed_to: str | None = None,
    due: str | None = None,
    at: str | None = None,
    repeat: str | None = None,
    links: tuple[dict[str, str], ...] = (),
    created_at: datetime = NOW,
    updated_at: datetime | None = None,
    check: dict[str, str] | None = None,
    importance: dict[str, object] | None = None,
) -> Record:
    timing: dict = {}
    if due is not None:
        timing["due"] = due
    if at is not None:
        timing["at"] = at
    data: dict = {"level": level, "size": "task", "timing": timing}
    if owed_to is not None:
        data["owed_to"] = owed_to
    if repeat is not None:
        data["repeat"] = repeat
    if check is not None:
        data["check"] = check
    if importance is not None:
        data["importance"] = importance
    return Record(
        schema=RECORD_SCHEMA_V2,
        id=id,
        kind="item",
        title=title,
        state=state,
        created_at=created_at,
        updated_at=updated_at or created_at,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=(),
        supersedes=(),
        dedupe_key=None,
        data=data,
        body="",
        source=None,
        links=links,
    )


def focus_preference(projects: list[str]) -> Record:
    return Record(
        schema=RECORD_SCHEMA_V2, id="brn_focus", kind="preference", title="Focus projects", state="active",
        created_at=NOW, updated_at=NOW, assertion=None, confidence=None, sensitivity="private", vaults=(),
        owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None, data={"focus_projects": projects},
        body="", source={"said_by": "owner", "came_by": "direct", "words": "my focus", "recorded_by": "hermes-wa"},
        links=(),
    )


def wrap(record: Record, *, path: str | None = None) -> ItemRecord:
    return ItemRecord(record=record, path=path or f"records/item/2026/10/{record.id}.md")


def note(task_id: str | None, *, session_id: str | None = None, next_step: str = "open the file",
         updated_at: datetime = NOW) -> OpenNote:
    return OpenNote(session_id=session_id or f"item-{task_id}", next_step=next_step, updated_at=updated_at, task_id=task_id)


class _AssertNeverCalled:
    def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("must not be called")

    def start(self, **kwargs: object) -> object:
        raise AssertionError("must not start a note")


class FakeContinuity:
    """Idempotent by contract (orient.py's `ContinuityStarter`): starting the same session twice
    is a no-op, never an error - exactly what a real adapter wrapping
    `brain_core.continuity.start_continuity` must guarantee by catching its own RECORD_EXISTS."""

    def __init__(self) -> None:
        self.notes: dict[str, dict[str, object]] = {}
        self.calls = 0

    def start(
        self, *, session_id: str, actor: str, next_step: str, task_id: str | None = None, intent: str | None = None,
    ) -> object:
        self.calls += 1
        self.notes.setdefault(session_id, {"actor": actor, "next_step": next_step, "task_id": task_id, "intent": intent})
        return self.notes[session_id]


def context(
    *,
    tmp_path: Path,
    items: tuple[ItemRecord, ...] = (),
    notes=(),
    continuity: FakeContinuity | None = None,
    preferences=None,
    calendar_events=None,
    open_requests=None,
    brain_facts=None,
    task_sitreps=None,
) -> tuple[OrientationContext, WhyLog]:
    why_log_path = tmp_path / "why.sqlite3"
    why_log = WhyLog(why_log_path)
    notes_reader = notes if callable(notes) else (lambda: tuple(notes))
    sources = OrientationSources(
        review=ReviewSources(
            brain_items=lambda: items,
            brain_items_by_path=lambda paths: tuple(item for item in items if item.path in paths),
            open_notes=notes_reader,
            # Connected but empty unless a test says otherwise, so "Couldn't see" names only failures.
            calendar_events=calendar_events or (lambda start, end: ()),
            open_requests=open_requests or (lambda: ()),
            brain_facts=brain_facts,
        ),
        preferences=preferences,
        task_sitreps=task_sitreps,
    )
    ctx = OrientationContext(
        orientation_settings=enabled_settings(),
        review_settings=review_settings(str(why_log_path)),
        why_log=why_log,
        sources=sources,
        continuity=continuity,
    )
    return ctx, why_log


def lines(answer: dict) -> list[tuple[str, str]]:
    return [(section["section"], line["text"]) for section in answer["sections"] for line in section["lines"]]


# --- Disabled by default ----------------------------------------------------------------------------


def test_orientation_disabled_by_default_reads_and_writes_nothing() -> None:
    guard = _AssertNeverCalled()
    ctx = OrientationContext(
        orientation_settings=OrientationSettings.from_table(None, review_enabled=False),
        review_settings=ReviewSettings.from_table(None),
        why_log=None,
        sources=OrientationSources(
            review=ReviewSources(brain_items=guard, calendar_events=guard, open_requests=guard, open_notes=guard),
            open_notes=guard, preferences=guard, task_sitreps=guard,
        ),
        continuity=guard,
    )
    assert overview(ctx, NOW) == {"outcome": "disabled"}
    assert what_next(ctx, NOW) == {"outcome": "disabled"}
    assert respond(ctx, NOW, actor="chatgpt-web", surfacing_id="brn_x", response="yes") == {"outcome": "disabled"}


class FakeWorkDoor:
    """Local host fake using the real moves and reader, with no storage adapter."""

    def __init__(self, item: Record):
        self.item = item
        self.calls = []
        self.records = ()
        self.flags = ()

    def __call__(self, caller, request):
        from dataclasses import asdict
        from hashlib import sha256
        from brain_core import moves, work
        from brain_core.contracts import stable_hash

        self.calls.append((caller, request))
        change = asdict(request)
        change.pop("check_only")
        # The host-only proof is checked by the real Brain provider before it
        # constructs the core change; it is not a field in the core move shape.
        change.pop("expected_records")
        if change["speaker"] is None:
            change.pop("speaker")
        ids = {(0, j): "brn_20261004_060000_" + sha256(f"{request.operation_id}:{j}".encode()).hexdigest()[:16]
               for j in range(25)}
        change["_context"] = moves.ExecutionContext(
            raw_records={self.item.id: self.item}, write_index=work.write_index((self.item,)),
            planned_ids=ids, operation_hash=stable_hash(change),
        )
        result = moves.apply_change(work.index_things((self.item,)), change, actor=caller.subject, now=NOW)
        assert result.refusal is None, result.refusal
        self.records, self.flags = result.records, result.flags
        return result.reply


def _start_fixture(tmp_path):
    from hashlib import sha256
    from brain_core.records import record_from_mapping
    from gremlin_town.mcp.brain_runtime import BrainCaller

    item = make_item(id="brn_20261004_060000_0000000000000001", title="Sort the photos")
    raw = item.as_dict()
    raw.update(source={"said_by": "owner", "came_by": "direct", "words": "Sort the photos",
                       "recorded_by": "hermes-wa"}, vaults=["vault://brain-home"], owners=["brain-home"],
               evidence=[{"source_type": "session_message", "locator": "fixture",
                          "excerpt": "Sort the photos", "content_sha256": sha256(b"Sort the photos").hexdigest()}])
    item = record_from_mapping(raw, "W14 Start fixture")
    ctx, why_log = context(tmp_path=tmp_path, items=(wrap(item),), continuity=_AssertNeverCalled())
    door = FakeWorkDoor(item)
    ctx = replace(ctx, work_door=door)
    pick = what_next(ctx, NOW)
    caller = BrainCaller("hermes-wa", frozenset({"bc:read", "bc:write"}))
    return ctx, why_log, door, pick["surfacing_id"], caller


def test_w14_start_fixture_matches_record_envelope(tmp_path):
    from brain_core import work
    ctx, log, door, ref, caller = _start_fixture(tmp_path)
    own = work.write_index((door.item,))[ref]
    assert own.evidence and own.vaults == ("vault://brain-home",)
    assert own.owners == ("brain-home",)
    assert caller.scopes == frozenset({"bc:read", "bc:write"})
    log.close()


def test_w14_tool_passes_authenticated_async_door_boundary(tmp_path):
    from gremlin_town.mcp.brain_tools import build_orientation_tools
    from gremlin_town.mcp.brain_runtime import BrainCaller, WorkRequest
    ctx, log, _, ref, _ = _start_fixture(tmp_path)
    calls = []
    async def callback(caller: BrainCaller, request: WorkRequest):
        calls.append((caller, request))
        return {"status": "accepted"}
    async def authorize():
        return None
    tool = next(tool for tool in build_orientation_tools(
        replace(ctx, work_door=callback), lambda: Actor("hermes-wa"),
        scopes_provider=lambda: {"bc:read", "bc:write"}, authorize=authorize)
        if tool.name == "bc_next_respond")
    answer = asyncio.run(tool.run({"surfacing_id": ref, "response": "yes", "said_by": "owner",
                                   "message": "I am starting to sort the photos"}))
    assert not answer.isError, answer.structuredContent
    caller, request = calls[0]
    assert caller == BrainCaller("hermes-wa", frozenset({"bc:read", "bc:write"}))
    assert isinstance(request, WorkRequest) and request.message == "I am starting to sort the photos"
    assert request.moves == ({"move": "start", "thing": ref},)
    log.close()


@pytest.mark.parametrize("response", ["yes", "other"])
def test_w14_diablo_starts_through_door_without_a_note(tmp_path, response):
    ctx, log, door, ref, caller = _start_fixture(tmp_path)
    answer = respond(ctx, NOW, actor=caller, surfacing_id=ref, response=response,
                     target=ref if response == "other" else None, said_by="owner",
                     message="I am starting to sort the photos", words="starting to sort the photos",
                     first_step="An invented agent action")
    assert answer["status"] == "accepted"
    assert door.records[0].data["started_at"] == NOW.isoformat()
    assert "next_action" not in door.records[0].data
    assert door.calls[0][0] == caller
    assert door.calls[0][1].message == "I am starting to sort the photos"
    assert door.calls[0][1].moves == ({"move": "start", "thing": ref, "words": "starting to sort the photos"},)
    assert log.full_reason(ref)[-1].response == response
    log.close()


def test_w14_tier_b_records_answer_without_reading_or_writing_work(tmp_path):
    ctx, log, door, ref, _ = _start_fixture(tmp_path)
    ctx = replace(ctx, sources=_AssertNeverCalled())
    assert respond(ctx, NOW, actor="chatgpt-web", surfacing_id=ref, response="yes",
                   said_by="owner", message="I am starting to sort the photos") == {
                       "outcome": "recorded", "response": "yes"}
    assert door.calls == []
    log.close()


def test_w14_legacy_door_name_cannot_supply_authentication(tmp_path):
    from gremlin_town.mcp.brain_runtime import BrainRuntimeError
    ctx, log, door, ref, _ = _start_fixture(tmp_path)
    ctx = replace(ctx, sources=_AssertNeverCalled())
    with pytest.raises(BrainRuntimeError) as failure:
        respond(ctx, NOW, actor="hermes-wa", surfacing_id=ref, response="yes", said_by="owner",
                message="I am starting to sort the photos")
    assert failure.value.code == "ACCESS_DENIED" and failure.value.field == "actor"
    assert "host-authenticated BrainCaller" in failure.value.fix
    assert door.calls == []
    assert log.full_reason(ref)[-1].response == "yes"
    log.close()


def test_w14_unknown_code_caller_logs_but_has_no_work_access(tmp_path):
    from gremlin_town.mcp.brain_runtime import BrainCaller
    ctx, log, door, ref, _ = _start_fixture(tmp_path)
    ctx = replace(ctx, sources=_AssertNeverCalled())
    answer = respond(ctx, NOW, actor=BrainCaller("system:orientation", frozenset({"bc:read", "bc:write"})),
                     surfacing_id=ref, response="yes", said_by="owner", message="I am starting to sort the photos")
    assert answer == {"outcome": "recorded", "response": "yes"}
    assert door.calls == []
    log.close()


def test_w14_door_tool_needs_existing_write_scope(tmp_path):
    from gremlin_town.mcp.brain_tools import build_orientation_tools
    ctx, log, door, ref, _ = _start_fixture(tmp_path)
    async def authorize():
        return None
    tool = next(tool for tool in build_orientation_tools(
        ctx, lambda: Actor("hermes-wa"), scopes_provider=lambda: {"bc:read"}, authorize=authorize)
        if tool.name == "bc_next_respond")
    answer = asyncio.run(tool.run({"surfacing_id": ref, "response": "yes", "said_by": "owner",
                                  "message": "I am starting to sort the photos"}))
    assert answer.isError and answer.structuredContent["error"]["code"] == "ACCESS_DENIED"
    assert door.calls == []
    assert log.full_reason(ref)[-1].response is None
    log.close()


def test_w14_admission_uses_host_door_participant(tmp_path):
    from gremlin_town.mcp.brain_runtime import BrainCaller
    ctx, log, door, ref, _ = _start_fixture(tmp_path)
    caller = BrainCaller("alice", frozenset({"bc:read", "bc:write"}))
    result = respond(replace(ctx, door_participant="alice"), NOW, actor=caller,
                     surfacing_id=ref, response="yes", said_by="owner",
                     message="I am starting to sort the photos")
    assert result["status"] == "accepted"
    assert door.calls[0][0] == caller
    log.close()


def test_w14_start_keeps_in_progress_caution_without_personal_notes(tmp_path):
    from brain_core.records import record_from_mapping
    ctx, log, door, ref, caller = _start_fixture(tmp_path)
    started = []
    for number in range(3):
        raw = door.item.as_dict()
        raw.update(id=f"brn_20261004_060000_{number + 2:016x}", title=f"Started {number}")
        raw["data"] = {**raw["data"], "started_at": NOW.isoformat()}
        started.append(wrap(record_from_mapping(raw, "W14 started fixture")))
    sources = replace(ctx.sources, review=replace(ctx.sources.review,
        brain_items=lambda: (wrap(door.item), *started)))
    result = respond(replace(ctx, sources=sources), NOW, actor=caller,
                     surfacing_id=ref, response="yes", said_by="owner",
                     message="I am starting to sort the photos")
    assert {entry["title"] for entry in result["in_progress"]} == {f"Started {number}" for number in range(3)}
    assert door.records[0].data["started_at"] == NOW.isoformat()
    log.close()


def test_w14_first_id_pick_pins_original_current_source(tmp_path):
    from brain_core.records import record_from_mapping
    ctx, log, door, _, caller = _start_fixture(tmp_path)
    old = door.item
    raw = old.as_dict()
    raw.update(id="brn_20261004_060001_0000000000000002", supersedes=[old.id],
               created_at=(NOW + timedelta(seconds=1)).isoformat(),
               updated_at=(NOW + timedelta(seconds=1)).isoformat())
    latest = record_from_mapping(raw, "W14 current source fixture")
    current = wrap(latest, path="records/item/2026/10/current-source.md")
    sources = replace(ctx.sources, review=replace(ctx.sources.review,
        brain_items=lambda: (wrap(old), current),
        brain_items_by_path=lambda paths: (current,) if current.path in paths else ()))
    ctx = replace(ctx, sources=sources)
    answer = what_next(ctx, NOW + timedelta(seconds=2))
    assert answer["surfacing_id"] == old.id
    row = log.full_reason(old.id)[-1]
    assert row.record_path == current.path and row.record_sha256 == current.sha256
    # Door receives the first ID, while the audit lookup reads the raw successor.
    captured = []
    def callback(actor, request):
        captured.append(request)
        return {"status": "accepted"}
    respond(replace(ctx, work_door=callback), NOW, actor=caller, surfacing_id=old.id,
            response="yes", said_by="owner", message="I am starting to sort the photos")
    assert captured[0].moves[0]["thing"] == old.id
    log.close()


def _w14_caution_records(template, deadline_source):
    """Strict synthetic equivalents: one known due-today deadline, or no deadline."""
    from hashlib import sha256
    from brain_core.records import record_from_mapping

    def sourced(raw, words):
        return {**raw, "source": {"said_by": "owner", "came_by": "direct", "words": words,
                                 "recorded_by": "hermes-wa"},
                "evidence": [{"source_type": "session_message", "locator": "synthetic W14 caution fixture",
                              "excerpt": words, "content_sha256": sha256(words.encode()).hexdigest()}]}

    parent_id = "brn_20261004_060000_0000000000000004"
    first_id = "brn_20261004_060000_0000000000000002"
    # Native deadline shape used by W7's _w7_date fixture, with these known
    # synthetic owner words and the same day for inherited and explicit dates.
    deadline = {"day": "2026-10-04", "words": "due today", "read_by": "code", "read_on": "2026-10-04"}
    parent_raw = {**template, "id": parent_id, "title": "Exhibition permit",
                  "data": {"level": "accepted", "order": 1}}
    parent_words = "Exhibition permit"
    if deadline_source == "inherited":
        parent_raw["data"]["dates"] = {"deadline": deadline}
        parent_words += " due today"
    parent = record_from_mapping(sourced(parent_raw, parent_words), "W14 caution parent")
    part_raw = {**template, "id": first_id, "title": "Get the inspector's checklist",
                "data": {"level": "accepted", "parent": parent_id, "order": 1}}
    part_words = "Get the inspector's checklist"
    if deadline_source == "explicit":
        part_raw["data"]["dates"] = {"deadline": deadline}
        part_words += " due today"
    part_raw = sourced(part_raw, part_words)
    old = record_from_mapping(part_raw, "W14 caution first version")
    current_raw = {**part_raw, "id": "brn_20261004_060001_0000000000000003",
                   "supersedes": [first_id], "created_at": (NOW + timedelta(seconds=1)).isoformat(),
                   "updated_at": (NOW + timedelta(seconds=1)).isoformat()}
    current = wrap(record_from_mapping(current_raw, "W14 caution current version"),
                   path="records/item/2026/10/memo-current.md")
    return parent, old, current


@pytest.mark.parametrize("deadline_source", ["inherited", "explicit", "none"])
def test_w14_caution_fixture_constructs_strict_native_deadline(tmp_path, deadline_source):
    _, log, door, _, _ = _start_fixture(tmp_path)
    parent, old, current = _w14_caution_records(door.item.as_dict(), deadline_source)
    assert current.record.supersedes == (old.id,)
    assert current.record.data["parent"] == parent.id
    dated = parent if deadline_source == "inherited" else current.record
    if deadline_source == "none":
        assert not parent.data.get("dates") and not current.record.data.get("dates")
    else:
        deadline = dated.data["dates"]["deadline"]
        assert deadline["day"] == deadline["read_on"] == "2026-10-04"
        assert deadline["read_by"] == "code" and deadline["words"] in dated.source["words"]
    log.close()


@pytest.mark.parametrize("deadline_source", ["inherited", "explicit", "none"])
def test_w14_alternative_caution_uses_full_reader_index_and_raw_successor_pin(tmp_path, deadline_source):
    ctx, log, door, target, caller = _start_fixture(tmp_path)
    parent, old, current = _w14_caution_records(door.item.as_dict(), deadline_source)
    first_id = old.id
    entries = (wrap(parent), wrap(old), current, wrap(door.item))
    reads = []
    def snapshot():
        reads.append("complete")
        return entries
    sources = replace(ctx.sources, review=replace(ctx.sources.review, brain_items=snapshot,
        brain_items_by_path=lambda paths: tuple(entry for entry in entries if entry.path in paths)))
    ctx = replace(ctx, sources=sources)
    pick = what_next(ctx, NOW + timedelta(seconds=2))
    # With no dates, use the returned alternative if the unrelated target wins.
    assert first_id in {pick["surfacing_id"], *(entry["record_id"] for entry in pick["alternatives"])}
    row = log.full_reason(first_id)[-1]
    assert (row.record_path, row.record_sha256) == (current.path, current.sha256)
    reads.clear()
    result = respond(ctx, NOW + timedelta(seconds=3), actor=caller, surfacing_id=first_id,
                     response="other", target=target, said_by="owner",
                     message="I am starting to sort the photos", words="starting to sort the photos")
    assert result["status"] == "accepted"
    assert door.records[0].data["started_at"] == NOW.isoformat()
    assert door.calls[0][1].moves[0]["thing"] == target
    assert reads == ["complete", "complete"]  # existing progress read and the caution's full snapshot
    if deadline_source == "none":
        assert "caution" not in result
        assert not any(row.rule == "caution" for row in log.full_reason(first_id))
    else:
        assert result["caution"] == 'Noted. "Get the inspector\'s checklist" is still due today.'
        caution_row = log.full_reason(first_id)[-1]
        assert caution_row.rule == "caution"
        assert (caution_row.item_ref, caution_row.record_path, caution_row.record_sha256) == (
            first_id, current.path, current.sha256)
        again = respond(ctx, NOW + timedelta(seconds=4), actor=caller, surfacing_id=first_id,
                        response="other", target=target, said_by="owner",
                        message="I am starting to sort the photos", words="starting to sort the photos")
        assert "caution" not in again
    log.close()


def test_w14_missing_door_names_fix_without_note_fallback(tmp_path):
    from gremlin_town.mcp.brain_runtime import BrainRuntimeError
    ctx, log, door, ref, caller = _start_fixture(tmp_path)
    with pytest.raises(BrainRuntimeError, match="no work door") as failure:
        respond(replace(ctx, work_door=None), NOW, actor=caller, surfacing_id=ref, response="yes",
                said_by="owner", message="I am starting to sort the photos")
    assert failure.value.field == "work_door"
    assert "BrainRuntime.change_work" in failure.value.fix
    assert door.calls == []
    log.close()


@pytest.mark.parametrize("message", [None, "", "   "])
def test_w14_missing_message_names_precise_fix(tmp_path, message):
    from gremlin_town.mcp.brain_runtime import BrainRuntimeError
    ctx, log, door, ref, caller = _start_fixture(tmp_path)
    with pytest.raises(BrainRuntimeError) as failure:
        respond(ctx, NOW, actor=caller, surfacing_id=ref, response="yes", said_by="owner", message=message)
    assert failure.value.field == "message"
    assert "original message" in failure.value.fix
    assert door.calls == []
    log.close()


def test_w14_bare_reply_keeps_the_moves_drop_guard(tmp_path):
    ctx, log, door, ref, caller = _start_fixture(tmp_path)
    answer = respond(ctx, NOW, actor=caller, surfacing_id=ref, response="yes",
                     said_by="owner", message="yes", words="yes")
    assert answer["status"] == "accepted"
    assert "started_at" not in door.records[0].data
    assert any(flag["dropped"] for flag in door.flags)
    assert "outcome" not in answer or answer["outcome"] != "started"
    log.close()


def test_w14_async_door_runs_on_the_hosts_existing_loop(tmp_path):
    ctx, log, door, ref, caller = _start_fixture(tmp_path)
    async def callback(actor, request):
        await asyncio.sleep(0)
        return door(actor, request)
    async def host():
        return await respond(replace(ctx, work_door=callback), NOW, actor=caller,
                             surfacing_id=ref, response="yes", said_by="owner",
                             message="I am starting to sort the photos")
    assert asyncio.run(host())["status"] == "accepted"
    assert door.records[0].data["started_at"] == NOW.isoformat()
    log.close()


def _w14_http_settings(tmp_path):
    import json
    from tests.mcp.test_brain_composition import _settings
    settings = _settings(tmp_path)
    policy = json.loads(settings.policy_file.read_text())
    policy["subject_scopes"] = {"alice": ["bc:read", "bc:write"]}
    policy["owners"][0]["readers"] = ["alice", "chatgpt-web"]
    policy["owners"][0]["writers"] = ["alice"]
    settings.policy_file.write_text(json.dumps(policy))
    return settings


def test_w14_http_settings_and_authenticated_callback_construct(tmp_path):
    from gremlin_town.mcp.brain_assembly import load_owner_policy
    from gremlin_town.mcp.brain_runtime import BrainCaller
    settings = _w14_http_settings(tmp_path)
    policy = load_owner_policy(settings.policy_file)
    caller = BrainCaller("alice", policy.subject_scopes["alice"])
    assert caller.subject == "alice" and "bc:read" in caller.scopes
    assert "bc:write" in caller.scopes


def test_w14_same_pick_over_private_http_and_mcp(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from gremlin_town.api.brain import install_brain_routes
    from gremlin_town.mcp import brain_tools
    from gremlin_town.runtime import build_runtime
    from gremlin_town.transport import FakeTransport
    from tests.integration.test_mcp_runtime import mcp_config
    from tests.mcp.test_brain_tools import Runtime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    class HostRuntime(Runtime):
        async def aclose(self):
            return None
    monkeypatch.setattr(brain_tools, "datetime", Clock)
    task = replace(make_item(id="brn_task", title="Exhibition permit"), data={
        "level": "accepted", "called": "task", "order": 1})
    part = replace(make_item(id="brn_memo", title="Inspector's checklist"), data={
        "level": "accepted", "parent": "brn_task", "order": 1})
    # Both surfaces use the same actual source fixture and audit pins.
    ctx, log = context(tmp_path=tmp_path, items=(wrap(task), wrap(part)))
    config, tokens = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    runtime.brain = _w14_http_settings(tmp_path)
    runtime.brain_runtime = HostRuntime()
    runtime.orientation, runtime.orientation_context = ctx.orientation_settings, ctx
    install_brain_routes(runtime.app, runtime)
    async def authorize():
        return None
    definitions = {tool.name: tool for tool in brain_tools.build_orientation_tools(
        ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read", "bc:write"}, authorize=authorize)}
    mcp = asyncio.run(definitions["bc_next"].run({})).structuredContent
    with TestClient(runtime.app) as client:
        response = client.post("/v1/brain/next", headers={"Authorization": "Bearer " + tokens["alice"]}, json={})
        assert response.status_code == 200, response.text
        http = response.json()
    assert http["top_pick"] == mcp["top_pick"]
    assert http["surfacing_id"] == mcp["surfacing_id"] == "brn_memo"
    assert mcp["top_pick"]["belongs_to"] == "Exhibition permit"
    assert log.full_reason("brn_memo")[-1].record_sha256 == wrap(part).sha256
    log.close()


# --- Case 4: the whole picture, ranked, and three options, important over fun --------------------------


def _case_4(tmp_path: Path, continuity: FakeContinuity | None = None):
    """Two projects, a calendar item, a routine and a chore in play, plus a promise due today."""

    job = wrap(make_item(id="brn_job", title="Send the job application", owed_to="the recruiter", due="2026-10-04",
                         created_at=NOW - timedelta(days=1)))
    exosuit = wrap(make_item(id="brn_exosuit", title="Build the next slice",
                             links=({"type": "about", "target": "vault_exosuit"},), created_at=NOW - timedelta(days=2)))
    robotics = wrap(make_item(id="brn_robotics", title="Read the robotics course notes",
                              links=({"type": "about", "target": "vault_robotics"},), created_at=NOW - timedelta(days=30)))
    laundry = wrap(make_item(id="brn_laundry", title="Laundry", repeat="weekly:sun", created_at=NOW - timedelta(days=20)))
    chore = wrap(make_item(id="brn_chore", title="Buy light bulbs", created_at=NOW - timedelta(days=40)))
    events = (CalendarEvent(title="Dentist", start=datetime(2026, 10, 4, 16, 0, tzinfo=SGT),
                            end=datetime(2026, 10, 4, 17, 0, tzinfo=SGT)),)
    return context(
        tmp_path=tmp_path,
        items=(job, exosuit, robotics, laundry, chore),
        continuity=continuity,
        preferences=lambda: (focus_preference(["vault_exosuit"]),),
        calendar_events=lambda start, end: events,
        open_requests=lambda: (),
    )


def test_where_am_i_shows_the_whole_picture_ranked_and_recommends_nothing(tmp_path: Path) -> None:
    continuity = FakeContinuity()
    ctx, why_log = _case_4(tmp_path, continuity)

    answer = overview(ctx, NOW)
    assert answer["title"] == "All my work"
    assert answer["outcome"] == "answered"
    # The undated projects and chore show under "No date yet", the focus project first (N17);
    # until 28 Sep they weren't shown at all.
    assert lines(answer) == [
        # The three highest scores Nic could start now, across every section (PRD §4.5).
        ("highest_right_now", "Send the job application — due today; the recruiter is waiting"),
        ("highest_right_now", "Laundry — every Sunday, due today"),
        ("highest_right_now", "Build the next slice — focus project: vault_exosuit"),
        ("today", "Dentist — on your calendar at 4 pm"),
        ("today", "Send the job application — due today"),
        ("today", "Laundry — every Sunday, due today; streak 0; done 0 of the last 2 (0%); slipping"),
        ("no_date_yet", "Build the next slice — last changed Fri 2 Oct"),
        ("no_date_yet", "Buy light bulbs — last changed Tue 25 Aug"),
        ("no_date_yet", "Read the robotics course notes — last changed Fri 4 Sep"),
    ]
    assert answer["sections"][0]["title"] == "Highest right now"
    assert answer["sections"][0]["lines"][0]["reasons"] == ["due today", "the recruiter is waiting"]
    assert answer["sections"][1]["title"] == "Today"
    assert answer["sections"][1]["lines"][1]["reasons"] == ["due today", "the recruiter is waiting"]
    assert answer["sections"][2]["title"] == "No date yet"
    assert answer["sections"][2]["lines"][0]["reasons"] == ["focus project: vault_exosuit"]
    # Nothing is recommended: no pick, no first step, no time box, nothing to say yes to (N1).
    assert not {"top_pick", "surfacing_id", "alternatives", "first_step", "time_box_minutes"} & set(answer)
    assert continuity.calls == 0
    # Logged before answering, one row per line, on its own channel.
    history = why_log.history()
    assert [(row.channel, row.item_ref, row.rule) for row in history] == [
        ("overview", "brn_job", "overview_highest_right_now"),
        ("overview", "brn_laundry", "overview_highest_right_now"),
        ("overview", "brn_exosuit", "overview_highest_right_now"),
        ("overview", "calendar:Dentist:2026-10-04T16:00:00+08:00", "overview_today"),
        ("overview", "brn_job", "overview_today"),
        ("overview", "brn_laundry", "overview_today"),
        ("overview", "brn_exosuit", "overview_no_date_yet"),
        ("overview", "brn_chore", "overview_no_date_yet"),
        ("overview", "brn_robotics", "overview_no_date_yet"),
    ]
    why_log.close()


def test_the_asks_read_preferences_once_never_through_the_reviews_own_reader(tmp_path: Path) -> None:
    """N16: the review's preferences reader is for the 06:00 review's "Coming up"; both asks read
    Nic's preferences once, through their own source."""

    ctx, why_log = _case_4(tmp_path)
    review = replace(ctx.sources.review, brain_preferences=_AssertNeverCalled())
    ctx = replace(ctx, sources=replace(ctx.sources, review=review))
    assert overview(ctx, NOW)["outcome"] == "answered"
    assert what_next(ctx, NOW)["outcome"] == "answered"
    why_log.close()


def test_what_next_gives_a_top_pick_and_two_alternatives_important_over_fun(tmp_path: Path) -> None:
    continuity = FakeContinuity()
    ctx, why_log = _case_4(tmp_path, continuity)

    answer = what_next(ctx, NOW)
    assert answer["outcome"] == "answered"
    pick = answer["top_pick"]
    assert pick["record_id"] == "brn_job"
    assert pick["rules"] == ["due today", "the recruiter is waiting"]
    assert pick["plain_word"] == "promise"
    assert pick["first_step"] == ""  # the agent writes one, labelled as its own suggestion
    assert answer["surfacing_id"] == "brn_job"
    # By points (PRD §5): the routine due today (its date, 8.6) beats the focus project (5), which
    # beats the other project and the chore.
    assert [(alt["record_id"], alt["rules"]) for alt in answer["alternatives"]] == [
        ("brn_laundry", ["every Sunday, due today"]),
        ("brn_exosuit", ["focus project: vault_exosuit"]),
    ]
    assert [(entry["record_id"], entry["reason"]) for entry in answer["can_wait"]] == [
        ("brn_chore", "no date set yet"), ("brn_robotics", "no date set yet"),
    ]
    assert "in_progress" not in answer and "not_enough" not in answer
    # It waits: nothing starts before a yes; only why-log rows are written, on the orient channel.
    assert continuity.calls == 0
    assert [(row.channel, row.item_ref, row.rule, row.response) for row in why_log.history()] == [
        ("orient", "brn_job", "due today; the recruiter is waiting", None),
        ("orient", "brn_laundry", "alternative", None),
        ("orient", "brn_exosuit", "alternative", None),
        ("orient", "brn_chore", "can_wait", None),
        ("orient", "brn_robotics", "can_wait", None),
    ]
    why_log.close()


def test_a_started_task_beats_a_new_one_at_the_same_level(tmp_path: Path) -> None:
    """Case 17: at the same rank, what Nic already started comes first (N10a)."""

    new = wrap(make_item(id="brn_new", title="Plan the trip", created_at=NOW - timedelta(days=9)))
    started = wrap(make_item(id="brn_started", title="Sort the photos", created_at=NOW - timedelta(days=1)))
    from brain_core.orientation import in_progress as stored_progress
    assert stored_progress((started,), (note(started.record.id),), NOW) == ()
    started = wrap(replace(started.record, updated_at=NOW, source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"},
                        data={**started.record.data, "started_at": NOW.isoformat(),
                              "next_action": {"text": "open the file", "done": False}}))
    ctx, why_log = context(tmp_path=tmp_path, items=(new, started), notes=(note("brn_started", session_id="photos"),))

    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == "brn_started"
    assert answer["top_pick"]["rules"] == ["already started"]
    assert answer["top_pick"]["first_step"] == "open the file"
    assert [alt["record_id"] for alt in answer["alternatives"]] == ["brn_new"]
    why_log.close()


def test_not_enough_says_so_plainly_with_no_candidates(tmp_path: Path) -> None:
    ctx, why_log = context(tmp_path=tmp_path)
    answer = what_next(ctx, NOW)
    assert "top_pick" not in answer
    assert answer["not_enough"] == {"reason": "Nothing accepted and open right now.", "questions": []}
    assert answer["surfacing_id"] == "__orient__"
    assert [(row.item_ref, row.rule) for row in why_log.history()] == [
        ("__orient__", "Nothing accepted and open right now."),
    ]
    why_log.close()


def test_not_enough_asks_every_question_it_needs(tmp_path: Path) -> None:
    """N7: no limit on questions."""

    waiting = tuple(
        wrap(make_item(id=f"brn_w{index}", title=f"Quote {index}", state="waiting", owed_to=f"Supplier {index}",
                       created_at=NOW - timedelta(days=index)))
        for index in range(1, 5)
    )
    ctx, why_log = context(tmp_path=tmp_path, items=waiting)
    answer = what_next(ctx, NOW)
    assert answer["not_enough"]["questions"] == [
        'Is "Quote 4" still waiting on Supplier 4?', 'Is "Quote 3" still waiting on Supplier 3?',
        'Is "Quote 2" still waiting on Supplier 2?', 'Is "Quote 1" still waiting on Supplier 1?',
    ]
    assert why_log.history()[0].record_sha256 == ""
    why_log.close()


# --- A start time that has passed is asked about, never picked (Accepted - Nic, 28 Sep 2026) ----------
# The Greta meeting: saved due at 00:00, and at 6:43 pm the same day "what next?" picked it.


def _meeting() -> ItemRecord:
    return wrap(make_item(id="brn_greta", title="Meeting with Greta at 12am", due="2026-10-04", at="00:00"))


def test_what_next_asks_about_a_passed_start_time_and_logs_the_question(tmp_path: Path) -> None:
    lunch = wrap(make_item(id="brn_lunch", title="Eat lunch", due="2026-10-03"))
    ctx, why_log = context(tmp_path=tmp_path, items=(_meeting(), lunch))
    answer = what_next(ctx, NOW)  # 14:00 SGT, 14 hours after the meeting
    assert answer["top_pick"]["record_id"] == "brn_lunch"  # overdue with no time: still a pick
    assert "brn_greta" not in {entry["record_id"] for entry in (*answer["alternatives"], *answer["can_wait"])}
    assert answer["missed"] == [{
        "record_id": "brn_greta",
        "title": "Meeting with Greta at 12am",
        "question": '"Meeting with Greta at 12am" was today at 12 am. Did it happen? Done, move or drop?',
    }]
    (row,) = [row for row in why_log.history() if row.item_ref == "brn_greta"]
    assert (row.channel, row.rule) == ("orient", "missed")
    why_log.close()


def test_with_only_a_passed_start_time_what_next_has_nothing_to_start_and_asks(tmp_path: Path) -> None:
    ctx, why_log = context(tmp_path=tmp_path, items=(_meeting(),))
    answer = what_next(ctx, NOW)
    assert "top_pick" not in answer
    assert answer["not_enough"]["reason"] == (
        "Nothing to start right now: everything open was set for a time that has passed."
    )
    assert [entry["record_id"] for entry in answer["missed"]] == ["brn_greta"]
    assert [row.rule for row in why_log.history()] == [answer["not_enough"]["reason"], "missed"]
    why_log.close()


# --- Case 5: a stalling task gets a time box, not a plan -----------------------------------------------


def _surfacing(item: ItemRecord, when: datetime, channel: str = "orient") -> Surfacing:
    return Surfacing(
        surfaced_at=when, channel=channel, item_ref=item.record.id, record_sha256=item.sha256,
        record_path=item.path, record_state=item.record.state, rule="due today", fields_used={},
    )


def test_a_stalling_task_gets_a_20_minute_time_box(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_stuck", title="File the tax form", due="2026-10-04"))
    ctx, why_log = context(tmp_path=tmp_path, items=(item,))
    why_log.record_surfacings([_surfacing(item, NOW - timedelta(days=1)), _surfacing(item, NOW - timedelta(days=2))])

    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["time_box_minutes"] == 20
    assert answer["top_pick"]["first_step"] == ""
    why_log.close()


def test_asking_where_am_i_never_makes_a_task_look_stalled(tmp_path: Path) -> None:
    """Redesign default 11: "where am I?" rows don't count towards "tends to stall"."""

    # Its window closes on Mon 5 Oct, so "where am I?" showed it under "Windows closing" on both
    # earlier days, unchanged.
    item = wrap(make_item(id="brn_task", title="File the tax form"))
    item = wrap(replace(item.record, data={**item.record.data, "timing": {"window": {"start": "2026-09-30", "end": "2026-10-05"}}}))
    ctx, why_log = context(tmp_path=tmp_path, items=(item,))
    overview(ctx, NOW - timedelta(days=2))
    overview(ctx, NOW - timedelta(days=1))
    # Each time: its "Highest right now" line and its "Windows closing" line.
    assert [(row.channel, row.item_ref, row.record_sha256) for row in why_log.history()] == [
        ("overview", "brn_task", item.sha256),
    ] * 4

    assert what_next(ctx, NOW)["top_pick"]["time_box_minutes"] is None
    why_log.close()


# --- Case 6: one yes creates exactly one tracked item, with its first step ------------------------------


def test_one_yes_creates_exactly_one_tracked_item_with_its_first_step(tmp_path: Path) -> None:
    """Tier B's yes is logged, replayed safely, and cannot create a personal note."""
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=continuity)
    ref = what_next(ctx, NOW)["surfacing_id"]
    for _ in range(2):
        assert respond(ctx, NOW, actor="chatgpt-web", surfacing_id=ref, response="yes",
                       first_step="Open the deck") == {"outcome": "recorded", "response": "yes"}
    assert continuity.calls == 0 and continuity.notes == {}
    assert [row.response for row in why_log.history()] == ["yes"]
    why_log.close()


def test_a_yes_with_no_first_step_leaves_it_empty_never_a_placeholder(tmp_path: Path) -> None:
    """Tier B cannot add a placeholder or create a note when no action was given."""
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=continuity)
    result = respond(ctx, NOW, actor="chatgpt-web", surfacing_id=what_next(ctx, NOW)["surfacing_id"], response="yes")
    assert result == {"outcome": "recorded", "response": "yes"}
    assert continuity.notes == {}
    why_log.close()


def test_no_does_not_change_the_record_or_start_anything(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=continuity)
    result = respond(ctx, NOW, actor="chatgpt-web", surfacing_id=what_next(ctx, NOW)["surfacing_id"], response="no")
    assert result == {"outcome": "recorded", "response": "no"}
    assert continuity.calls == 0
    why_log.close()


def test_picking_an_alternative_starts_it_with_at_most_one_caution(tmp_path: Path) -> None:
    declined = wrap(make_item(id="brn_declined", title="Pay the electricity bill", due="2026-10-04"))
    target = wrap(make_item(id="brn_target", title="Call the bank", created_at=NOW - timedelta(days=1)))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(declined, target), continuity=continuity)

    from gremlin_town.mcp.brain_runtime import BrainCaller
    caller = BrainCaller("hermes-wa", frozenset({"bc:read", "bc:write"}))
    calls = []
    def door(actor, request):
        calls.append((actor, request))
        return {"status": "accepted"}
    ctx = replace(ctx, work_door=door)
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == "brn_declined"
    assert [alt["record_id"] for alt in answer["alternatives"]] == ["brn_target"]

    result = respond(ctx, NOW, actor=caller, surfacing_id="brn_declined", response="other", target="brn_target",
                     said_by="owner", message="I am starting the bank first", words="the bank first")
    assert result["status"] == "accepted"
    assert calls[0][1].moves[0]["thing"] == "brn_target"
    assert result["caution"] == 'Noted. "Pay the electricity bill" is still due today.'
    assert continuity.calls == 0

    again = respond(ctx, NOW, actor=caller, surfacing_id="brn_declined", response="other", target="brn_target", said_by="owner", message="I am starting the bank first", words="the bank first")
    assert "caution" not in again
    assert calls[0][1].operation_id == calls[1][1].operation_id
    why_log.close()


def test_the_yes_lands_on_the_what_next_row_not_a_later_where_am_i_row(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=FakeContinuity())
    what_next(ctx, NOW)
    overview(ctx, NOW + timedelta(minutes=5))

    respond(ctx, NOW + timedelta(minutes=6), actor="chatgpt-web", surfacing_id="brn_task", response="yes", words="yes")
    assert [(row.channel, row.response) for row in why_log.history()] == [
        ("orient", "yes"), ("overview", None), ("overview", None),
    ]
    why_log.close()


def test_a_yes_keeps_the_tasks_open_note_whatever_it_is_called(tmp_path: Path) -> None:
    """An agent already started this task's note under its own name: "what next?" leaves its first
    step empty, and the yes keeps that note rather than starting a second (N12b)."""

    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    existing = note("brn_task", session_id="deck-for-sam", next_step="Ask Sam which version")
    handed_off = note("brn_task", session_id="gt-req_0123", next_step="Result returned, not yet checked.")
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), notes=(handed_off, existing), continuity=continuity)

    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["first_step"] == ""  # A passive note supplies no owner action.
    result = respond(ctx, NOW, actor="chatgpt-web", surfacing_id=answer["surfacing_id"], response="yes",
                     first_step="Something else")
    assert result == {"outcome": "recorded", "response": "yes"}
    assert continuity.calls == 0
    why_log.close()


def test_a_new_yes_names_what_is_already_in_progress_and_still_starts(tmp_path: Path) -> None:
    """N10d: with 3 or more things in progress, "what next?" names them, and so does the answer
    to a yes to something new; the new note still starts. Handed-off work doesn't count."""

    started = tuple(
        wrap(make_item(id=f"brn_s{index}", title=f"Started {index}", created_at=NOW - timedelta(days=index)))
        for index in range(1, 4)
    )
    started = tuple(wrap(replace(entry.record, updated_at=NOW, source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"},
        data={**entry.record.data, "started_at": NOW.isoformat(),
              "next_action": {"text": "open the file", "done": False}})) for entry in started)
    new = wrap(make_item(id="brn_new", title="Pay the bill", due="2026-10-04"))
    notes = tuple(note(entry.record.id) for entry in started) + (note("brn_new", session_id="gt-req_9"),)
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(*started, new), notes=notes, continuity=continuity)

    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == "brn_new"
    assert [entry["title"] for entry in answer["in_progress"]] == ["Started 3", "Started 2", "Started 1"]
    assert answer["in_progress"][0] == {"record_id": "brn_s3", "title": "Started 3", "next_step": "open the file",
                                        "stuck": False}

    result = respond(ctx, NOW, actor="chatgpt-web", surfacing_id="brn_new", response="yes", first_step="Log in")
    assert result == {"outcome": "recorded", "response": "yes"}
    assert continuity.calls == 0
    again = respond(ctx, NOW, actor="chatgpt-web", surfacing_id="brn_new", response="other", target="brn_s1")
    assert again == {"outcome": "recorded", "response": "other"}
    why_log.close()


def test_an_unreadable_items_source_never_blocks_a_yes_and_is_reported(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=continuity)
    what_next(ctx, NOW)

    def broken():
        raise RuntimeError("Brain Home unreachable")

    ctx = replace(ctx, sources=replace(ctx.sources, review=replace(ctx.sources.review, brain_items=broken)))
    result = respond(ctx, NOW, actor="chatgpt-web", surfacing_id="brn_task", response="yes", first_step="Open it")
    assert result == {"outcome": "recorded", "response": "yes"}
    assert continuity.calls == 0
    why_log.close()


def test_an_unreadable_notes_source_never_blocks_a_yes_and_is_reported(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=continuity)
    what_next(ctx, NOW)

    def broken():
        raise RuntimeError("Brain Home unreachable")

    ctx = replace(ctx, sources=replace(ctx.sources, review=replace(ctx.sources.review, open_notes=broken)))
    result = respond(ctx, NOW, actor="chatgpt-web", surfacing_id="brn_task", response="yes", first_step="Open it")
    assert result == {"outcome": "recorded", "response": "yes"}
    assert continuity.calls == 0
    why_log.close()


def test_without_the_structured_notes_a_yes_still_finds_a_note_by_its_older_key(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=continuity)
    ctx = replace(ctx, sources=replace(
        ctx.sources, review=replace(ctx.sources.review, open_notes=None),
        open_notes=lambda: {"item-brn_task": "drop the pricing into slide 6"},
    ))
    answer = what_next(ctx, NOW)
    assert "notes: not_configured" in answer["unknowns"]
    result = respond(ctx, NOW, actor="chatgpt-web", surfacing_id=answer["surfacing_id"], response="yes")
    assert result == {"outcome": "recorded", "response": "yes"}
    assert continuity.calls == 0
    why_log.close()


# --- Case 14 and case 17: out-of-reach notes, stuck work, this week's finished items --------------------


def test_both_asks_still_answer_with_the_notes_out_of_reach(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))

    def broken_notes():
        raise RuntimeError("connector unreachable")

    ctx, why_log = context(tmp_path=tmp_path, items=(item,), notes=broken_notes)
    picture = overview(ctx, NOW)
    assert ("today", "Send the deck — due today") in lines(picture)
    assert ("could_not_see", "notes: RuntimeError: connector unreachable") in lines(picture)
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == "brn_task"
    assert "notes: RuntimeError: connector unreachable" in answer["unknowns"]
    why_log.close()


def test_where_am_i_marks_stuck_work_and_lists_this_weeks_finished_items(tmp_path: Path) -> None:
    moving = wrap(make_item(id="brn_moving", title="Draft the essay", created_at=NOW - timedelta(days=1)))
    stuck = wrap(make_item(id="brn_stuck", title="Clear the inbox", created_at=NOW - timedelta(days=5)))
    finished = wrap(make_item(id="brn_done", title="Send the invoice", state="done",
                              check={"locator": "sent email", "checked_by": "nic"},
                              updated_at=datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)))
    old_finish = wrap(make_item(id="brn_old", title="Old thing", state="done",
                                check={"locator": "x", "checked_by": "nic"}, updated_at=NOW - timedelta(days=8)))
    notes = (
        note("brn_moving", next_step="Write the intro", updated_at=NOW - timedelta(hours=5)),
        note("brn_stuck", next_step="Archive the newsletters", updated_at=NOW - timedelta(days=3)),
    )
    from brain_core.orientation import in_progress as stored_progress
    assert stored_progress((moving,), (note(moving.record.id),), NOW) == ()
    moving = wrap(replace(moving.record, updated_at=NOW - timedelta(hours=5), source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"},
                        data={**moving.record.data, "started_at": (NOW - timedelta(hours=5)).isoformat(),
                              "next_action": {"text": "Write the intro", "done": False}}))
    stuck = wrap(replace(stuck.record, updated_at=NOW - timedelta(days=3), source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"},
                        data={**stuck.record.data, "started_at": (NOW - timedelta(days=3)).isoformat(),
                              "next_action": {"text": "Archive the newsletters", "done": False}}))
    ctx, why_log = context(tmp_path=tmp_path, items=(moving, stuck, finished, old_finish), notes=notes)

    answer = overview(ctx, NOW)
    assert lines(answer) == [
        ("highest_right_now", "Clear the inbox — already started"),
        ("highest_right_now", "Draft the essay — already started"),
        ("in_progress", "Clear the inbox — next: Archive the newsletters; last changed Thu 1 Oct; stuck"),
        ("in_progress", "Draft the essay — next: Write the intro; last changed today"),
        ("finished_this_week", "Send the invoice — done Fri 2 Oct"),
    ]
    in_progress = answer["sections"][1]["lines"]
    assert in_progress[0]["stuck"] is True and "stuck" not in in_progress[1]
    assert why_log.history()[0].record_state == "open"
    why_log.close()


def test_where_am_i_shows_waiting_work_requests_and_what_it_could_not_see(tmp_path: Path) -> None:
    waiting = wrap(make_item(id="brn_wait", title="Fix the leak", state="waiting",
                             links=({"type": "waiting_on", "target": "the plumber"},)))
    request = OpenRequest(id="req_1", handed_to="fleet-boss-grok", since=date(2026, 10, 1), objective="Research roles")
    hub_note = note(None, session_id="gt-req_1", next_step="Compare the three offers")

    def no_calendar(start, end):
        raise RuntimeError("token_revoked")

    ctx, why_log = context(tmp_path=tmp_path, items=(waiting,), notes=(hub_note,), open_requests=lambda: (request,),
                           calendar_events=no_calendar)
    assert lines(overview(ctx, NOW)) == [
        ("waiting_on", "Fix the leak — waiting on the plumber"),
        ("waiting_on", "Research roles — with fleet-boss-grok since Thu 1 Oct; next: Compare the three offers"),
        ("could_not_see", "calendar: RuntimeError: token_revoked"),
    ]
    rows = why_log.history()
    assert [(row.item_ref, row.record_state) for row in rows] == [
        ("brn_wait", "waiting"), ("req_1", "open"), ("calendar", None),
    ]
    assert all(row.rule.startswith("overview_") for row in rows)
    why_log.close()


def test_neither_ask_reads_the_reviews_facts_and_a_failing_preferences_reader_is_named(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))

    def broken_preferences():
        raise RuntimeError("preferences unreadable")

    ctx, why_log = context(tmp_path=tmp_path, items=(item,), brain_facts=_AssertNeverCalled(),
                           preferences=broken_preferences)
    assert ("could_not_see", "preferences: RuntimeError: preferences unreadable") in lines(overview(ctx, NOW))
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == "brn_task"
    assert answer["unknowns"] == ["preferences: RuntimeError: preferences unreadable"]
    why_log.close()


# --- Project work (N4; rebuild plan slice 8, part 2): the task-sitrep reader, sitreps off and on -------


GTBC = "vault_gtbc_plans"


def project_task(
    session_id: str,
    *,
    intent: str = "Build slice 8",
    state: str = "open",
    next_step: str = "Write the tests",
    blockers: tuple[str, ...] = (),
    updated_at: datetime = NOW,
    sha: str = "c" * 64,
) -> TaskSitrep:
    """One of Nic's project tasks, as the hub's task sitrep holds it (b-project-sitreps.md)."""

    return TaskSitrep(
        project=GTBC, session_id=session_id, intent=intent, state=state, next_step=next_step, blockers=blockers,
        note_updated_at=updated_at, note_record_id="brn_20261004_060000_ab12", note_content_sha256=sha,
    )


def sitreps_reader(*tasks: TaskSitrep, as_of: datetime = NOW - timedelta(hours=1)):
    return lambda: (ProjectTasks(project=GTBC, as_of=as_of, tasks=tasks),)


def test_where_am_i_shows_focus_projects_tasks_from_the_sitreps_with_their_freshness(tmp_path: Path) -> None:
    """Sitreps on: each focus project, how fresh its summary is, then its open and blocked
    tasks, ranked; a focus project with no summary says so. Logged like every other line."""

    building = project_task("slice-8", updated_at=NOW - timedelta(hours=2), sha="1" * 64)
    blocked = project_task("release", intent="Package the release", state="blocked",
                           blockers=("Waiting on the merge",), updated_at=NOW - timedelta(days=1), sha="2" * 64)
    ctx, why_log = context(
        tmp_path=tmp_path,
        preferences=lambda: (focus_preference([GTBC, "vault_robotics"]),),
        task_sitreps=sitreps_reader(building, blocked),
    )

    answer = overview(ctx, NOW)
    assert lines(answer) == [
        ("highest_right_now", "Build slice 8 — focus project: vault_gtbc_plans; already started"),
        ("focus_projects", "vault_gtbc_plans — summary as of today at 1 pm"),
        ("focus_projects", "Package the release — blocked: Waiting on the merge; last changed yesterday"),
        ("focus_projects", "Build slice 8 — next: Write the tests; last changed today"),
        ("focus_projects", "vault_robotics — no summary yet"),
    ]
    section = answer["sections"][1]
    assert section["title"] == "Focus projects"
    assert section["lines"][0] == {
        "text": "vault_gtbc_plans — summary as of today at 1 pm", "item_ref": "project:vault_gtbc_plans", "project": GTBC,
    }
    assert section["lines"][2] == {
        "text": "Build slice 8 — next: Write the tests; last changed today", "item_ref": "project:vault_gtbc_plans:slice-8",
        "reasons": ["focus project: vault_gtbc_plans", "already started"], "project": GTBC,
    }
    assert "project" not in section["lines"][3]
    # Logged before answering, on "where am I?"'s own channel, each task with its note's version.
    assert [(row.channel, row.item_ref, row.record_sha256, row.record_state) for row in why_log.history()] == [
        ("overview", "project:vault_gtbc_plans:slice-8", "1" * 64, "open"),
        ("overview", "project:vault_gtbc_plans", "", None),
        ("overview", "project:vault_gtbc_plans:release", "2" * 64, "blocked"),
        ("overview", "project:vault_gtbc_plans:slice-8", "1" * 64, "open"),
        ("overview", "project:vault_robotics", "", None),
    ]
    why_log.close()


def test_what_next_ranks_a_focus_projects_task_by_points_and_picking_it_starts_nothing_new(tmp_path: Path) -> None:
    late_fee = {"level": "real_cost", "reason": "a late fee", "proposed_by": "hermes-wa", "confirmed": False}
    due = wrap(make_item(id="brn_due", title="Pay the electricity bill", due="2026-10-04", importance=late_fee))
    chore = wrap(make_item(id="brn_chore", title="Buy light bulbs", created_at=NOW - timedelta(days=40)))
    task = project_task("slice-8", sha="9" * 64)
    blocked = project_task("release", intent="Package the release", state="blocked",
                           blockers=("Waiting on the merge",), updated_at=NOW - timedelta(days=1))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(due, chore), continuity=continuity,
                           preferences=lambda: (focus_preference([GTBC]),), task_sitreps=sitreps_reader(task, blocked))

    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == "project:vault_gtbc_plans:slice-8"
    assert answer["alternatives"] == [
        {"record_id": "brn_due", "title": "Pay the electricity bill", "plain_word": "task",
         "rules": ["due today"]},
        {"record_id": "brn_chore", "title": "Buy light bulbs", "plain_word": "task",
         "rules": ["no date or stake yet; saved Tue 25 Aug"]},
    ]
    assert answer["can_wait"] == [
        {"record_id": "project:vault_gtbc_plans:release", "title": "Package the release",
         "reason": "blocked: Waiting on the merge", "project": GTBC, "session_id": "release"},
    ]
    (row,) = why_log.full_reason("project:vault_gtbc_plans:slice-8")
    assert (row.channel, row.rule, row.record_sha256, row.record_path) == ("orient", "focus project: vault_gtbc_plans; already started", "9" * 64, None)
    assert {key: row.fields_used[key] for key in ("project", "session_id", "next_step")} == {
        "project": GTBC, "session_id": "slice-8", "next_step": "Write the tests",
    }
    assert why_log.full_reason("project:vault_gtbc_plans:release")[0].fields_used["session_id"] == "release"

    # Nic picks it instead: its note is already open in its project, so nothing starts, and the
    # bill he declined is still due today.
    from gremlin_town.mcp.brain_runtime import BrainCaller
    caller = BrainCaller("hermes-wa", frozenset({"bc:read", "bc:write"}))
    result = respond(ctx, NOW, actor=caller, surfacing_id="brn_due", response="other",
                     target="project:vault_gtbc_plans:slice-8", first_step="Something else")
    assert result == {
        "outcome": "started", "project": GTBC, "session_id": "slice-8", "first_step": "Write the tests",
        "time_box_minutes": None, "caution": 'Noted. "Pay the electricity bill" is still due today.',
    }
    assert continuity.calls == 0
    # A project task never shown is refused loudly, not guessed.
    from gremlin_town.mcp.brain_runtime import BrainRuntimeError
    with pytest.raises(BrainRuntimeError, match="Could not resolve") as failure:
        respond(ctx, NOW, actor=caller, surfacing_id="brn_due", response="other",
                target="project:vault_gtbc_plans:never-shown")
    assert failure.value.field == "target" and "returned project task" in failure.value.fix
    why_log.close()


def test_a_focus_projects_task_as_the_top_pick_carries_its_next_step_and_its_time_box(tmp_path: Path) -> None:
    """Picked on two earlier days with its note unchanged since: it tends to stall (N3)."""

    ref = "project:vault_gtbc_plans:slice-8"
    continuity = FakeContinuity()
    ctx, why_log = context(
        tmp_path=tmp_path, continuity=continuity, preferences=lambda: (focus_preference([GTBC]),),
        task_sitreps=sitreps_reader(project_task("slice-8", next_step="  Write the tests ", sha="7" * 64)),
    )
    why_log.record_surfacings([
        Surfacing(surfaced_at=NOW - timedelta(days=days), channel="orient", item_ref=ref, record_sha256="7" * 64,
                  record_state="open", rule="focus project: vault_gtbc_plans", fields_used={})
        for days in (1, 2)
    ])

    answer = what_next(ctx, NOW)
    pick = answer["top_pick"]
    assert answer["surfacing_id"] == ref
    assert (pick["record_id"], pick["first_step"], pick["time_box_minutes"], pick["estimate_minutes"]) == (
        ref, "Write the tests", 20, None,
    )
    assert (pick["project"], pick["session_id"], pick["fields_used"]["focus_project"]) == (GTBC, "slice-8", GTBC)
    from gremlin_town.mcp.brain_runtime import BrainCaller
    caller = BrainCaller("hermes-wa", frozenset({"bc:read", "bc:write"}))
    result = respond(ctx, NOW, actor=caller, surfacing_id=ref, response="yes", words="yes")
    assert result == {"outcome": "started", "project": GTBC, "session_id": "slice-8", "first_step": "Write the tests",
                      "time_box_minutes": 20}
    assert continuity.calls == 0
    assert why_log.full_reason(ref)[-1].response == "yes"
    why_log.close()


def test_with_sitreps_off_both_asks_answer_exactly_as_before(tmp_path: Path) -> None:
    """`[sitreps]` off: no reader is wired, so nothing is read or said about projects, and a
    saved task in a focus project still scores for it as before."""

    exosuit = wrap(make_item(id="brn_exosuit", title="Build the next slice", links=({"type": "about", "target": GTBC},)))
    ctx, why_log = context(tmp_path=tmp_path, items=(exosuit,), notes=(note("brn_exosuit"),),
                           preferences=lambda: (focus_preference([GTBC]),))
    assert ctx.sources.task_sitreps is None
    assert [section["section"] for section in overview(ctx, NOW)["sections"]] == ["highest_right_now", "no_date_yet"]
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["rules"] == ["focus project: vault_gtbc_plans"]
    assert answer["top_pick"]["first_step"] == "" and "in_progress" not in answer
    assert answer["alternatives"] == [] and answer["unknowns"] == []
    why_log.close()


def test_unreadable_project_summaries_are_named_and_never_block_either_ask(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))

    def broken():
        raise RuntimeError("Brain Home unreachable")

    ctx, why_log = context(tmp_path=tmp_path, items=(item,), preferences=lambda: (focus_preference([GTBC]),),
                           task_sitreps=broken)
    picture = overview(ctx, NOW)
    assert "focus_projects" not in [section["section"] for section in picture["sections"]]
    assert ("could_not_see", "project summaries: RuntimeError: Brain Home unreachable") in lines(picture)
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == "brn_task"
    assert answer["unknowns"] == ["project summaries: RuntimeError: Brain Home unreachable"]
    why_log.close()


# --- The signal, and the three tools --------------------------------------------------------------------


def test_calendar_gap_signal_reaches_the_top_picks_reason(tmp_path: Path) -> None:
    item = wrap(make_item(id="brn_task", title="Quick email", due="2026-10-04"))
    ctx, why_log = context(tmp_path=tmp_path, items=(item,))
    answer = what_next(ctx, NOW, signal=OrientationSignal(kind="calendar_gap", gap_minutes=45))
    assert answer["top_pick"]["fields_used"]["gap_minutes"] == 45
    why_log.close()


def test_the_three_tools_answer_and_the_respond_tool_takes_the_first_step(tmp_path: Path) -> None:
    from gremlin_town.mcp.brain_tools import build_orientation_tools

    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    continuity = FakeContinuity()
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=continuity)

    async def authorize() -> None:
        return None

    tools = {
        tool.name: tool
        for tool in build_orientation_tools(
            ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read", "bc:write"}, authorize=authorize,
        )
    }
    assert list(tools) == ["bc_orient", "bc_next", "bc_next_respond", "bc_agenda", "bc_weekly"]
    assert tools["bc_orient"].parameters == {"additionalProperties": False, "properties": {}, "type": "object"}
    assert (tools["bc_orient"].annotations.readOnlyHint, tools["bc_next"].annotations.readOnlyHint,
            tools["bc_next_respond"].annotations.readOnlyHint) == (True, True, False)

    # The tools use the real clock, so this checks the shape, not which day the task falls on.
    whole = asyncio.run(tools["bc_orient"].run({}))
    assert not whole.isError and whole.structuredContent["outcome"] == "answered"
    assert "sections" in whole.structuredContent and "top_pick" not in whole.structuredContent
    pick = asyncio.run(tools["bc_next"].run({"signal": "calendar_gap", "gap_minutes": 30}))
    assert pick.structuredContent["top_pick"]["record_id"] == "brn_task"
    assert "Compatibility only" in tools["bc_next_respond"].parameters["properties"]["first_step"]["description"]
    result = asyncio.run(tools["bc_next_respond"].run({
        "surfacing_id": pick.structuredContent["surfacing_id"], "response": "yes", "first_step": "Open the deck",
    }))
    assert not result.isError
    assert result.structuredContent == {"outcome": "recorded", "response": "yes"}
    assert continuity.calls == 0
    why_log.close()


class _AllowanceUsedUp:
    def request(self, method, url, *, headers, body):  # type: ignore[no-untyped-def]
        from brain_core.github_storage import HTTPResponse

        return HTTPResponse(
            403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, b'{"message": "rate limited"}',
        )


@pytest.mark.parametrize("problem", ["allowance", "scan limit"])
def test_a_yes_whose_note_could_not_start_says_nothing_was_saved(tmp_path: Path, problem: str) -> None:
    """Tier B does not touch an unavailable personal-note store, for either failure mode."""

    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    from gremlin_town.mcp.brain_tools import build_orientation_tools
    from gremlin_town.review.continuity_source import GitHubContinuityStarter
    from tests.mcp.test_brain_composition import LANE_B

    remote = LANE_B.FakeGitHub()
    remote.seed({f"records/continuity/2026/09/brn_20260921_040000_{index:08x}.md": b"x" for index in range(2)})
    storage = GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN,
        transport=_AllowanceUsedUp() if problem == "allowance" else remote, object_cache=GitHubObjectCache(),
    )
    item = wrap(make_item(id="brn_task", title="Send the deck", due="2026-10-04"))
    ctx, why_log = context(tmp_path=tmp_path, items=(item,), continuity=GitHubContinuityStarter(storage, max_records=1))

    async def authorize() -> None:
        return None

    tools = {
        tool.name: tool
        for tool in build_orientation_tools(
            ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read", "bc:write"}, authorize=authorize,
        )
    }
    pick = asyncio.run(tools["bc_next"].run({})).structuredContent
    answer = asyncio.run(tools["bc_next_respond"].run({"surfacing_id": pick["surfacing_id"], "response": "yes"}))
    why_log.close()

    assert not answer.isError
    assert answer.structuredContent == {"outcome": "recorded", "response": "yes"}


_ANY_ARGUMENTS = {"bc_next_respond": {"surfacing_id": "x", "response": "no"}, "bc_agenda": {"person": "Sam"}}


def test_what_to_raise_with_sam_comes_back_from_bc_agenda(tmp_path: Path) -> None:
    """PRD §4.9, acceptance case 14, through the tool any agent calls."""

    from gremlin_town.mcp.brain_tools import build_orientation_tools

    topic = wrap(replace(make_item(id="brn_topic", title="The lease renewal"),
                         data={"level": "accepted", "size": "task", "raise_with": "Sam"}))
    promise = wrap(make_item(id="brn_deck", title="Send Sam the deck", owed_to="Sam", due="2026-10-06"))
    waiting = wrap(make_item(id="brn_quote", title="Sam's pricing", state="waiting",
                             links=({"type": "waiting_on", "target": "Sam"},)))
    other = wrap(make_item(id="brn_priya", title="Priya's thing", owed_to="Priya"))
    ctx, why_log = context(tmp_path=tmp_path, items=(topic, promise, waiting, other))

    async def authorize() -> None:
        return None

    tools = {tool.name: tool for tool in build_orientation_tools(
        ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read"}, authorize=authorize)}
    answer = asyncio.run(tools["bc_agenda"].run({"person": "sam"})).structuredContent
    assert answer["outcome"] == "answered" and answer["person"] == "Sam"  # as his records name them
    assert [line["record_id"] for line in answer["to_raise"]] == ["brn_topic"]
    assert [line["record_id"] for line in answer["promised"]] == ["brn_deck"]
    assert [line["record_id"] for line in answer["waiting_on"]] == ["brn_quote"]
    assert tools["bc_agenda"].annotations.readOnlyHint is True
    assert why_log.history() == ()  # it writes nothing
    why_log.close()


def test_bc_agenda_asks_which_sam_when_two_answer_to_the_name(tmp_path: Path) -> None:
    """Nic, 29 Sep 2026: a first name finds a full one; ask him when several people answer to it."""

    from gremlin_town.mcp.brain_tools import build_orientation_tools

    wong = wrap(replace(make_item(id="brn_wong", title="The Q3 budget"),
                        data={"level": "accepted", "size": "task", "raise_with": "Sam Wong"}))
    tan = wrap(make_item(id="brn_tan", title="Send the deck", owed_to="Sam Tan"))
    ctx, why_log = context(tmp_path=tmp_path, items=(wong, tan))

    async def authorize() -> None:
        return None

    tools = {tool.name: tool for tool in build_orientation_tools(
        ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read"}, authorize=authorize)}
    asked = asyncio.run(tools["bc_agenda"].run({"person": "Sam"})).structuredContent
    assert asked["outcome"] == "ask_which" and asked["ask_which"] == ["Sam Tan", "Sam Wong"]
    assert "to_raise" not in asked
    chosen = asyncio.run(tools["bc_agenda"].run({"person": "Sam Wong", "exact": True})).structuredContent
    assert chosen["outcome"] == "answered" and [line["record_id"] for line in chosen["to_raise"]] == ["brn_wong"]
    assert chosen["promised"] == []
    why_log.close()


def test_the_weekly_review_comes_back_from_bc_weekly(tmp_path: Path) -> None:
    """PRD §4.8, acceptance case 13, through the tool any agent calls: the packet, read-only."""

    from gremlin_town.mcp.brain_tools import build_orientation_tools

    late = wrap(make_item(id="brn_late", title="Pay the phone bill", due="2026-01-05"))  # the tool reads the real clock
    waiting = wrap(make_item(id="brn_quote", title="Get the quote", state="waiting",
                             links=({"type": "waiting_on", "target": "Sam"},)))
    idea = wrap(replace(make_item(id="brn_idea", title="Agent title", level="possible"),
                        source={"said_by": "owner", "words": "My idea in my words"}))
    ctx, why_log = context(tmp_path=tmp_path, items=(late, waiting, idea))

    async def authorize() -> None:
        return None

    tools = {tool.name: tool for tool in build_orientation_tools(
        ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read"}, authorize=authorize)}
    answer = asyncio.run(tools["bc_weekly"].run({})).structuredContent
    assert answer["outcome"] == "answered"
    parts = {section["part"]: [line["text"] for line in section["lines"]] for section in answer["sections"]}
    assert parts["waiting_by_person"] == ["Sam: Get the quote"]
    assert any(text.startswith("Pay the phone bill — missed since Mon 5 Jan") for text in parts["overdue"])
    assert "ideas" not in parts
    assert answer["ideas_question"] == "Want to go through your ideas?"
    shown = asyncio.run(tools["bc_weekly"].run({"ideas": True})).structuredContent
    assert next(section["lines"] for section in shown["sections"] if section["part"] == "ideas") == [
        {"text": "My idea in my words", "item_ref": "brn_idea"},
    ]
    assert tools["bc_weekly"].annotations.readOnlyHint is True
    assert why_log.history() == ()
    why_log.close()


def test_agenda_wrapper_includes_whose_and_needs_from_without_writing(tmp_path: Path) -> None:
    from gremlin_town.review.orient import agenda

    part = make_item(id="brn_rowan_part", title="Her part")
    need = make_item(id="brn_rowan_need", title="Her approval")
    items = (wrap(replace(part, data={**part.data, "whose": "Rowan Tan"})),
             wrap(replace(need, data={**need.data, "needs": {"from": "Rowan Tan"}})))
    ctx, why_log = context(tmp_path=tmp_path, items=items)
    answer = agenda(ctx, NOW, person="Rowan")
    assert answer["outcome"] == "answered" and answer["person"] == "Rowan Tan"
    assert {line["title"] for line in answer["waiting_on"]} == {"Her part", "Her approval"}
    assert why_log.history() == ()
    why_log.close()


def test_weekly_wrapper_asks_before_ideas_and_reads_next_weeks_appointments(tmp_path: Path) -> None:
    from gremlin_town.review.orient import weekly

    idea = wrap(replace(make_item(id="brn_idea", title="Changed title", level="possible"),
                        source={"said_by": "owner", "words": "My own idea"}))
    ranges = []
    event = CalendarEvent("Dentist", NOW + timedelta(days=2), NOW + timedelta(days=2, hours=1))

    def calendar(start, end):
        ranges.append((start, end))
        return (event,) if start <= event.start < end else ()

    ctx, why_log = context(tmp_path=tmp_path, items=(idea,), calendar_events=calendar)
    answer = weekly(ctx, NOW)
    assert answer["ideas_question"] == "Want to go through your ideas?"
    assert all(section["part"] != "ideas" for section in answer["sections"])
    assert any(line["text"].startswith("Dentist") for section in answer["sections"] for line in section["lines"])
    assert (ranges[-1][1] - ranges[-1][0]).days == 7
    shown = weekly(ctx, NOW, ideas=True)
    assert "ideas_question" not in shown
    assert next(section["lines"] for section in shown["sections"] if section["part"] == "ideas") == [
        {"text": "My own idea", "item_ref": "brn_idea"},
    ]
    assert why_log.history() == ()
    why_log.close()


def test_weekly_stale_packet_consumes_only_shown_rows_once_after_restart_and_prune(tmp_path: Path) -> None:
    from gremlin_town.review.orient import weekly

    requests = tuple(OpenRequest(f"stale-{n}", "agent", NOW.date() - timedelta(days=14), f"Idle {n}",
                                 last_activity=NOW - timedelta(days=14)) for n in range(12))
    ctx, why_log = context(tmp_path=tmp_path, open_requests=lambda: requests)
    first = weekly(ctx, NOW)
    assert sum("stale" in line["text"] for part in first["sections"] for line in part["lines"]) == 10
    assert len(why_log.request_review_history()) == 10
    weekly(ctx, NOW + timedelta(seconds=1))
    assert len(why_log.request_review_history()) == 12
    why_log.close()
    reopened = WhyLog(tmp_path / "why.sqlite3")
    later = NOW + timedelta(days=100)
    reopened.prune(later)
    answer = weekly(replace(ctx, why_log=reopened), later)
    assert all(part["part"] != "waiting_by_person" for part in answer["sections"])
    assert len(reopened.request_review_history()) == 12
    kinds = reopened._connection.execute("SELECT DISTINCT kind FROM runs").fetchall()
    assert [row["kind"] for row in kinds] == ["weekly"]
    reopened.close()


def test_weekly_packet_failure_consumes_nothing_and_retry_can_show_stale(tmp_path: Path) -> None:
    from gremlin_town.review.orient import weekly
    from gremlin_town.review.why_log import WhyLogError

    stale = OpenRequest("stale", "agent", NOW.date() - timedelta(days=14), "Idle",
                        last_activity=NOW - timedelta(days=14))
    ctx, why_log = context(tmp_path=tmp_path, open_requests=lambda: (stale,))
    why_log._connection.execute("""CREATE TRIGGER fail_weekly BEFORE INSERT ON request_review_consumption
                                   BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
    with pytest.raises(WhyLogError):
        weekly(ctx, NOW)
    assert why_log.request_review_history() == ()
    why_log._connection.execute("DROP TRIGGER fail_weekly")
    answer = weekly(ctx, NOW)
    assert any("stale" in line["text"] for part in answer["sections"] for line in part["lines"])
    again = weekly(ctx, NOW + timedelta(seconds=1))
    assert all(part["part"] != "waiting_by_person" for part in again["sections"])
    assert len(why_log.request_review_history()) == 1
    why_log.close()


def test_the_tools_check_scopes_and_map_errors(tmp_path: Path) -> None:
    from gremlin_town.mcp.brain_tools import build_orientation_tools

    ctx, why_log = context(tmp_path=tmp_path)

    async def authorize() -> None:
        return None

    read_only = {
        tool.name: tool
        for tool in build_orientation_tools(ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read"},
                                            authorize=authorize)
    }
    assert not asyncio.run(read_only["bc_orient"].run({})).isError
    assert not asyncio.run(read_only["bc_next"].run({})).isError
    denied = asyncio.run(read_only["bc_next_respond"].run({"surfacing_id": "brn_x", "response": "yes"}))
    assert denied.isError and denied.structuredContent["error"]["code"] == "ACCESS_DENIED"

    nobody = build_orientation_tools(ctx, lambda: None, scopes_provider=lambda: {"bc:read"}, authorize=authorize)
    assert all(asyncio.run(tool.run(_ANY_ARGUMENTS.get(tool.name, {})))
               .structuredContent["error"]["code"] == "ACCESS_DENIED" for tool in nobody)

    full = {
        tool.name: tool
        for tool in build_orientation_tools(ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read", "bc:write"},
                                            authorize=authorize)
    }
    # An answer still needs a shown why-log row, even though tier B cannot change work.
    answer = asyncio.run(full["bc_next_respond"].run({"surfacing_id": "brn_never", "response": "yes"}))
    assert answer.isError
    why_log.close()


def test_the_tools_report_a_broken_why_log_as_an_error(tmp_path: Path) -> None:
    from gremlin_town.mcp.brain_tools import build_orientation_tools

    ctx, why_log = context(tmp_path=tmp_path)
    ctx = replace(ctx, why_log=None)

    async def authorize() -> None:
        return None

    tools = build_orientation_tools(ctx, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read", "bc:write"},
                                    authorize=authorize)
    # bc_agenda and bc_weekly only filter records and write no why-log row, so they still answer.
    assert all(asyncio.run(tool.run(_ANY_ARGUMENTS.get(tool.name, {}))).isError
               for tool in tools if tool.name not in ("bc_agenda", "bc_weekly"))
    agenda = next(tool for tool in tools if tool.name == "bc_agenda")
    assert asyncio.run(agenda.run({"person": "Sam"})).structuredContent["outcome"] == "answered"
    why_log.close()


# --- Time blocks (Ciel and Brain Home PRD §4.3; release ciel-03 slice 10), off until the consent ------


def _blocks_on(ctx: OrientationContext) -> OrientationContext:
    return replace(ctx, orientation_settings=replace(ctx.orientation_settings, blocks=True))


def _day(start, end):
    """Nic's blocks today, from the calendar reader: deep focus 10 to 1, admin 2 to 3 (Singapore)."""

    sgt = timezone(timedelta(hours=8))
    return (
        CalendarEvent(title="Deep focus", start=datetime(2026, 10, 4, 10, tzinfo=sgt), end=datetime(2026, 10, 4, 13, tzinfo=sgt)),
        CalendarEvent(title="Admin", start=datetime(2026, 10, 4, 14, tzinfo=sgt), end=datetime(2026, 10, 4, 15, tzinfo=sgt)),
    )


def test_with_blocks_on_what_next_offers_the_blocks_kind_of_work_and_says_so(tmp_path: Path) -> None:
    """Acceptance case 4 through GT: at 11:00 inside the deep-focus block, the admin form waits for
    the 2 pm admin block."""

    report = wrap(replace(make_item(id="brn_report", title="Write the report"),
                          data={"level": "accepted", "size": "task", "block": "deep_focus"}))
    form = wrap(replace(make_item(id="brn_form", title="Send the tax form"),
                        data={"level": "accepted", "size": "task", "block": "admin",
                              "timing": {"due": "2026-10-04", "at": "17:00"}}))
    ctx, why_log = context(tmp_path=tmp_path, items=(report, form), calendar_events=_day)
    eleven = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)
    off = what_next(ctx, eleven)
    assert "moment" not in off and "question" not in off  # off: exactly as before
    answer = what_next(_blocks_on(ctx), eleven)
    assert answer["top_pick"]["record_id"] == "brn_report"
    assert answer["moment"]["kind"] == "deep_focus" and answer["moment"]["source"] == "block"
    assert [(entry["record_id"], entry["reason"]) for entry in answer["can_wait"]] == [
        ("brn_form", "waits for the 2 pm admin block"),
    ]
    why_log.close()


def test_bc_next_takes_nics_override_only_while_blocks_are_on(tmp_path: Path) -> None:
    """Acceptance case 5 through the tool: after "I'm out", errands are offered."""

    from gremlin_town.mcp.brain_tools import build_orientation_tools

    shop = wrap(replace(make_item(id="brn_shop", title="Buy boxes"),
                        data={"level": "accepted", "size": "task", "block": "errands"}))
    report = wrap(replace(make_item(id="brn_report", title="Write the report"),
                          data={"level": "accepted", "size": "task", "block": "deep_focus"}))
    ctx, why_log = context(tmp_path=tmp_path, items=(report, shop), calendar_events=_day)

    async def authorize() -> None:
        return None

    def tools(context_):
        return {tool.name: tool for tool in build_orientation_tools(
            context_, lambda: Actor("chatgpt-web"), scopes_provider=lambda: {"bc:read"}, authorize=authorize)}

    assert "override" not in tools(ctx)["bc_next"].parameters["properties"]
    on = tools(_blocks_on(ctx))["bc_next"]
    assert {"override", "override_words", "minutes"} <= set(on.parameters["properties"])
    assert "pass override" in on.description
    answer = asyncio.run(on.run({"override": "out", "override_words": "I'm out"})).structuredContent
    assert answer["top_pick"]["record_id"] == "brn_shop"
    assert answer["moment"]["source"] == "override" and answer["moment"]["kind"] == "errands"
    refused = asyncio.run(on.run({"override": "out"}))
    assert refused.isError and "override_words" in refused.structuredContent["error"]["message"]
    why_log.close()
