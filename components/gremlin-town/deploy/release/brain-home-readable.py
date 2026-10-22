"""brain-home-readable.py [--archive] ROOT [ROOT ...]

A read-only rollback check. It parses every record file the hub's Brain reads, and the PC connector's
project notes, with THIS interpreter's Brain Core, and lists each file that Brain Core refuses. Run it with
the Python of the release you would roll back to: that release's venv on the hub, or a venv holding that
release's Brain Core wheel on the PC. Point it at a fresh clone of Brain Home, or at a project's continuity
folder. It never writes.

Why it matters: the scans behind memory.list, continuity.list, the 06:00 review and Diablo's block read a
whole record kind at a time, and one record they refuse fails the whole read of that kind. From the Ciel
rebuild release on, records can carry fields older releases refuse: an item's `importance`, a
preference's `focus_projects`, a note's `task_id` or an empty next step. From release ciel-03 on (the
Ciel and Brain Home PRD §3, §4.1), also an item's `block`, `raise_with` or `first_step`, an importance
level `real_cost` or `minor`, and a timing's `start_by` or `estimated_by`; and routine occurrences (PRD §4.7).
Work-01 also accepts an item's work-shape fields: parent, order, called, next_action, whose, expected,
after, needs, dates (including start_by and confirmed), finish_line, labels, started_at, stake,
review_marks (including looks_like, ask_stake and spelling), and words_from. Any of these is refused by
pre-work-01 Brain Core. Work-01 reads both shapes; this check still only calls this interpreter's parser.

Only `records/<kind>/**/*.md` for the eight record kinds older releases know is read. The hub's scans never
open `records/sitrep/` or `records/focus_copy/` (the Ciel rebuild's own kinds) or `archive/`, so neither
does this check, unless:

`--archive` also reads `archive/records/<kind>/**/*.md` for the same kinds. From the storage-limit plan's layer 1
on, a read by record ID or by a pinned path can open an archived file; one that doesn't parse is skipped (the read
says not found) and logged, so run this with the release being installed, on a clone, to list them first.

Exit 0: this release reads every record. Exit 1: it refuses the files listed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from brain_core.records import parse_record

KINDS = ("capture", "commitment", "decision", "continuity", "entity", "preference", "item", "fact")


def main(arguments: list[str]) -> int:
    archive = "--archive" in arguments
    roots = [argument for argument in arguments if argument != "--archive"]
    if not roots:
        print(__doc__.strip().splitlines()[0])
        return 2
    refused = 0
    for root in map(Path, roots):
        if not (root / "records").is_dir():
            print(f"STOP: {root} has no records/ folder")
            return 2
        folders = [("", root / "records" / kind, kind) for kind in KINDS]
        if archive:
            folders += [("archived ", root / "archive" / "records" / kind, kind) for kind in KINDS]
        for label, folder, kind in folders:
            files = sorted(folder.glob("**/*.md"))
            bad = []
            for path in files:
                try:
                    parse_record(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
                except Exception as exc:  # noqa: BLE001 - each refusal is reported, and the exit code says so
                    details = getattr(exc, "details", None) or {}
                    reason = details.get("expected") or details.get("field") or type(exc).__name__
                    bad.append(f"  refused {path.relative_to(root).as_posix()}: {reason}")
            if files:
                print(f"{root} {label}{kind}: {len(files) - len(bad)} of {len(files)} read")
            for line in bad:
                print(line)
            refused += len(bad)
    print(f"{refused} record(s) this release refuses" if refused else "every record reads with this release")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
