#!/usr/bin/env python3
"""Storage layer 2 maintenance. Dry run by default; never deletes a Git blob.

--config CONFIG [--apply --expected-head SHA --moved-list FILE]
--config CONFIG --restore FILE --apply --expected-head SHA

The switch is enabled separately before apply and disabled before restore.
FILE journals path/blob pairs before each conditional commit, so an uncertain
reply can be checked and undone. This script is built/tested on fakes only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from brain_core.github_storage import (
    MAX_ARCHIVE_PATHS, SCAN_KINDS, GitHubConflict, GitHubStorageError, _record_id_created_at,
)
from brain_core.routines import is_routine


def _live(storage, revision):
    blobs = storage._tree_blobs(revision)
    paths = tuple(p for p in blobs if p.startswith(tuple(f"records/{k}/" for k in SCAN_KINDS)) and p.endswith(".md"))
    records = {}
    for item in storage._read_blobs(revision, paths, blobs, missing_ok=False).files:
        record = storage._parse(item, revision)
        if item.path != f"records/{record.kind}/{record.created_at:%Y/%m}/{record.id}.md":
            raise GitHubStorageError("github_record_invalid", "Record differs from its path", details={"path": item.path})
        records[(record.kind, record.id)] = (item.path, record)
    replaced = {(r.kind, old) for _, r in records.values() for old in r.supersedes}
    current = {key: (path, blobs[path]) for key, (path, _) in records.items() if key not in replaced}
    return blobs, records, current


def plan(storage, revision=None):
    """Pin a complete tree, check reachable history, and list safe moves and leftovers."""
    revision = revision or storage.revision()
    blobs, live, current = _live(storage, revision)
    moves, left = {}, []
    for key, (current_path, _) in sorted(current.items()):
        record = live[key][1]
        if is_routine(record):
            continue
        declared = set(record.supersedes)
        pending, seen, candidates, problems = list(record.supersedes), set(), {}, []
        while pending:
            old = pending.pop()
            if old in seen:
                continue
            seen.add(old)
            at = _record_id_created_at(old)
            path = f"records/{record.kind}/{at:%Y/%m}/{old}.md"
            archived = "archive/" + path
            source = path if path in blobs else archived if archived in blobs else None
            if source is None:
                problems.append(f"missing {path}")
                continue
            if path in blobs and archived in blobs and blobs[path] != blobs[archived]:
                raise GitHubConflict("github_archive_conflict", "Archive contains a different blob", details={"path": archived})
            previous = storage._parse(storage._read_blobs(revision, (source,), blobs, missing_ok=False).file(source), revision)
            if previous.id != old or previous.kind != record.kind or previous.created_at.strftime("%Y/%m") != at.strftime("%Y/%m"):
                raise GitHubStorageError("github_record_invalid", "History differs from its path", details={"path": path})
            if is_routine(previous):
                problems.append(f"routine {path}")
            missing_history = set(previous.supersedes) - declared
            if missing_history:
                problems.append(f"{current_path} omits history: {', '.join(sorted(missing_history))}")
            pending.extend(previous.supersedes)
            if path in blobs:
                candidates[path] = blobs[path]
        if problems:
            left.extend(problems)
        else:
            moves.update(candidates)
    counts = {}
    for kind in sorted(SCAN_KINDS):
        count = sum(r.kind == kind for _, r in live.values())
        moving = sum(path.startswith(f"records/{kind}/") for path in moves)
        counts[kind] = {"live_now": count, "to_move": moving, "live_after": count - moving}
    return {"head": revision.commit_sha, "tree": revision.tree_sha, "counts": counts,
            "moves": moves, "current": {path: blob for path, blob in current.values()}, "left_alone": sorted(set(left))}


def _journal(path, moves, *, repository, branch):
    value = {"schema": "brain.archive-moves/v1", "repository": repository, "branch": branch, "moves": moves}
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("x", encoding="utf-8") as file:
        file.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def execute(storage, *, apply=False, expected_head=None, moved_list=None, restore=None):
    """Fixture-testable command body. Refuse surprises; recompute after a raced head."""
    revision = storage.revision()
    if apply and expected_head != revision.commit_sha:
        raise GitHubConflict("github_revision_conflict", "Apply requires the exact dry-run head")
    if restore is not None:
        if storage.archive_superseded:
            raise ValueError("Disable archive_superseded before restore")
        journal = json.loads(restore.read_text(encoding="utf-8"))
        if (journal.get("schema"), journal.get("repository"), journal.get("branch")) != (
            "brain.archive-moves/v1", storage.repository, storage.branch,
        ):
            raise ValueError("Moved list belongs to another repository or branch")
        moves = journal["moves"]
        for offset in range(0, len(moves), MAX_ARCHIVE_PATHS):
            storage._archive_paths(list(moves)[offset:offset + MAX_ARCHIVE_PATHS], SCAN_KINDS | {"entity"})
        blobs = storage._tree_blobs(revision)
        for path, blob in moves.items():
            if blobs.get("archive/" + path, blobs.get(path)) != blob or (path in blobs and blobs[path] != blob):
                raise GitHubConflict("github_archive_conflict", "Restore blob differs from the moved list", details={"path": path})
        if not apply:
            return {"head": revision.commit_sha, "restore": moves}
        for offset in range(0, len(moves), MAX_ARCHIVE_PATHS):
            storage.restore_archived_records(paths=list(moves)[offset:offset + MAX_ARCHIVE_PATHS], expected_revision=revision.commit_sha)
            revision = storage.revision()
            tree = storage._tree_blobs(revision)
            for path in list(moves)[offset:offset + MAX_ARCHIVE_PATHS]:
                if tree.get(path) != moves[path] or "archive/" + path in tree:
                    raise GitHubStorageError("github_archive_verification_failed", "Restore did not preserve its blob", details={"path": path})
        return {"head": revision.commit_sha, "restored": len(moves)}
    before = plan(storage, revision)
    if not apply:
        return before
    if not storage.archive_superseded:
        raise ValueError("Enable archive_superseded before the one-time move")
    if moved_list is None or moved_list.exists():
        raise ValueError("Apply needs a new --moved-list file; preserve an existing journal")
    moved = {}
    races = 0
    while before["moves"]:
        batch = dict(list(before["moves"].items())[:MAX_ARCHIVE_PATHS])
        # Journal first: even a lost reply retains the exact intended path/blob pairs.
        moved.update(batch)
        _journal(moved_list, moved, repository=storage.repository, branch=storage.branch)
        try:
            storage.archive_superseded_records(paths=list(batch), expected_revision=before["head"])
        except GitHubConflict as race:
            if race.code != "github_revision_conflict":
                raise
            races += 1
            if races >= 2:
                raise GitHubConflict("github_revision_conflict", "Branch raced twice; inspect the journal and rerun a dry run") from race
            before = plan(storage)
            continue  # changed head: recomputed plan, never an unchanged retry
        after = plan(storage)
        blobs = storage._tree_blobs(storage._revision_at(after["head"]))
        if before["current"] != after["current"] or any(path in blobs or blobs.get("archive/" + path) != blob for path, blob in batch.items()):
            raise GitHubStorageError("github_archive_verification_failed", "Current records or moved blobs changed unexpectedly")
        before = after
        races = 0
    _journal(moved_list, moved, repository=storage.repository, branch=storage.branch)
    return {**before, "moved": moved}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--moved-list", type=Path)
    parser.add_argument("--restore", type=Path)
    args = parser.parse_args(arguments)
    if args.apply and not args.expected_head:
        parser.error("--apply requires --expected-head")
    from gremlin_town.runtime import build_runtime, _orientation_brain_storage
    runtime = build_runtime(args.config, start_worker=False)
    try:
        storage = _orientation_brain_storage(runtime.brain)
    finally:
        runtime.close()
    if storage is None:
        raise ValueError("Needs [brain_core] enabled")
    print(json.dumps(execute(storage, apply=args.apply, expected_head=args.expected_head,
                             moved_list=args.moved_list, restore=args.restore), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"STOP: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
