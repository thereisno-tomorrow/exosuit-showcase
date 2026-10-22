"""brain-home-archived-names.py ROOT

A read-only check before installing the storage-limit plan's layer 1: which reads of an old record ID will
answer differently. Point it at a fresh clone of Brain Home and run it with the Python of the release being
installed. It never writes.

Before layer 1, a read by record ID looked only under `records/`, so an ID whose file sits in `archive/` was
not found. From layer 1 on, `bc_memory_read` and `bc_memory_change` also look in `archive/`: an archived ID that a
live record of the same kind names in `supersedes` then reads as before its move (`freshness: stale`, with
`superseded_by`), and a change naming it is refused as changed. An archived ID no live record names (a thing
archived whole, such as test data) stays not found, and so does an archived file that doesn't parse.

It prints every archived ID a live record of its kind names, with the live IDs that name it: those are exactly
the reads whose answer changes. For capture, commitment, item, fact, preference and decision records (what
`memory.read` returns) the answer becomes stale; for a note (continuity) it becomes refused, as a live note's is.
An archived record of a kind the hub never scans (entity) stays not found whatever names it, so it is counted with
the rest that stay not found. Then each archived file that doesn't parse.

Exit 0 after printing; 2 when ROOT has no records/ folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

from brain_core.records import parse_record

KINDS = ("capture", "commitment", "decision", "continuity", "entity", "preference", "item", "fact")
# What memory.read returns; the hub also scans continuity; it never scans entity (brain_core.github_storage.SCAN_KINDS).
READABLE = frozenset({"capture", "commitment", "decision", "preference", "item", "fact"})
SCANNED = READABLE | {"continuity"}


def _records(folder: Path, root: Path) -> tuple[list[tuple[str, object]], list[str]]:
    parsed, broken = [], []
    for path in sorted(folder.glob("**/*.md")):
        relative = path.relative_to(root).as_posix()
        try:
            parsed.append((relative, parse_record(path.read_text(encoding="utf-8"), relative)))
        except Exception as exc:  # noqa: BLE001 - each broken file is listed, never hidden
            broken.append(f"{relative}: {type(exc).__name__}")
    return parsed, broken


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print(__doc__.strip().splitlines()[0])
        return 2
    root = Path(arguments[0])
    if not (root / "records").is_dir():
        print(f"STOP: {root} has no records/ folder")
        return 2
    changing = unnamed = 0
    broken_all: list[str] = []
    for kind in KINDS:
        live, broken_live = _records(root / "records" / kind, root)
        archived, broken_archived = _records(root / "archive" / "records" / kind, root)
        broken_all += broken_archived
        if not archived:
            continue
        named_by: dict[str, list[str]] = {}
        for _path, record in live:
            for old in record.supersedes:
                named_by.setdefault(old, []).append(record.id)
        for path, record in archived:
            names = named_by.get(record.id)
            if names and kind in SCANNED:
                changing += 1
                becomes = "stale" if kind in READABLE else "refused, as a live note is"
                print(f"{kind} {record.id}: not found -> {becomes}, named by {', '.join(sorted(names))} ({path})")
            else:
                unnamed += 1
        if broken_live:
            print(f"{kind}: {len(broken_live)} live file(s) don't parse; run brain-home-readable.py")
    print(f"{changing} archived ID(s) will answer differently; {unnamed} stay not found")
    for line in broken_all:
        print(f"archived file that doesn't parse (stays not found): {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
