"""W18 fixture-only planner proofs; no live Brain Home or network."""
import importlib.util
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from brain_core import work
from brain_core.errors import BrainError
from brain_core.records import WORK_FIELDS, first_id, parse_record, record_from_mapping, render_record
from brain_core.workmove import plan_move_over, plan_move_back
from brain_core.review import ItemRecord, MorningInputs, build_morning

FIXTURE = Path(__file__).parents[1] / "fixtures/work_model"
RULES_PATH = Path(__file__).parents[3] / "gremlin-town/src/gremlin_town/move_over_rules.json"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


helpers = module("w18_fixture", FIXTURE / "helpers.py")
expected = module("w18_expected", FIXTURE / "moved_over.py")


@pytest.fixture
def tree(tmp_path):
    helpers.write_fixture(tmp_path)
    return tmp_path


def rules():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def synthetic_old_record(number, *, state="open", timing=None, links=(), supersedes=(), level="accepted"):
    """Explicitly made-up cases; never reconstructed audited identities/facts."""
    value = deepcopy(helpers.load_records()[0].as_dict())
    words = (timing or {}).get("resolved_from", {}).get("words", "Made-up stale-link fixture")
    value.update(id=f"brn_20261001_000000_{0xf100 + number:016x}",
                 created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00",
                 title=f"Synthetic migration case {number}", body="Made-up fixture, not an audited owner record.",
                 state=state, supersedes=list(supersedes), links=list(links), dedupe_key=None,
                 data=dict(level=level, size="task", timing=timing),
                 source=dict(said_by="other", came_by="connected_source", speaker="synthetic migration fixture",
                             recorded_by="fixture:synthetic-workmove", words=words),
                 evidence=[dict(source_type="synthetic_fixture", locator="test_workmove.py",
                                excerpt=words, content_sha256=sha256(words.encode()).hexdigest())])
    return record_from_mapping(value, "synthetic workmove case")


def write_synthetic(root, record):
    path = root / f"records/item/{record.created_at:%Y/%m}/{record.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_record(record), encoding="utf-8")
    return path


@pytest.mark.parametrize("extra", ["start_by", "on_event", "on_return"])
def test_general_due_keeps_other_date_kinds_provenance_and_reader_meaning(tmp_path, extra):
    timing = dict(due="2026-10-10", resolved_from=dict(words="by 10 October", today="2026-09-30"))
    timing[extra] = "2026-10-05" if extra == "start_by" else "Synthetic approval event"
    old = synthetic_old_record(1, timing=timing)
    write_synthetic(tmp_path, old)
    own = work.write_index([old])[old.id]
    before_index = work.index_things([old])
    plan = plan_move_over(tmp_path, rules(), {}, helpers.NOW)
    assert len(plan.rows) == 1 and plan.rows[0].group == "moved by the general rules"
    new = plan.rows[0].after
    assert set(new.data["dates"]) == {"deadline", "start_by" if extra == "start_by" else "not_before"}
    assert new.data["dates"] == own.data["dates"]
    assert not {"due", "start_by", "on_event", "on_return", "resolved_from"}.intersection(new.data["timing"])
    assert parse_record(render_record(new), "general migrated synthetic").data == new.data
    after_index = work.index_things(plan.records)
    for moment in (helpers.NOW, helpers.NOW + timedelta(days=15)):
        assert work.explain(old.id, after_index, moment) == work.explain(old.id, before_index, moment)
        assert work.dates_of(old.id, after_index) == work.dates_of(old.id, before_index)
    if extra != "start_by":
        assert work.status_of(old.id, after_index, helpers.NOW) == "waiting"
        assert "Synthetic approval event" in work.explain(old.id, after_index, helpers.NOW)["reason"]


@pytest.mark.parametrize("timing,field", [
    (dict(start_by="2026-10-05", resolved_from=dict(words="start by 5 October", today="2026-09-30")), "data.timing.start_by"),
    (dict(due="2026-10-10", on_event="Event one", on_return="Event two", resolved_from=dict(words="by 10 October", today="2026-09-30")), "data.timing.on_return"),
])
def test_general_unrepresentable_legacy_dates_keep_precise_reader_refusal(tmp_path, timing, field):
    old = synthetic_old_record(2, timing=timing)
    path = write_synthetic(tmp_path, old)
    before = path.read_bytes()
    assert work.write_index([old])[old.id].as_dict() == old.as_dict()
    with pytest.raises(ValueError, match=field) as failure:
        plan_move_over(tmp_path, rules(), {}, helpers.NOW)
    assert old.id in str(failure.value)
    assert path.read_bytes() == before


def test_mixed_unrepresentable_dates_keep_public_reader_refusal(tmp_path):
    old = synthetic_old_record(4, timing=dict(start_by="2026-10-05", resolved_from=dict(
        words="start by 5 October", today="2026-09-30")))
    # Deliberately invalid mixed input: the frozen parser owns this refusal.
    mixed = replace(old, data={**old.data, "called": "project"})
    path = write_synthetic(tmp_path, mixed)
    before = path.read_bytes()
    with pytest.raises(BrainError) as failure:
        plan_move_over(tmp_path, rules(), {}, helpers.NOW)
    assert failure.value.details["field"] == "data.timing.resolved_from"
    assert failure.value.details["locator"] == str(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("timing,field", [
    (dict(due="2026-10-10", start_by="2026-10-05", resolved_from=dict(words="on 10 October", today="2026-09-30")), "data.timing.start_by"),
    (dict(due="2026-10-10", on_event="Approval", resolved_from=dict(words="not before 10 October", today="2026-09-30")), "data.dates.not_before"),
])
def test_general_due_rekind_refuses_unrepresentable_combination(tmp_path, timing, field):
    old = synthetic_old_record(3, timing=timing)
    path = write_synthetic(tmp_path, old)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=field):
        plan_move_over(tmp_path, rules(), {}, helpers.NOW)
    assert path.read_bytes() == before


def test_twenty_synthetic_stale_nonstructural_links_and_untouched_resolution(tmp_path):
    """20 distinct superseded target cases, separate from W0's missing audit links."""
    targets, sources, stale_links = [], [], []
    for pos in range(20):
        first = synthetic_old_record(100 + pos * 3, level="possible")
        middle = synthetic_old_record(101 + pos * 3, level="possible", supersedes=[first.id])
        current = synthetic_old_record(102 + pos * 3, level="possible", supersedes=[first.id, middle.id])
        for target in (first, middle, current):
            write_synthetic(tmp_path, target)
        link = dict(type=("came_from", "replaces", "handed_to")[pos % 3], target=middle.id)
        source = synthetic_old_record(200 + pos, links=[link], timing=dict(
            due="2026-10-10", resolved_from=dict(words="by 10 October", today="2026-09-30")))
        write_synthetic(tmp_path, source)
        targets.append((first, middle, current))
        sources.append(source)
        stale_links.append(link)
    legacy = synthetic_old_record(300, level="possible", links=stale_links)
    legacy_path = write_synthetic(tmp_path, legacy)
    preserved = {p:p.read_bytes() for p in tmp_path.rglob("*.md") if p.stem not in {r.id for r in sources}}
    assert len({link["target"] for link in stale_links}) == 20
    assert all(link["target"] != first.id for link, (first, _, _) in zip(stale_links, targets))
    plan = plan_move_over(tmp_path, rules(), {}, helpers.NOW)
    assert len(plan.rows) == 20
    own, index = work.write_index(plan.records), work.index_things(plan.records)
    for source, (first, middle, current) in zip(sources, targets):
        moved = own[source.id]
        assert moved.links == (dict(type=source.links[0]["type"], target=first.id),)
        assert index[first.id].id == current.id
        assert work.next_of(middle.id, index, helpers.NOW)["item_ref"] == first.id
        assert work.next_of(middle.id, index, helpers.NOW)["text"] == current.title
        row = next(row for row in plan.rows if row.first_id == source.id)
        assert row.after.id == moved.id and row.reader["next"]["item_ref"] == source.id
        assert row.reader["status"] == work.status_of(moved.id, index, helpers.NOW)
    assert legacy_path.read_bytes() == preserved[legacy_path]
    assert next(r for r in plan.records if r.id == legacy.id).links == tuple(stale_links)
    for link, (first, _, current) in zip(legacy.links, targets):
        assert work.status_of(link["target"], index, helpers.NOW) == work.status_of(current.id, index, helpers.NOW)
        assert work.next_of(link["target"], index, helpers.NOW)["item_ref"] == first.id
    assert all(p.read_bytes() == content for p, content in preserved.items())


def test_audited_rows_history_and_no_invention(tree):
    before = {p: p.read_bytes() for p in tree.rglob("*.md")}
    plan = plan_move_over(tree, rules(), {}, helpers.NOW)
    assert len(plan.rows) == 13
    assert [len(b.records) for b in plan.batches] == [13]
    assert len(plan.archive) == 3
    assert len(plan.borderline) == 4
    assert len(plan.stake_calls) == 5
    assert {p: p.read_bytes() for p in tree.rglob("*.md")} == before
    assert all(not c["answer"] for c in plan.stake_calls)
    assert plan == plan_move_over(tree, rules(), {}, helpers.NOW)
    for batch in plan.batches:
        for record in batch.records:
            assert record.source["recorded_by"] == "system:work-move-over"
            assert "first_step" not in record.data and "importance" not in record.data
            assert not {"due", "at", "window", "resolved_from", "start_by"}.intersection(record.data.get("timing") or {})
            assert not {"part_of", "blocked_by", "waiting_on"}.intersection(link["type"] for link in record.links)
            assert "finish_line" not in record.data
    original_ids = {r.id for r in helpers.load_records()}
    assert original_ids - {a.record_id for a in plan.archive} <= {r.id for r in plan.records}
    metadata = helpers.manifest()
    assert sum("exact came_from target unavailable; omitted" in row["gaps"] for row in metadata["records"].values()) == 14
    # These missing audited links are not fabricated into the reconstructed tree.
    assert all(not any(l["type"] == "came_from" for l in r.links) for r in helpers.load_records())


def test_w5_exact_substantive_equality_gated_on_fixture_correction(tree):
    plan = plan_move_over(tree, rules(), {}, helpers.NOW)
    wanted = work.write_index(expected.moved_over_records())
    actual = work.write_index(plan.records)
    officer = next(r for batch in plan.batches for r in batch.records if r.data.get("parent") == expected.Exhibition and r.data.get("order") == 1)
    aliases = {officer.id: expected.OFFICER}
    for key in actual:
        mapped = aliases.get(key, key)
        assert mapped in wanted
        left, right = actual[key].as_dict(), wanted[mapped].as_dict()
        # W5 explicitly declares generated IDs/clocks and reconstruction source
        # attribution synthetic. Nothing in data/title/state/body is relaxed.
        for value in (left, right):
            for field in ("id", "created_at", "updated_at", "source", "evidence"):
                value.pop(field, None)
            if value["data"].get("stake"):
                value["data"]["stake"]["proposed_by"] = "synthetic fixture proposer"
            if mapped == expected.OFFICER:
                value["data"]["check"] = dict(locator="synthetic fixture check", checked_by="synthetic fixture checker")
        assert left == right, mapped
    assert {aliases.get(key, key) for key in actual} == set(wanted)


@pytest.mark.parametrize("offset", [1, 2, 5])
def test_officer_done_day_comes_from_source_not_move_clock(tree, offset):
    from brain_core.weekly import weekly_review
    at = helpers.NOW + timedelta(days=offset)
    plan = plan_move_over(tree, rules(), {}, at)
    officer = next(r for r in plan.records if r.title == "Text my coordinator")
    index = work.index_things(plan.records)
    assert officer.updated_at >= at
    assert officer.data["dates"]["done"] == dict(words="I basically texted my coordinator",
        read_by="code", read_on="2026-09-30", day="2026-09-30")
    assert work.done_day_of(officer, index).isoformat() == "2026-09-30"
    review = weekly_review(tuple(ItemRecord(record=r, path=r.id) for r in plan.records),
                           helpers.NOW + timedelta(days=2))
    assert "Text my coordinator — done Wed 30 Sep" in [line.text for line in review.lines if line.part == "finished"]


def test_reader_answers_and_complete_histories(tree):
    plan = plan_move_over(tree, rules(), {}, helpers.NOW)
    actual = work.write_index(plan.records)
    index = work.index_things(plan.records)
    for row in plan.rows:
        assert row.reader["status"] == work.status_of(row.first_id, index, helpers.NOW)
        assert row.reader["next"] == work.next_of(row.first_id, index, helpers.NOW)
        assert row.reader["progress"] == work.progress_of(row.first_id, index)
    exhibition = actual[expected.Exhibition]
    assert len(exhibition.supersedes) == 2
    assert work.progress_of(expected.Exhibition, work.index_things(plan.records))["text"] == "1 of 3"
    for row in plan.rows:
        for ref in row.after.supersedes:
            assert work.status_of(ref, work.index_things(plan.records), helpers.NOW) == row.reader["status"]


def test_titles_derive_only_from_cited_exact_phrases(tree):
    plan = plan_move_over(tree, rules(), {}, helpers.NOW)
    actual = work.write_index(plan.records)
    aliases = {ref:first_id(r) for r in helpers.load_records() for ref in (r.id, *r.supersedes)}
    for row in rules()["rows"]:
        if row.get("title"):
            assert row["title_quote"] in row["quoted_message"]
            record = actual[aliases[row["id"]]]
            assert record.title.casefold() == row["title_quote"].casefold()
            assert any(e.locator == row["title_source"] and e.excerpt == row["quoted_message"] for e in record.evidence)
    assert "Actual tick time unknown" in next(r.body for b in plan.batches for r in b.records if r.data.get("order") == 1 and r.data.get("parent") == expected.Exhibition)
    invalid = rules()
    invalid["rows"][1]["title"] = "Invented safety action"
    with pytest.raises(ValueError, match="cited exact phrase"):
        plan_move_over(tree, invalid, {}, helpers.NOW)


def test_general_rules_and_whole_thing_batches(tmp_path):
    helpers.write_fixture(tmp_path, include_made_up=True)
    plan = plan_move_over(tmp_path, rules(), {}, helpers.NOW)
    assert sum(row.group == "moved by the general rules" for row in plan.rows) == 26
    assert len(plan.batches) == 2
    assert all(len(batch.records) <= 25 for batch in plan.batches)
    assert sum(len(batch.records) for batch in plan.batches) == 39
    locations = {first_id(record): pos for pos, batch in enumerate(plan.batches) for record in batch.records}
    for batch in plan.batches:
        for record in batch.records:
            parent = record.data.get("parent")
            if parent in locations:
                assert locations[parent] == locations[first_id(record)]


def test_stakes_yes_only_and_quotes_exact(tree):
    answers = {"stakes": {key: dict(yes=True, words=f"Yes, confirm my stake on {key}.") for key in (expected.Exhibition, expected.VENUE_RENTAL, expected.Printworks, expected.ExhibitCover, expected.EMAIL)}}
    plan = plan_move_over(tree, rules(), answers, helpers.NOW)
    current = work.write_index(plan.records)
    for key, reply in answers["stakes"].items():
        assert current[key].data["stake"]["confirmed"] is True
        assert current[key].source["words"] == reply["words"]
        assert current[key].evidence[-1].excerpt == reply["words"]
    allowed = {"Just a label", "I basically texted my coordinator", *(v["words"] for v in answers["stakes"].values())}
    assert all(r.source["words"] in allowed for b in plan.batches for r in b.records if r.source["said_by"] == "owner")
    with pytest.raises(ValueError, match="verbatim"):
        plan_move_over(tree, rules(), {"stakes": {expected.Exhibition: {"yes": True}}}, helpers.NOW)
    with pytest.raises(ValueError, match="boolean"):
        plan_move_over(tree, rules(), {"stakes": {expected.Exhibition: {"yes": "yes"}}}, helpers.NOW)


@pytest.mark.parametrize("confirm_stake", [False, True])
def test_finish_line_keeps_owner_reply_in_same_version_and_removes_review_question(tree, confirm_stake):
    text = "My Permit is approved — I'm excused."
    words = f"  My finish line: {text}\nThat's my whole answer.  "
    answers = {"finish_lines": {expected.Exhibition: dict(text=f" \n{text}\t", words=words)}}
    stake_words = "Yes, confirm the exhibition stake."
    if confirm_stake:
        answers["stakes"] = {expected.Exhibition: dict(yes=True, words=stake_words)}
    before = plan_move_over(tree, rules(), {}, helpers.NOW)
    plan = plan_move_over(tree, rules(), answers, helpers.NOW)
    row = next(row for row in plan.rows if row.first_id == expected.Exhibition)
    baseline = next(row for row in before.rows if row.first_id == expected.Exhibition)
    assert row.after.id == baseline.after.id and row.after.supersedes == baseline.after.supersedes
    assert [len(b.records) for b in plan.batches] == [len(b.records) for b in before.batches]
    assert row.after.data["finish_line"] == text
    assert row.after.source == dict(said_by="owner", came_by="direct", words=words, recorded_by="system:work-move-over")
    assert row.after.evidence[-1].locator == "answers:finish_lines:" + expected.Exhibition
    assert row.after.evidence[-1].excerpt == words
    assert row.after.evidence[-1].content_sha256 == sha256(words.encode()).hexdigest()
    assert row.after.data["stake"]["confirmed"] is confirm_stake
    if confirm_stake:
        assert any(e.locator == "answers:stakes:" + expected.Exhibition and e.excerpt == stake_words for e in row.after.evidence)
    assert not any("finish line" in q.casefold() for q in row.questions)
    assert not any("finish line" in need["text"].casefold() for need in row.reader["needs"])
    morning = build_morning(MorningInputs(tuple(ItemRecord(r, r.id) for r in plan.records), (), (), (), {}, helpers.NOW))
    assert not any("finish line" in line.text.casefold() for part in morning.parts for line in part.lines if line.item_ref == expected.Exhibition)


@pytest.mark.parametrize("reply", [
    dict(text="Approved", words="approved"),
    dict(text="Not said", words="My actual answer"),
    dict(text=" \n\t", words=" \n\t"),
    dict(text="x" * 257, words="x" * 257),
    dict(text=1, words="1"),
    dict(text="Approved", words=None),
    dict(text="Approved"),
    "Approved",
])
def test_invalid_finish_line_refuses_naming_first_id_without_writes(tree, reply):
    before = {p: p.read_bytes() for p in tree.rglob("*.md")}
    with pytest.raises(ValueError, match=expected.Exhibition):
        plan_move_over(tree, rules(), {"finish_lines": {expected.Exhibition: reply}}, helpers.NOW)
    assert {p: p.read_bytes() for p in tree.rglob("*.md")} == before


@pytest.mark.parametrize("length", [1, 256])
def test_finish_line_accepts_length_boundaries(tree, length):
    text = "x" * length
    plan = plan_move_over(tree, rules(), {"finish_lines": {expected.Exhibition: dict(text=text, words=text)}}, helpers.NOW)
    assert work.write_index(plan.records)[expected.Exhibition].data["finish_line"] == text


def test_finish_line_refuses_unknown_and_nonfirst_ids(tree):
    current = work.write_index(helpers.load_records())[expected.Exhibition]
    assert current.id != expected.Exhibition
    for key in ("brn_unknown", current.id):
        with pytest.raises(ValueError, match=key):
            plan_move_over(tree, rules(), {"finish_lines": {key: dict(text="Approved", words="Approved")}}, helpers.NOW)


def test_finish_line_refuses_known_things_not_being_moved(tree):
    records = helpers.load_records()
    replaced = {ref for r in records for ref in r.supersedes}
    excluded = {first_id(r) for r in records if r.id not in replaced and (r.state in {"done", "dropped", "no_longer_needed"} or r.data.get("repeat") or r.data.get("level") == "possible")}
    aliases = {r.id: first_id(r) for r in records}
    excluded.update(aliases[row["id"]] for row in rules()["rows"] if row.get("unchanged"))
    excluded.update(aliases[ref] for ref in rules()["test_data_ids"])
    for key in excluded:
        with pytest.raises(ValueError, match=key):
            plan_move_over(tree, rules(), {"finish_lines": {key: dict(text="Approved", words="Approved")}}, helpers.NOW)
    borderline = rules()["borderline"][0]["id"]
    key = first_id(next(r for r in records if r.id == borderline))
    with pytest.raises(ValueError, match=key):
        plan_move_over(tree, rules(), {"archive": {borderline: dict(yes=True, words="Archive it")},
                                     "finish_lines": {key: dict(text="Approved", words="Approved")}}, helpers.NOW)
    new_shape = synthetic_old_record(500)
    new_shape = replace(new_shape, data={**new_shape.data, "called": "project"})
    write_synthetic(tree, new_shape)
    with pytest.raises(ValueError, match=new_shape.id):
        plan_move_over(tree, rules(), {"finish_lines": {new_shape.id: dict(text="Approved", words="Approved")}}, helpers.NOW)


def test_no_finish_lines_preserves_existing_plan_and_question(tree):
    plan = plan_move_over(tree, rules(), {}, helpers.NOW)
    assert plan == plan_move_over(tree, rules(), {"finish_lines": {}}, helpers.NOW)
    row = next(row for row in plan.rows if row.first_id == expected.Exhibition)
    assert "finish_line" not in row.after.data
    assert any("finish line" in q.casefold() for q in row.questions)
    assert any("finish line" in need["text"].casefold() for need in row.reader["needs"])


def test_borderline_archives_whole_history_and_preserves_bytes(tree):
    answers = {"archive": {row["id"]: {"yes": True, "words": "Yes archive it"} for row in rules()["borderline"]}}
    plan = plan_move_over(tree, rules(), answers, helpers.NOW)
    assert len(plan.archive) == 10
    archived = {entry.record_id for entry in plan.archive}
    assert not archived.intersection(r.id for r in plan.records)
    assert not any(archived.intersection(r.supersedes) for r in plan.records)


def test_corrupt_record_fails_loudly(tree):
    next(tree.rglob("*.md")).write_text("not a record", encoding="utf-8")
    with pytest.raises(Exception):
        plan_move_over(tree, rules(), {}, helpers.NOW)


def test_move_back_is_pure_and_names_every_newshape(tree):
    over = plan_move_over(tree, rules(), {}, helpers.NOW)
    for batch in over.batches:
        for record in batch.records:
            path = tree / f"records/item/{record.created_at:%Y/%m}/{record.id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_record(record), encoding="utf-8")
    back = plan_move_back(tree)
    # Ending Admin only adds old-shape reason/state; it must remain untouched.
    admin = work.write_index(over.records)[expected.ADMIN]
    assert not WORK_FIELDS.intersection(admin.data)
    assert len(back.archive) == 12
    assert all(not WORK_FIELDS.intersection(r.data) for r in back.records)
    officer = next(r for r in back.records if r.title == "Text my coordinator")
    assert officer.state == "done" and officer.data["check"]
    assert "dates" not in officer.data
    assert parse_record(render_record(officer), "move-back officer").data == officer.data
    assert all(r.source["recorded_by"] == "system:work-move-back" for b in back.batches for r in b.records)
    for batch in back.batches:
        for record in batch.records:
            original = next(r for r in over.records if r.id == record.supersedes[-1])
            assert record.evidence == original.evidence
            assert {k:v for k,v in record.source.items() if k != "recorded_by"} == {k:v for k,v in original.source.items() if k != "recorded_by"}


def test_inverse_links_period_appointment_open_action_and_all_fields(tree):
    seed = next(r for r in helpers.load_records() if r.data.get("level") == "accepted")
    values = []
    for pos, data in enumerate((
        dict(called="project", next_action=dict(text="Open the file", done=False)),
        dict(dates=dict(planned=dict(words="this weekend", read_on="2026-09-30", read_by="code", start="2026-10-03", end="2026-10-04"))),
        dict(dates=dict(appointment=dict(words="at 12pm", read_on="2026-09-30", read_by="code", day="2026-09-30", at="12:00"))),
        dict(needs={"from":"Officer", "what":"approval", "expected":dict(words="by friday", read_on="2026-09-30", read_by="code", day="2026-10-02")}),
    )):
        value = deepcopy(seed.as_dict())
        value.update(id=f"brn_20260930_050000_{0xbb00+pos:016x}", created_at="2026-09-30T05:00:00+00:00", updated_at="2026-09-30T05:00:00+00:00", supersedes=[], links=[], state="open", data=dict(level="accepted", size="task", **data))
        values.append(record_from_mapping(value, "inverse fake"))
    child = deepcopy(values[0].as_dict())
    child.update(id="brn_20260930_050000_000000000000bb04", data=dict(level="accepted", size="task", parent=values[0].id, order=2, after=[values[1].id]))
    values.append(record_from_mapping(child, "inverse linked fake"))
    for r in values:
        path = tree / f"records/item/{r.created_at:%Y/%m}/{r.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_record(r), encoding="utf-8")
    plan = plan_move_back(tree)
    current = {first_id(r):r for b in plan.batches for r in b.records}
    assert current[values[0].id].data["size"] == "goal"
    assert current[values[0].id].data["first_step"] == "Open the file"
    assert current[values[1].id].data["timing"]["window"] == dict(start="2026-10-03", end="2026-10-04")
    assert current[values[2].id].data["timing"]["due"] == "2026-09-30"
    assert current[values[2].id].data["timing"]["at"] == "12:00"
    assert current[values[3].id].state == "waiting"
    assert dict(type="waiting_on", target="Officer") in current[values[3].id].links
    assert current[values[4].id].state == "blocked"
    assert set((l["type"],l["target"]) for l in current[values[4].id].links) == {("part_of",values[0].id),("blocked_by",values[1].id)}
    assert all(not WORK_FIELDS.intersection(r.data) for r in current.values())
    assert any({values[0].id,values[4].id} <= set(t.first_ids) for b in plan.batches for t in b.things)


def test_exhibition_memo_moves_with_a_plan_and_event_wait(tree):
    plan = plan_move_over(tree, rules(), {}, helpers.NOW)
    index = work.index_things(plan.records)
    dates = work.dates_of(expected.MEMO, index)
    assert dates["planned"] == dates["not_before"]
    assert dates["planned"]["day"] == "2026-10-08"
    assert work.explain(expected.MEMO, index, helpers.NOW)["reason"] == "on Thu 8 Oct (venue inspection)"
    assert work.next_of(expected.Exhibition, index, helpers.NOW)["reason"] == "on Thu 8 Oct (venue inspection)"
    morning = build_morning(MorningInputs(tuple(ItemRecord(r, f"records/{r.id}.md") for r in plan.records),
                                         (), (), (), {}, helpers.NOW + timedelta(days=1)))
    row = next(line for line in morning.parts[0].lines if line.item_ref == expected.MEMO)
    assert "on Thu 8 Oct (venue inspection)" in row.text and "not before" not in row.text
    from datetime import datetime, timezone
    on_day = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
    memo = replace(index[expected.MEMO], data=dict(level="accepted", size="task", dates=dates))
    current = build_morning(MorningInputs((ItemRecord(memo, "memo.md"),),
                                         (), (), (), {}, on_day))
    pick = next(line for line in current.parts[3].lines if line.item_ref == expected.MEMO)
    assert pick.fields_used["preselected"] and "already picked" in pick.text


def test_completed_officer_part_keeps_source_day_in_the_frozen_version_fields(tree):
    from datetime import datetime, timezone
    from brain_core.weekly import weekly_review
    moved_at = helpers.NOW + timedelta(days=1)  # Move on 1 Oct, words recorded on 30 Sep.
    plan = plan_move_over(tree, rules(), {}, moved_at)
    officer = next(r for r in plan.records if r.data.get("parent") == expected.Exhibition and r.data.get("order") == 1)
    source = max((r for r in helpers.load_records() if first_id(r) == expected.Exhibition),
                 key=lambda r: r.updated_at)
    assert officer.updated_at == officer.created_at == moved_at
    assert officer.data["dates"]["done"]["day"] == source.updated_at.date().isoformat()
    assert officer.data["dates"]["done"]["day"] == "2026-09-30"
    assert officer.data["check"] == {"locator": rules()["rows"][0]["source"], "checked_by": "owner"}
    assert parse_record(render_record(officer), "unchanged work-01 parser").as_dict() == officer.as_dict()
    review = weekly_review(tuple(ItemRecord(r, f"records/{r.id}.md") for r in plan.records),
                           datetime(2026, 10, 1, 22, tzinfo=timezone.utc))  # Fri 2 Oct, two days later.
    line = next(line for line in review.lines if line.item_ref == officer.id)
    assert line.text == "Text my coordinator — done Wed 30 Sep"
    assert line.fields_used["done_at"] == moved_at.astimezone(timezone.utc).isoformat()
    assert line.fields_used["done_day"] == "2026-09-30"
