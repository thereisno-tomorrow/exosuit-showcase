"""Which Brain Home items the Notion view shows, and as what (docs/product/steps/h-notion-view.md §2, §3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from brain_core.records import RECORD_SCHEMA_V2, Record

from gremlin_town.notion_view.mapping import (
    PROPERTY_TYPES,
    picked_today_refs,
    plan_rows,
    properties,
    schema_problems,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def item(
    record_id: str,
    *,
    title: str = "a task",
    state: str = "open",
    level: str = "accepted",
    sensitivity: str = "private",
    supersedes: tuple[str, ...] = (),
    updated_at: datetime = NOW,
    data: dict[str, Any] | None = None,
    links: tuple[dict[str, Any], ...] = (),
    kind: str = "item",
    schema: str = RECORD_SCHEMA_V2,
) -> Record:
    return Record(
        schema=schema, id=record_id, kind=kind, title=title, state=state, created_at=updated_at,
        updated_at=updated_at, assertion=None, confidence=None, sensitivity=sensitivity, vaults=(), owners=(),
        entities=(), evidence=(), supersedes=supersedes, dedupe_key=None,
        data={"level": level, "size": "task", "timing": {}, **(data or {})}, links=links,
    )


def ids(rows) -> list[str]:
    return [row.root_id for row in rows]


def test_only_public_and_private_items_are_shown():
    rows = plan_rows(
        [
            item("brn_20260901_000000_aaaa", sensitivity="public"),
            item("brn_20260901_000000_bbbb", sensitivity="private"),
            item("brn_20260901_000000_cccc", sensitivity="restricted"),
            item("brn_20260901_000000_dddd", sensitivity="local-only"),
        ],
        now=NOW, done_days=14,
    )
    assert ids(rows) == ["brn_20260901_000000_aaaa", "brn_20260901_000000_bbbb"]


def test_finished_items_stay_for_done_days_only():
    rows = plan_rows(
        [
            item("brn_20260901_000000_aaaa", state="done", updated_at=NOW - timedelta(days=13)),
            item("brn_20260901_000000_bbbb", state="dropped", updated_at=NOW - timedelta(days=15)),
            item("brn_20260901_000000_cccc", state="parked", updated_at=NOW - timedelta(days=90)),
        ],
        now=NOW, done_days=14,
    )
    assert ids(rows) == ["brn_20260901_000000_aaaa", "brn_20260901_000000_cccc"]


def test_other_kinds_and_v1_records_are_left_out():
    rows = plan_rows(
        [item("brn_20260901_000000_aaaa", kind="fact", state="current"), item("brn_20260901_000000_bbbb", schema="brain.record/v1")],
        now=NOW, done_days=14,
    )
    assert rows == ()


def test_a_row_stays_keyed_to_the_oldest_version():
    first = item("brn_20260901_000000_aaaa", title="v1")
    second = item("brn_20260902_000000_bbbb", title="v2", supersedes=(first.id,))
    third = item("brn_20260903_000000_cccc", title="v3", supersedes=(second.id,))
    (row,) = plan_rows([first, second, third], now=NOW, done_days=14)
    assert row.root_id == first.id
    assert row.record.title == "v3"


def test_the_root_holds_even_when_older_versions_are_missing():
    # Only the chain's middle and newest version are in the snapshot; the newest names the middle.
    middle = item("brn_20260902_000000_bbbb", supersedes=("brn_20260901_000000_aaaa",))
    newest = item("brn_20260903_000000_cccc", supersedes=(middle.id,))
    (row,) = plan_rows([middle, newest], now=NOW, done_days=14)
    assert row.root_id == "brn_20260901_000000_aaaa"


def test_a_merged_item_leaves_one_row():
    one = item("brn_20260901_000000_aaaa")
    two = item("brn_20260902_000000_bbbb")
    merged = item("brn_20260903_000000_cccc", supersedes=(one.id, two.id))
    assert ids(plan_rows([one, two, merged], now=NOW, done_days=14)) == [one.id]


def test_picked_today_matches_any_version():
    first = item("brn_20260901_000000_aaaa")
    second = item("brn_20260902_000000_bbbb", supersedes=(first.id,))
    (row,) = plan_rows([first, second], now=NOW, done_days=14, picked=frozenset({first.id}))
    assert row.picked_today


def test_part_of_points_at_the_goals_root():
    goal_v1 = item("brn_20260901_000000_aaaa", data={"size": "goal"})
    goal_v2 = item("brn_20260902_000000_bbbb", data={"size": "goal"}, supersedes=(goal_v1.id,))
    task = item("brn_20260903_000000_cccc", links=({"type": "part_of", "target": goal_v2.id},))
    rows = {row.root_id: row for row in plan_rows([goal_v1, goal_v2, task], now=NOW, done_days=14)}
    assert rows[task.id].part_of == (goal_v1.id,)
    values = properties(rows[task.id], page_for={goal_v1.id: "page-goal"})
    assert values["Part of"] == {"relation": [{"id": "page-goal"}]}
    # A goal that isn't shown is simply left out.
    assert properties(rows[task.id], page_for={})["Part of"] == {"relation": []}


def test_every_property_is_mapped():
    record = item(
        "brn_20260901_000000_aaaa", title="Call the accountant", level="suggested", state="parked",
        data={
            "block": "admin", "first_step": "Find the number", "owed_to": "Mum",
            "importance": {"level": "high", "reason": "fine", "proposed_by": "ciel", "confirmed": False},
            "parked_until": {"event": "the flat is sold"},
            "timing": {"due": "2026-10-03", "start_by": "2026-10-01", "window": {"start": "2026-10-01", "end": "2026-10-05"},
                       "duration_minutes": 20, "estimated_by": "ciel"},
        },
    )
    (row,) = plan_rows([record], now=NOW, done_days=14)
    values = properties(row, page_for={})
    assert set(values) == set(PROPERTY_TYPES)
    assert values["Name"]["title"][0]["text"]["content"] == "Call the accountant"
    assert values["Status"] == {"select": {"name": "Parked"}}
    assert values["Level"] == {"select": {"name": "Suggested"}}
    assert values["Size"] == {"select": {"name": "Task"}}
    assert values["Block"] == {"select": {"name": "Admin"}}
    assert values["Stake"] == {"select": {"name": "Can't undo?"}}
    assert values["First step"]["rich_text"][0]["text"]["content"] == "Find the number"
    assert values["Due"] == {"date": {"start": "2026-10-03"}}
    assert values["Start by"] == {"date": {"start": "2026-10-01"}}
    assert values["Window"] == {"date": {"start": "2026-10-01", "end": "2026-10-05"}}
    assert values["Minutes"] == {"number": 20}
    assert values["Owed to"]["rich_text"][0]["text"]["content"] == "Mum"
    assert values["Parked until"]["rich_text"][0]["text"]["content"] == "after: the flat is sold"
    assert values["Picked today"] == {"checkbox": False}
    assert values["Brain ID"]["rich_text"][0]["text"]["content"] == record.id
    assert values["Brain version"]["rich_text"][0]["text"]["content"] == record.id
    assert values["Changed"] == {"date": {"start": NOW.isoformat()}}


def test_empty_fields_clear_their_property():
    (row,) = plan_rows([item("brn_20260901_000000_aaaa")], now=NOW, done_days=14)
    values = properties(row, page_for={})
    assert values["Block"] == {"select": None}
    assert values["Stake"] == {"select": None}
    assert values["First step"] == {"rich_text": []}
    assert values["Due"] == {"date": None}
    assert values["Minutes"] == {"number": None}


def test_stakes():
    def stake(importance):
        (row,) = plan_rows([item("brn_20260901_000000_aaaa", data={"importance": importance})], now=NOW, done_days=14)
        return properties(row, page_for={})["Stake"]["select"]["name"]

    assert stake({"level": "high", "reason": "r", "proposed_by": "nic", "confirmed": True}) == "Can't undo"
    assert stake({"level": "real_cost", "reason": "r", "proposed_by": "ciel", "confirmed": False}) == "Real cost"
    assert stake({"level": "minor", "reason": "r", "proposed_by": "ciel", "confirmed": False}) == "Minor"


def test_long_text_is_cut_to_notions_limit():
    (row,) = plan_rows([item("brn_20260901_000000_aaaa", title="x" * 3000)], now=NOW, done_days=14)
    assert len(properties(row, page_for={})["Name"]["title"][0]["text"]["content"]) == 2000


@dataclass
class _History:
    item_ref: str
    response: str | None
    surfaced_at: datetime
    responded_at: datetime | None = None


def test_picks_count_only_for_today_in_singapore():
    today = NOW.date()
    history = [
        _History("a", "picked_today", NOW),
        _History("b", "picked_today", NOW - timedelta(days=1)),
        _History("c", "not_now", NOW),
        # 23:30 UTC on the 28th is 07:30 on the 29th in Singapore.
        _History("d", "picked_today", NOW - timedelta(days=2), responded_at=datetime(2026, 9, 28, 23, 30, tzinfo=UTC)),
    ]
    assert picked_today_refs(history, today) == frozenset({"a", "d"})


def test_schema_problems_name_what_is_missing_or_wrong():
    found = {name: {"type": kind} for name, kind in PROPERTY_TYPES.items()}
    assert schema_problems(found) == ()
    found.pop("Block")
    found["Due"] = {"type": "rich_text"}
    assert schema_problems(found) == ("missing 'Block' (select)", "'Due' is rich_text, needs date")


def test_rows_are_stable_whatever_the_input_order():
    records = [item(f"brn_20260901_00000{n}_aaaa") for n in range(5)]
    assert ids(plan_rows(records, now=NOW, done_days=14)) == ids(plan_rows(list(reversed(records)), now=NOW, done_days=14))
