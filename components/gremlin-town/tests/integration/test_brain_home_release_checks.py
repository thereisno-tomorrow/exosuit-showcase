"""The two read-only checks the orchestrator runs on a clone of Brain Home before installing layer 1 of the
storage-limit plan: `brain-home-readable.py --archive` and `brain-home-archived-names.py`."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

from brain_core.records import record_from_mapping, render_record

RELEASE = Path(__file__).resolve().parents[2] / "deploy" / "release"


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), RELEASE / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(record_id: str, *, supersedes: tuple[str, ...] = ()) -> tuple[str, bytes]:
    moment = (
        f"{record_id[4:8]}-{record_id[8:10]}-{record_id[10:12]}T"
        f"{record_id[13:15]}:{record_id[15:17]}:{record_id[17:19]}+00:00"
    )
    record = record_from_mapping({
        "schema": "brain.record/v2", "id": record_id, "kind": "item", "title": "File the form", "state": "open",
        "created_at": moment, "updated_at": moment, "sensitivity": "private",
        "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:item",
                      "content_sha256": hashlib.sha256(record_id.encode()).hexdigest(), "excerpt": "File the form"}],
        "supersedes": list(supersedes), "dedupe_key": None, "data": {"level": "accepted", "size": "task"},
        "body": "File the form",
        "source": {"said_by": "owner", "came_by": "direct", "words": "File the form", "recorded_by": "hermes-wa"},
    }, "release check fixture")
    return f"records/item/{moment[:4]}/{moment[5:7]}/{record_id}.md", render_record(record).encode("utf-8")


def _entity(record_id: str, *, supersedes: tuple[str, ...] = ()) -> tuple[str, bytes]:
    record = record_from_mapping({
        "schema": "brain.record/v1", "id": record_id, "kind": "entity", "title": "Sam", "state": "active",
        "created_at": "2026-09-21T04:00:00+00:00", "updated_at": "2026-09-21T04:00:00+00:00",
        "assertion": "explicit", "confidence": 1.0, "sensitivity": "private",
        "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:entity", "content_sha256": "e" * 64, "excerpt": "Sam"}],
        "supersedes": list(supersedes), "dedupe_key": None,
        "data": {"entity_type": "person", "why_it_matters": "a colleague", "privacy": "private", "linked_events": ["x"]},
        "body": "",
    }, "entity fixture")
    return f"records/entity/2026/09/{record_id}.md", render_record(record).encode("utf-8")


def _clone(tmp_path: Path) -> Path:
    """A Brain Home clone: one thing with its old version archived, one thing archived whole, one broken file,
    and an entity whose old version is archived (the hub never scans entities, so it stays not found)."""

    old, new, alone = "brn_20260926_010000_aaaa1111", "brn_20260926_020000_bbbb2222", "brn_20260926_030000_cccc3333"
    old_entity, new_entity = "brn_20260921_040000_eeee1111", "brn_20260921_040000_eeee2222"
    files = {
        "archive/" + _item(old)[0]: _item(old)[1],
        _item(new, supersedes=(old,))[0]: _item(new, supersedes=(old,))[1],
        "archive/" + _item(alone)[0]: _item(alone)[1],
        "archive/records/item/2026/09/brn_20260926_040000_dddd4444.md": b"not a record",
        "archive/" + _entity(old_entity)[0]: _entity(old_entity)[1],
        _entity(new_entity, supersedes=(old_entity,))[0]: _entity(new_entity, supersedes=(old_entity,))[1],
    }
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return tmp_path


def test_the_readable_check_reads_archive_only_when_asked(tmp_path: Path, capsys) -> None:
    readable = _script("brain-home-readable.py")
    root = _clone(tmp_path)

    assert readable.main([str(root)]) == 0
    assert "archived" not in capsys.readouterr().out

    assert readable.main(["--archive", str(root)]) == 1
    out = capsys.readouterr().out
    assert f"{root} archived item: 2 of 3 read" in out
    assert f"{root} archived entity: 1 of 1 read" in out
    assert "refused archive/records/item/2026/09/brn_20260926_040000_dddd4444.md" in out


def test_the_archived_names_check_lists_exactly_the_reads_whose_answer_changes(tmp_path: Path, capsys) -> None:
    names = _script("brain-home-archived-names.py")
    root = _clone(tmp_path)

    assert names.main([str(root)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "item brn_20260926_010000_aaaa1111: not found -> stale, named by brn_20260926_020000_bbbb2222 "
        "(archive/records/item/2026/09/brn_20260926_010000_aaaa1111.md)",
        "1 archived ID(s) will answer differently; 2 stay not found",
        "archived file that doesn't parse (stays not found): "
        "archive/records/item/2026/09/brn_20260926_040000_dddd4444.md: BrainError",
    ]
    assert names.main([str(tmp_path / "missing")]) == 2
