"""Brain Home reading for orientation and the review: v2 items, facts, preferences and open
where-I-stopped notes.

Reads directly through Brain Core's existing public `GitHubBrainHomeStorage`
interface (`snapshot_records`/`read_records`) - not step C2's not-yet-merged
v2 save-tool format (unneeded for reads), and not GT's async `BrainRuntime`,
which wraps the same storage with per-call MCP authorization overhead that a
read-only, host-configured adapter doesn't need (`runtime.py`'s own
`_google_calendar_reader`/`_review_open_requests_reader` are the same shape:
a small, self-contained, synchronous adapter function).

`SnapshotSource` is the narrow structural interface actually used
(`GitHubBrainHomeStorage` satisfies it as-is); tests pass a trivial stub
instead of mocking GitHub's HTTP API, which Brain Core's own test suite
(`tests/unit/test_github_storage.py`) already proves against a fake transport.

`runtime.py` wires the same readers into step E's `ReviewSources` and into
`OrientationContext` (one storage instance); the preference and task-sitrep
readers are orientation's only.

Brain Home's size (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1): with
`[review]` on, every reader here goes through the hub's copy of Brain Home with the hub's limits
(5,000 files, 32 MiB a scan); `HUB_SCANS` lists every scan the hub runs, for the size check and the
06:00 review's "Brain Home size" line; and `plain_brain_problem` turns a scan past its cap, a tree
too big to list and GitHub's used-up allowance into one plain sentence, so every surface says the
same thing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, Sequence

from brain_core.capacity import (
    NOTHING_LOST,
    NOTHING_SAVED,
    TIDY_UP_NOW,
    Headroom,
    Scan,
    headroom,
    over_sentence,
    records_name,
)
from brain_core.github_storage import MAX_DECISION_CANDIDATES, SCAN_KINDS, SNAPSHOT_MAX_BYTES, GitHubStorageError
from brain_core.records import Record
from brain_core.timewords import SINGAPORE

from gremlin_town.mcp.brain_home_copy import SCAN_MAX_BYTES, SCAN_MAX_RECORDS

logger = logging.getLogger(__name__)

_OPEN_CONTINUITY_STATES = frozenset({"open", "recovered"})
_ITEM_KIND = frozenset({"item"})
_FACT_KIND = frozenset({"fact"})
_PREFERENCE_KIND = frozenset({"preference"})
_CONTINUITY_KIND = frozenset({"continuity"})
# The hub's task sitreps and Focus copies (docs/product/steps/b-project-sitreps.md).
_SITREP_KINDS = frozenset({"sitrep", "focus_copy"})


class _RecordFile(Protocol):
    path: str
    record: Record


class _RecordRead(Protocol):
    records: tuple[_RecordFile, ...]


class SnapshotSource(Protocol):
    """What `GitHubBrainHomeStorage` actually offers that reading needs."""

    def snapshot_records(
        self, *, kinds: frozenset[str], max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES,
    ) -> _RecordRead: ...

    def read_records(self, paths: Sequence[str], *, archived_ok: bool = False) -> _RecordRead: ...


# Every scan the hub runs over Brain Home after layer 1, with its caps (the plan's §4 table): the
# size check (`gremlin-town brain-home size`) and the 06:00 review's "Brain Home size" line measure
# these. Search and agents' list read several kinds as one set, so they reach a cap first.
HUB_SCANS = (
    Scan(frozenset({"item"}), SCAN_MAX_RECORDS, SCAN_MAX_BYTES,
         stops="the review, placement, \"where am I?\" and \"what next?\" stop seeing your tasks"),
    Scan(frozenset({"item", "fact"}), SCAN_MAX_RECORDS, SCAN_MAX_BYTES, stops="agents can't list your tasks"),
    Scan(frozenset({"capture", "commitment", "item", "fact"}), SCAN_MAX_RECORDS, SCAN_MAX_BYTES,
         stops="agents can't search Brain Home"),
    Scan(frozenset({"fact"}), SCAN_MAX_RECORDS, SCAN_MAX_BYTES, stops="the review stops showing your answers"),
    # Placement reads the working hours first, so past this limit it plans nothing at all.
    Scan(frozenset({"preference"}), SCAN_MAX_RECORDS, SCAN_MAX_BYTES,
         stops="placement plans nothing and your focus projects stop counting"),
    Scan(frozenset({"continuity"}), SCAN_MAX_RECORDS, SCAN_MAX_BYTES,
         stops="notes stop updating and agents can't read them"),
    Scan(frozenset({"sitrep", "focus_copy"}), SCAN_MAX_RECORDS, SCAN_MAX_BYTES,
         stops="project summaries stop updating"),
    Scan(frozenset({"decision"}), MAX_DECISION_CANDIDATES, None, stops="new decisions can't be recorded"),
)

# GitHub stopped listing the tree, or a reply was over the transport's cap: waiting won't help, and the
# storage tidy-up doesn't shrink the tree, so the sentence asks for the check that says which.
# The day-planning readers' scans; the hub runs them within the limits in force (512 files without the
# copy file), so the size line measures them against those.
_READER_SCAN_KINDS = frozenset({
    frozenset({"item"}), frozenset({"fact"}), frozenset({"preference"}), frozenset({"continuity"}),
    frozenset({"sitrep", "focus_copy"}),
})


def hub_scans(*, max_records: int = SCAN_MAX_RECORDS, max_bytes: int = SCAN_MAX_BYTES) -> tuple[Scan, ...]:
    """`HUB_SCANS` with the day-planning readers' scans at the limits in force: the hub's 5,000 files and
    32 MiB with the copy file, Brain Core's 512 and 8 MiB without it (`runtime.py`). Agents' list and
    search and decisions run in the service and keep their own limits."""

    return tuple(
        replace(scan, max_files=max_records, max_bytes=max_bytes) if scan.kinds in _READER_SCAN_KINDS else scan
        for scan in HUB_SCANS
    )


_TOO_BIG = (
    "Brain Home has grown too big for GitHub to hand over in one go, so the hub can't read it. "
    "{outcome} Ask for the Brain Home size check."
)
# GitHub answered with another tree than the one asked for: odd, and not a size problem.
_DIFFERENT_TREE = (
    "GitHub answered with a different version of Brain Home than the one asked for, so the hub couldn't read it "
    "this time. {outcome} It should work again shortly."
)


def _storage_error(exc: BaseException) -> GitHubStorageError | None:
    """The Brain Core storage error behind ``exc``, if any (an adapter may have wrapped it)."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, GitHubStorageError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def brain_problem(exc: BaseException, *, nothing_saved: bool = False) -> tuple[str, bool] | None:
    """One plain sentence for a Brain Home read that failed on a size limit, a tree GitHub didn't
    hand over, or GitHub's hourly allowance, and whether it lasts (it won't pass by itself); None for
    any other failure. ``nothing_saved``: the failure stopped a save or change before it landed, so
    the sentence says nothing was saved instead of only that nothing is lost."""

    error = _storage_error(exc)
    if error is None:
        return None
    details = error.details
    outcome = NOTHING_SAVED if nothing_saved else NOTHING_LOST
    if error.code == "github_scan_limit":
        name = records_name(details.get("kinds") or ())
        max_bytes = _count(details.get("max_bytes"))
        if details.get("limit") == "bytes" and max_bytes:
            found = _count(details.get("bytes")) or max_bytes + 1
            return over_sentence(
                name, found, max_bytes, measure="bytes", at_least=True, nothing_saved=nothing_saved,
            ), True
        files, max_records = _count(details.get("files")), _count(details.get("max_records"))
        if files is not None and max_records:
            return over_sentence(name, files, max_records, nothing_saved=nothing_saved), True
        return f"Brain Home holds more {name} than the hub reads at once. {outcome} {TIDY_UP_NOW}", True
    if error.code == "github_decision_candidate_limit":
        found, cap = _count(details.get("candidates")), _count(details.get("max_candidates"))
        if found is not None and cap:
            return over_sentence("decision records", found, cap, nothing_saved=nothing_saved), True
        return f"Brain Home holds more decision records than the hub reads at once. {outcome} {TIDY_UP_NOW}", True
    if error.code == "github_tree_incomplete" and details.get("tree_matches") is False and not details.get("truncated"):
        return _DIFFERENT_TREE.format(outcome=outcome), False
    if error.code in {"github_tree_incomplete", "github_response_too_large"}:
        return _TOO_BIG.format(outcome=outcome), True
    if details.get("rate_limited"):
        reset = _count(details.get("reset"))
        when = "until " + f"{datetime.fromtimestamp(reset, SINGAPORE):%H:%M}" if reset is not None else "for now"
        if nothing_saved:
            again = "Try again then." if reset is not None else "Try again within the hour."
            return f"GitHub's hourly allowance for Brain Home is used up {when}, so nothing was saved. {again}", False
        again = "it works again then." if reset is not None else "it refills within the hour."
        return f"GitHub's hourly allowance for Brain Home is used up {when}. Nothing is lost; {again}", False
    return None


def plain_brain_problem(exc: BaseException) -> str | None:
    """`brain_problem`'s sentence alone, or None. For reads: it says nothing is lost."""

    problem = brain_problem(exc)
    return None if problem is None else problem[0]


def lasting_brain_problem(exc: BaseException) -> str | None:
    """The sentence, only for a problem that won't pass by itself (a size limit), else None."""

    problem = brain_problem(exc)
    return problem[0] if problem is not None and problem[1] else None


def describe_failure(exc: BaseException) -> str:
    """How a source's failure reads under "Couldn't see": the plain sentence when there is one,
    else the error's type and message, as before."""

    return plain_brain_problem(exc) or f"{type(exc).__name__}: {exc}"


class CountsSource(Protocol):
    """What `GitHubBrainHomeStorage` offers that the size check needs."""

    def record_counts(self) -> object: ...


def brain_home_size_reader(source: CountsSource, scans: tuple[Scan, ...] = HUB_SCANS):
    """How full each of the hub's scans is, and the tree (Brain Core's `capacity.headroom`), from
    one tree read and no file read. For the 06:00 review's "Brain Home size" line."""

    def read() -> tuple[Headroom, ...]:
        return headroom(source.record_counts(), scans)  # type: ignore[arg-type]

    return read


def _as_item_records(read: _RecordRead) -> tuple:
    from brain_core.review import ItemRecord

    return tuple(ItemRecord(record=file.record, path=file.path) for file in read.records)


@dataclass(frozen=True)
class ReviewSnapshot:
    """Items and preferences from one pinned revision; discarded after the tap."""
    revision: object
    items: tuple
    preferences: tuple


def review_snapshot_reader(source, *, max_records=512, max_bytes=SNAPSHOT_MAX_BYTES):
    """Pin once while retaining each kind's existing independent scan limits."""
    def read():
        revision = source.revision()
        items = source.snapshot_records(kinds=_ITEM_KIND, revision=revision,
                                        max_records=max_records, max_bytes=max_bytes)
        preferences = source.snapshot_records(kinds=_PREFERENCE_KIND, revision=revision,
                                              max_records=max_records, max_bytes=max_bytes)
        return ReviewSnapshot(revision, _as_item_records(items),
                              tuple(file.record for file in preferences.records))
    return read


def brain_items_reader(source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES):
    """A `ReviewSources`/`OrientationSources` `brain_items` adapter: every v2 item."""

    def read() -> tuple:
        return _as_item_records(
            source.snapshot_records(kinds=_ITEM_KIND, max_records=max_records, max_bytes=max_bytes)
        )

    return read


def brain_facts_reader(source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES):
    """A `ReviewSources.brain_facts` adapter: every v2 fact, from which Brain Core's
    `review.answers_by_task` picks Nic's answers about tasks (e-review-and-return.md §3.1)."""

    def read() -> tuple:
        return _as_item_records(
            source.snapshot_records(kinds=_FACT_KIND, max_records=max_records, max_bytes=max_bytes)
        )

    return read


def brain_preferences_reader(
    source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES,
):
    """An `OrientationSources.preferences` adapter: every preference record, from which Brain
    Core's `language.focus_preference` finds Nic's newest focus projects (N4, step F §3)."""

    def read() -> tuple:
        found = source.snapshot_records(kinds=_PREFERENCE_KIND, max_records=max_records, max_bytes=max_bytes)
        return tuple(file.record for file in found.records)

    return read


def brain_task_sitreps_reader(
    source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES,
):
    """An `OrientationSources.task_sitreps` adapter: every project's open and blocked tasks from
    the hub's task sitreps, with when the hub last wrote each project's summary (Brain Core's
    `sitreps.open_tasks_by_project`, ROA-60; step F §4, N4).

    Read-only, like every reader here. It does not go through the hub's `SitrepService` views
    (`project_view`/`list_view`): those retry queued sitrep writes and archive closed ones, and
    asking "where am I?" or "what next?" writes nothing but why-log rows (step F §2)."""

    def read() -> tuple:
        from brain_core.sitreps import open_tasks_by_project

        found = source.snapshot_records(kinds=_SITREP_KINDS, max_records=max_records, max_bytes=max_bytes)
        return open_tasks_by_project(file.record for file in found.records)

    return read


class _Found:
    def __init__(self, records: tuple) -> None:
        self.records = records


def brain_items_by_path_reader(
    source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES,
):
    """A `brain_items_by_path` adapter: the narrow set `collect_moments_candidates`
    (step E) and `respond` (step F's answer to a pick) name by path, not the whole
    store.

    A path the why-log pinned may since have moved into Brain Home's `archive/` folder, byte for
    byte (the storage-limit plan's layer 2): it is read from there, so the same bytes give the
    same answer. It is kept only when a current record of its kind names it in `supersedes`, so a
    thing archived whole, such as test data, never comes back. That check scans the kind, within
    ``max_records`` and ``max_bytes``, and only when an archived file was read."""

    def read(paths: Sequence[str]) -> tuple:
        found = tuple(source.read_records(tuple(paths), archived_ok=True).records)
        archived = [file for file in found if getattr(file, "archived", False)]
        unchecked = [file for file in archived if file.record.kind not in SCAN_KINDS]
        for file in unchecked:  # a kind no scan reads can't be checked: left out, loudly
            logger.warning("Leaving out archived %s record %s, which the hub can't check", file.record.kind, file.path)
        kinds = frozenset(file.record.kind for file in archived) & SCAN_KINDS
        named: set[str] = set()
        if kinds:
            live = tuple(
                file.record
                for file in source.snapshot_records(kinds=kinds, max_records=max_records, max_bytes=max_bytes).records
            )
            replaced = {old for record in live for old in record.supersedes}
            named = {old for record in live if record.id not in replaced for old in record.supersedes}
        if archived:
            found = tuple(file for file in found if not getattr(file, "archived", False) or file.record.id in named)
        return _as_item_records(_Found(found))

    return read


def current_open_notes(read: _RecordRead) -> tuple:
    """Every open or recovered note, newest version only, as Brain Core's `review.OpenNote`.

    A note never changes in place: each append or close writes a new version naming the old one
    in `supersedes`, and a snapshot returns every version. So only a version nothing supersedes
    counts - otherwise a closed note would still look open through its older versions (step B
    `continuity.list`'s own `current()` filter, applied here over `GitHubBrainHomeStorage`)."""

    from brain_core.review import OpenNote

    records = tuple(file.record for file in read.records)
    replaced = {old for record in records for old in record.supersedes}
    notes = []
    for record in records:
        if record.id in replaced or record.state not in _OPEN_CONTINUITY_STATES:
            continue
        data = record.data
        session_id, next_step = data.get("session_id"), data.get("next_step")
        if not isinstance(session_id, str) or not isinstance(next_step, str):
            continue
        task_id = data.get("task_id")
        intent = data.get("intent")
        notes.append(OpenNote(
            session_id=session_id,
            next_step=next_step,
            updated_at=record.updated_at,
            task_id=task_id if isinstance(task_id, str) else None,
            intent=intent if isinstance(intent, str) else "",
            record_id=record.id,
        ))
    notes.sort(key=lambda note: (note.updated_at, note.record_id or ""))
    return tuple(notes)


def open_note_records_reader(
    source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES,
):
    """A `ReviewSources.open_notes` adapter: every open note as an `OpenNote` (session, next step,
    last change, the task it names), newest version only. The 06:00 review asks for a started
    task's missing first step from these, and daytime returns find a first step in them (N12)."""

    def read() -> tuple:
        return current_open_notes(
            source.snapshot_records(kinds=_CONTINUITY_KIND, max_records=max_records, max_bytes=max_bytes)
        )

    return read


def open_notes_reader(source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES):
    """An `OrientationSources.open_notes` adapter: `session_id -> next_step` for every
    open or recovered continuity note (step B `continuity.list`'s own filter). Orientation's
    own note key is `item-<record_id>` (step F spec §6). Newest version only: before 28 Sep an
    older version of a note that had since closed could still show here."""

    def read() -> dict[str, str]:
        found = source.snapshot_records(kinds=_CONTINUITY_KIND, max_records=max_records, max_bytes=max_bytes)
        return {note.session_id: note.next_step for note in current_open_notes(found)}

    return read


def open_notes_by_task_reader(
    source: SnapshotSource, *, max_records: int = 512, max_bytes: int = SNAPSHOT_MAX_BYTES,
):
    """The same notes keyed by the task they name (N12b): `task_id -> next_step`, so a task's
    note is found whatever it is called. Nic's own notes only - a `gt-<request_id>` note is
    handed-off work, "Waiting on" rather than his next step (rebuild plan G2). With two notes
    for one task, the most recently changed wins."""

    def read() -> dict[str, str]:
        from brain_core.review import is_handoff_note

        found = source.snapshot_records(kinds=_CONTINUITY_KIND, max_records=max_records, max_bytes=max_bytes)
        by_task: dict[str, str] = {}
        for note in current_open_notes(found):  # oldest first, so the newest wins
            if note.task_id is not None and not is_handoff_note(note):
                by_task[note.task_id] = note.next_step
        return by_task

    return read
