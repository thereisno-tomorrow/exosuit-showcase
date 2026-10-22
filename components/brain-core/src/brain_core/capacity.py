"""How near Brain Home is to the limits of the scans that read it.

The storage-limit plan (docs/product/2026-09-30-brain-home-storage-limit-plan.md §4, §5): a host
lists each scan it runs - the record kinds it reads together, its file cap and byte cap - and
`headroom` says, for each, how full it is, a level and one plain sentence. The tree GitHub lists
gets the same levels by its entry count. Pure: counts in, levels and words out. No GitHub, no
model call.

Levels: `ok` below half a cap, `heads_up` from 50%, `urgent` from 80% (a scan still works at its
cap), `over` past it (the scan refuses). The tree: `heads_up` from 10,000 entries, `urgent` from
20,000, `over` past about 26,000, where GitHub stops listing it whole (inferred: GitHub truncates a
recursive tree past 7 MB, at about 270 bytes an entry).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

OK = "ok"
HEADS_UP = "heads_up"
URGENT = "urgent"
OVER = "over"
_RANK = {OK: 0, HEADS_UP: 1, URGENT: 2, OVER: 3}

TREE_HEADS_UP_ENTRIES = 10_000
TREE_URGENT_ENTRIES = 20_000
TREE_LIMIT_ENTRIES = 26_000  # inferred, not measured
TREE_NAME = "Brain Home's file list"

# What fixes a scan's limit (the storage-limit plan's layer 2), said once in full in the heads-up.
TIDY_UP = "the storage tidy-up (old versions moved into Brain Home's archive)"
TIDY_UP_NOW = "Ask for the storage tidy-up now."
# The tidy-up only moves files within the tree, so the tree's own fix is the hub no longer listing the
# archive at all (the plan's layer 3).
TREE_FIX = "the hub to stop listing the archive"
NOTHING_LOST = "Nothing is lost."
# For a save or change that failed on a limit: it did not land, and nothing already there is harmed.
NOTHING_SAVED = "Nothing was saved; what's already saved is safe."

# Plain words for each kind, in the order they are named.
_KIND_WORDS = (
    ("item", "task"),
    ("fact", "fact"),
    ("capture", "capture"),
    ("commitment", "commitment"),
    ("preference", "preference"),
    ("continuity", "note"),
    ("sitrep", "project summary"),
    ("focus_copy", "project summary"),
    ("decision", "decision"),
)


def records_name(kinds: Iterable[str]) -> str:
    """What a scan of ``kinds`` reads, in plain words: "task records", "task and fact records"."""

    wanted = set(kinds)
    words: list[str] = []
    for kind, word in _KIND_WORDS:
        if kind in wanted and word not in words:
            words.append(word)
    words += sorted(kind for kind in wanted if kind not in dict(_KIND_WORDS))
    if not words:
        return "records"
    if len(words) == 1:
        return f"{words[0]} records"
    return f"{', '.join(words[:-1])} and {words[-1]} records"


def megabytes(value: int) -> str:
    return f"{value / 1_048_576:.1f} MB"


@dataclass(frozen=True, slots=True)
class Scan:
    """One scan a host runs: the kinds it reads together and its caps. ``stops`` says, in plain
    words, what stops working past the cap ("the review, placement and 'where am I?' stop seeing
    your tasks"); ``name`` defaults to `records_name` of the kinds."""

    kinds: frozenset[str]
    max_files: int
    max_bytes: int | None = None
    stops: str = "the hub stops reading them"
    name: str = ""

    def __post_init__(self) -> None:
        if not self.kinds:
            raise ValueError("a scan reads at least one kind")
        if not isinstance(self.max_files, int) or isinstance(self.max_files, bool) or self.max_files <= 0:
            raise ValueError("max_files must be a positive integer")
        if self.max_bytes is not None and (
            not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool) or self.max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer or None")
        if not self.name:
            object.__setattr__(self, "name", records_name(self.kinds))


@dataclass(frozen=True, slots=True)
class Headroom:
    """How full one scan (or the tree) is. ``share`` is the larger of files and bytes against
    their caps; ``measure`` says which one it was."""

    name: str
    kinds: tuple[str, ...]
    files: int
    bytes: int
    max_files: int
    max_bytes: int | None
    share: float
    measure: str
    level: str
    sentence: str


class Counts(Protocol):
    """What `brain_core.github_storage.GitHubRecordCounts` offers that this needs."""

    live_files: Mapping[str, int]
    live_bytes: Mapping[str, int]
    tree_entries: int


def _level(found: int, cap: int) -> str:
    if found > cap:
        return OVER
    if found * 5 >= cap * 4:
        return URGENT
    if found * 2 >= cap:
        return HEADS_UP
    return OK


def over_sentence(
    name: str, found: int, cap: int, *, measure: str = "files", at_least: bool = False, nothing_saved: bool = False,
) -> str:
    """The sentence for a scan past its cap. ``at_least``: ``found`` is where the scan stopped
    counting, not the total (a byte cap stops reading as soon as it is passed). ``nothing_saved``:
    it stopped a save or change, so say that nothing was saved rather than only that nothing is lost."""

    outcome = NOTHING_SAVED if nothing_saved else NOTHING_LOST
    if measure == "bytes":
        size = f"more than {megabytes(cap)}" if at_least else megabytes(found)
        return (
            f"Brain Home's {name} take {size} and the hub reads at most {megabytes(cap)} at once. "
            f"{outcome} {TIDY_UP_NOW}"
        )
    return f"Brain Home holds {found:,} {name} and the hub reads at most {cap:,} at once. {outcome} {TIDY_UP_NOW}"


def _scan_sentence(scan: Scan, level: str, measure: str, found: int, cap: int) -> str:
    if level == OVER:
        return over_sentence(scan.name, found, cap, measure=measure)
    if measure == "bytes":
        now, limit = megabytes(found), megabytes(cap)
        if level == URGENT:
            return f"your {scan.name} take {now} of {limit}. Past {limit}, {scan.stops}. {TIDY_UP_NOW}"
        if level == HEADS_UP:
            return (
                f"your {scan.name} take {now} of the {limit} the hub reads at once. {NOTHING_LOST} "
                f"Past {limit}, {scan.stops}; ask for {TIDY_UP} before then."
            )
        return f"your {scan.name} take {now} of {limit}."
    if level == URGENT:
        return f"your {scan.name} are at {found:,} of {cap:,}. Past {cap:,}, {scan.stops}. {TIDY_UP_NOW}"
    if level == HEADS_UP:
        return (
            f"your {scan.name} are at {found:,} of the {cap:,} the hub reads at once. {NOTHING_LOST} "
            f"Past {cap:,}, {scan.stops}; ask for {TIDY_UP} before then."
        )
    return f"your {scan.name} are at {found:,} of {cap:,}."


def scan_headroom(scan: Scan, counts: Counts) -> Headroom:
    files = sum(int(counts.live_files.get(kind, 0)) for kind in scan.kinds)
    size = sum(int(counts.live_bytes.get(kind, 0)) for kind in scan.kinds)
    by_files = _level(files, scan.max_files)
    by_bytes = OK if scan.max_bytes is None else _level(size, scan.max_bytes)
    file_share = files / scan.max_files
    byte_share = 0.0 if scan.max_bytes is None else size / scan.max_bytes
    if _RANK[by_bytes] > _RANK[by_files] or (_RANK[by_bytes] == _RANK[by_files] and byte_share > file_share):
        level, measure, found, cap, share = by_bytes, "bytes", size, scan.max_bytes, byte_share
    else:
        level, measure, found, cap, share = by_files, "files", files, scan.max_files, file_share
    assert cap is not None
    return Headroom(
        name=scan.name, kinds=tuple(sorted(scan.kinds)), files=files, bytes=size, max_files=scan.max_files,
        max_bytes=scan.max_bytes, share=share, measure=measure, level=level,
        sentence=_scan_sentence(scan, level, measure, found, cap),
    )


def tree_headroom(entries: int) -> Headroom:
    if entries > TREE_LIMIT_ENTRIES:
        level = OVER
        sentence = (
            f"{TREE_NAME} has {entries:,} entries, past the about {TREE_LIMIT_ENTRIES:,} GitHub lists whole, "
            f"so every Brain Home read stops. {NOTHING_LOST} Ask for {TREE_FIX} now."
        )
    elif entries >= TREE_URGENT_ENTRIES:
        level = URGENT
        sentence = (
            f"{TREE_NAME} has {entries:,} entries of about {TREE_LIMIT_ENTRIES:,}. Past that, every Brain Home "
            f"read stops. Ask for {TREE_FIX} now."
        )
    elif entries >= TREE_HEADS_UP_ENTRIES:
        level = HEADS_UP
        sentence = (
            f"{TREE_NAME} has {entries:,} entries; GitHub lists it whole only up to about "
            f"{TREE_LIMIT_ENTRIES:,}, and past that every Brain Home read stops. {NOTHING_LOST} "
            f"Ask for {TREE_FIX} before then."
        )
    else:
        level = OK
        sentence = f"{TREE_NAME} has {entries:,} entries of about {TREE_LIMIT_ENTRIES:,}."
    return Headroom(
        name=TREE_NAME, kinds=(), files=entries, bytes=0, max_files=TREE_LIMIT_ENTRIES, max_bytes=None,
        share=entries / TREE_LIMIT_ENTRIES, measure="entries", level=level, sentence=sentence,
    )


def headroom(counts: Counts, scans: Iterable[Scan]) -> tuple[Headroom, ...]:
    """Each scan's headroom, in the order given, then the tree's last."""

    return (*(scan_headroom(scan, counts) for scan in scans), tree_headroom(int(counts.tree_entries)))


def worst(entries: Iterable[Headroom]) -> Headroom | None:
    """The fullest entry at a heads-up or worse (the highest level, then the largest share; the
    first on a tie), or None when all are fine."""

    found: Headroom | None = None
    for entry in entries:
        if entry.level == OK:
            continue
        if found is None or (_RANK[entry.level], entry.share) > (_RANK[found.level], found.share):
            found = entry
    return found


def level_rank(level: str) -> int:
    return _RANK[level]
