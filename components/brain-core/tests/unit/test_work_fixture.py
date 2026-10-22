"""W0 synthetic exhibition scenarios parse; synthetic history is labelled apart."""

import importlib.util
import json
import hashlib
from pathlib import Path

import pytest

from brain_core.records import WORK_FIELDS, first_id, parse_record, render_record

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "work_model"
SPEC = importlib.util.spec_from_file_location("work_fixture_helpers", FIXTURE / "helpers.py")
helpers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helpers)
META = helpers.manifest()
PATHS = sorted(FIXTURE.glob("*/records/item/**/*.md"))


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.stem)
def test_every_fixture_record_parses_with_today_brain_core(path):
    record = parse_record(path.read_text(encoding="utf-8"), str(path))
    assert parse_record(render_record(record)) == record
    assert record.source["recorded_by"].startswith("fixture:")
    for evidence in record.evidence:
        assert evidence.source_type == "synthetic_fixture"
        assert evidence.locator.startswith("fixture://synthetic-exhibition/")
        assert evidence.content_sha256 == hashlib.sha256(evidence.excerpt.encode("utf-8")).hexdigest()


def test_audited_35_current_and_12_predecessors_keep_their_chains():
    records = {r.id: r for r in helpers.load_records()}
    current = set(META["audited_current_ids"])
    predecessors = set(META["audited_predecessor_ids"])
    assert len(current) == 35 and len(predecessors) == 12
    assert current.isdisjoint(predecessors)
    assert set(records) == current | predecessors
    assert {rid for r in records.values() for rid in r.supersedes} == predecessors
    assert all(not records[rid].supersedes for rid in predecessors)
    assert sum(r.data["level"] == "possible" for rid, r in records.items() if rid in current) == 15


def test_audited_open_things_match_plan_9_3_and_added_ones_are_apart():
    # Independent expected memberships for the fictional exhibition scenario.
    expected = {
        "brn_20260930_035653_d00000000000002c", "brn_20260930_020457_d000000000000023",
        "brn_20260929_134536_d000000000000011", "brn_20260930_035041_d000000000000029",
        "brn_20260929_123418_d000000000000010", "brn_20260929_123409_d00000000000000e",
        "brn_20260929_123405_d00000000000000d", "brn_20260929_105126_d00000000000000a",
        "brn_20260930_035714_d00000000000002d", "brn_20260930_035132_d00000000000002b",
        "brn_20260929_123356_d00000000000000b", "brn_20260930_035026_d000000000000028",
        "brn_20260926_034820_d000000000000002",
    }
    current = set(META["audited_current_ids"])
    actual = {r.id for r in helpers.load_records() if r.id in current
              and r.state in {"open", "blocked"} and r.data["level"] != "possible"}
    assert len(expected) == 13 and actual == expected == set(META["audited_open_ids"])
    made_up = helpers.load_records("made_up_post_audit")
    assert len(made_up) > 25
    assert {r.id for r in made_up} == set(META["made_up_post_audit_ids"])
    assert actual.isdisjoint(r.id for r in made_up)
    assert all(r.source["recorded_by"] == "fixture:made-up-post-audit" for r in made_up)
    cases = {name: next(r for r in made_up if r.id == rid) for name, rid in META["made_up_cases"].items()}
    assert cases["planned_due_today"].data["timing"]["due"] == "2026-09-30"
    assert cases["agent_first_step"].data["first_step"]
    assert cases["importance"].data["importance"]
    assert cases["blocked_by"].links[0]["target"] == cases["planned_due_today"].id
    assert cases["routine"].data["repeat"] == "every morning"


def test_unknown_history_is_labelled_and_not_invented():
    current = [r for r in helpers.load_records() if r.id in META["audited_current_ids"]]
    ideas = [r for r in current if r.data["level"] == "possible" and r.id not in META["test_data_ids"]]
    assert len(ideas) == 14
    assert all(not any(link["type"] == "came_from" for link in r.links) for r in ideas)
    assert all(any("came_from" in gap for gap in META["records"][r.id]["gaps"]) for r in ideas)
    assert sum(r.title.startswith("idea brn_") for r in ideas) == 11
    # The raw edit-about target must stay the first Send ID, not its current version.
    edit = next(r for r in current if r.id == "brn_20260930_035653_d00000000000002c")
    assert edit.links == ({"type": "about", "target": "brn_20260930_020454_d000000000000022"},)


def test_messages_preserve_repo_words_and_known_singapore_time():
    data = json.loads((FIXTURE / "messages_30_sep.json").read_text(encoding="utf-8"))
    messages = {m["id"]: m for m in data["messages"]}
    assert {f"R{i:02}" for i in range(1, 16)} <= messages.keys()
    assert {f"S{i:02}" for i in range(1, 9)} <= messages.keys()
    assert "S01b" in messages
    assert "safety checklist" in messages["M01"]["message"]
    assert "Exhibit Portal" in messages["M01"]["message"]
    assert all(m["source"].startswith("fixture://synthetic-exhibition/") for m in messages.values())
    assert all("Invented exhibition scenario" in m["provenance"] for m in messages.values())
    assert all(m["category"] != "real_message" for m in messages.values())
    assert messages["M01"]["said_at_sgt"] == "2026-09-30T11:50:00+08:00"
    assert messages["M07"]["said_at_sgt"] == "2026-09-30T00:10:00+08:00"
    for mid, message in messages.items():
        if mid.startswith(("R", "S")):
            assert message["message"] and message["said_at_sgt"] is None


def test_materialization_and_generated_200_record_probe(tmp_path):
    paths = helpers.write_fixture(tmp_path, include_made_up=True)
    assert len(paths) == 74
    assert all(path.is_relative_to(tmp_path / "records/item") for path in paths)
    generated = helpers.generated_reply_records()
    assert len(generated) == len({r.id for r in generated}) == 200
    assert all(r.state == "open" and not r.supersedes and not r.links for r in generated)


def test_work01_reads_the_whole_old_shape_fixture_and_preserves_first_ids():
    records = [parse_record(path.read_text(encoding="utf-8"), str(path)) for path in PATHS]
    assert len(records) == 74
    assert all(not WORK_FIELDS.intersection(record.data) for record in records)
    assert all(first_id(record) == (record.supersedes[0] if record.supersedes else record.id) for record in records)
