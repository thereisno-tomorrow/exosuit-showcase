"""Ranking by points (Ciel and Brain Home PRD §5; build plan slice 2): each factor's weight, the
due-date curve at its edges, blocking passed down a chain, ties, the PRD's eleven worked examples
(the urgency test's Part 2, P1-P11) and acceptance cases 6 to 10."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from brain_core.orientation import (
    AGE_FULL_AFTER_DAYS,
    END_OF_DAY,
    INHERIT_MARGIN,
    POINTS,
    OrientationInputs,
    due_fraction,
    due_moment,
    first_saved,
    rank,
    what_next,
)
from brain_core.records import RECORD_SCHEMA_V2, Evidence, Record
from brain_core.review import RANKING_POINTS, RANKING_RULES, ItemRecord, OpenNote, ReviewInputs
from brain_core.timewords import SINGAPORE
from brain_core.work import first_id

# Mon 5 Oct 2026, 08:00 in Singapore: the moment the PRD's table was worked out for.
NOW = datetime(2026, 10, 5, 8, 0, tzinfo=SINGAPORE)
TODAY = date(2026, 10, 5)
EVIDENCE = (
    Evidence(source_type="session_message", locator="session:test:1", content_sha256="a" * 64, excerpt="fixture"),
)


def item(
    id_suffix: str,
    *,
    title: str = "a task",
    state: str = "open",
    due: str | None = None,
    at: str | None = None,
    window: tuple[str, str] | None = None,
    start_by: str | None = None,
    padded_minutes: int | None = None,
    owed_to: str | None = None,
    repeat: str | None = None,
    stake: tuple[str, str] | None = None,
    confirmed: bool = False,
    links: tuple[dict[str, str], ...] = (),
    saved: datetime = NOW,
    size: str = "task",
    supersedes: tuple[str, ...] = (),
) -> Record:
    """``stake`` is (level, reason): level ``high`` (can't undo), ``real_cost`` or ``minor``."""

    timing: dict[str, object] = {}
    if due is not None:
        timing["due"] = due
    if at is not None:
        timing["at"] = at
    if window is not None:
        timing["window"] = {"start": window[0], "end": window[1]}
    if start_by is not None:
        timing["start_by"] = start_by
    if padded_minutes is not None:
        timing["duration_minutes"] = padded_minutes * 2 // 3
        timing["padded_minutes"] = padded_minutes
        timing["estimated_by"] = "chatgpt-web"
    data: dict[str, object] = {"level": "accepted", "size": size, "timing": timing}
    if owed_to is not None:
        data["owed_to"] = owed_to
    if repeat is not None:
        data["repeat"] = repeat
    if stake is not None:
        data["stake"] = {"level": {"high": "changes_life", "real_cost": "hurts_weeks", "minor": "nuisance"}.get(stake[0], stake[0]), "at_stake": stake[1], "proposed_by": "chatgpt-web", "confirmed": confirmed}
    return Record(
        schema=RECORD_SCHEMA_V2,
        id=f"brn_20261005_000000_{id_suffix}",
        kind="item",
        title=title,
        state=state,
        created_at=saved,
        updated_at=saved,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=EVIDENCE,
        supersedes=supersedes,
        dedupe_key=None,
        data=data,
        body="",
        source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"},
        links=links,
    )


def wrap(*records: Record) -> tuple[ItemRecord, ...]:
    return tuple(ItemRecord(record=record, path=f"records/item/2026/10/{record.id}.md") for record in records)


def ranked_titles(*records: Record, now: datetime = NOW, **kwargs: object) -> list[str]:
    today = now.astimezone(SINGAPORE).date()
    return [candidate.title for candidate in rank(wrap(*records), today, now=now, **kwargs)]


def points_of(record: Record, *others: Record, now: datetime = NOW, **kwargs: object) -> dict[str, float]:
    today = now.astimezone(SINGAPORE).date()
    ranked = rank(wrap(record, *others), today, now=now, **kwargs)
    return next(candidate for candidate in ranked if candidate.ref == first_id(record)).fields_used["points"]


def at(day: int, hour: int = 9, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=SINGAPORE)


# --- The weights table ---------------------------------------------------------------------------


def test_the_weights_are_the_prds_table():
    assert dict(POINTS) == {
        "picked": 8.0, "due": 12.0, "blocking": 8.0, "waiting": 8.0, "hurts_weeks": 8.0, "start_by": 5.0, "focus": 5.0, "started": 4.0, "age": 2.0, "goal": 1.0,
    }
    with pytest.raises(TypeError):
        POINTS["due"] = 1.0  # one table, changed only by editing its line


# --- The due-date curve (Taskwarrior's urgency_due) ---------------------------------------------


def test_the_due_curve_at_its_edges():
    due = datetime(2026, 10, 20, 17, 0, tzinfo=SINGAPORE)
    assert due_fraction(due, due - timedelta(days=30)) == pytest.approx(0.2)
    assert due_fraction(due, due - timedelta(days=14)) == pytest.approx(0.2)
    assert due_fraction(due, due - timedelta(days=7)) == pytest.approx(0.2 + 7 * 0.8 / 21)
    assert due_fraction(due, due) == pytest.approx(0.2 + 14 * 0.8 / 21)
    assert due_fraction(due, due + timedelta(days=7)) == pytest.approx(1.0)
    assert due_fraction(due, due + timedelta(days=40)) == pytest.approx(1.0)


def test_the_due_curve_counts_to_the_minute_and_ignores_seconds():
    due = datetime(2026, 10, 20, 17, 0, tzinfo=SINGAPORE)
    one_minute_on = due_fraction(due, due + timedelta(minutes=1)) - due_fraction(due, due)
    assert one_minute_on == pytest.approx(0.8 / 21 / (24 * 60))
    assert due_fraction(due, due + timedelta(seconds=59)) == due_fraction(due, due)


def test_a_date_without_a_time_is_due_at_the_end_of_that_day():
    assert due_moment(item("a1", due="2026-10-06"), TODAY) == (
        datetime.combine(date(2026, 10, 6), END_OF_DAY, tzinfo=SINGAPORE), "due",
    )
    assert due_moment(item("a2", due="2026-10-06", at="09:30"), TODAY)[0] == datetime(2026, 10, 6, 9, 30, tzinfo=SINGAPORE)


def test_a_windows_last_day_and_a_routines_next_day_count_as_due():
    """Proposed - Nic to confirm (PRD §5)."""

    window = item("a1", window=("2026-10-01", "2026-10-09"))
    assert due_moment(window, TODAY) == (datetime(2026, 10, 9, 23, 59, tzinfo=SINGAPORE), "due")
    monday = item("a2", repeat="weekly:mon")
    thursday = item("a3", repeat="weekly:thu")
    assert due_moment(monday, TODAY) == (datetime(2026, 10, 5, 23, 59, tzinfo=SINGAPORE), "routine")
    assert due_moment(thursday, TODAY) == (datetime(2026, 10, 8, 23, 59, tzinfo=SINGAPORE), "routine")
    assert due_moment(item("a4", repeat="daily"), TODAY)[1] == "routine"
    assert due_moment(item("a5"), TODAY) is None
    assert rank(wrap(monday), TODAY, now=NOW)[0].rules == ("every Monday, due today",)
    assert rank(wrap(thursday), TODAY, now=NOW)[0].rules == ("every Thursday; next this Thursday, 8 Oct",)


# --- Each factor ---------------------------------------------------------------------------------


def test_each_factor_adds_its_weight():
    plain = item("p0")
    assert points_of(plain) == {"total": 0.0}
    assert points_of(plain, picked_today=frozenset({plain.id}))["picked"] == 8.0
    assert points_of(item("p1", owed_to="Sam"))["waiting"] == 8.0
    assert points_of(item("p2", stake=("high", "we lose the flat"), confirmed=True))["stake"] == 0.0
    assert points_of(item("p3", stake=("real_cost", "a late fee"), confirmed=True))["stake"] == 8.0
    assert points_of(item("p4", stake=("minor", "a messy desk"), confirmed=True))["stake"] == 0.0
    assert points_of(item("p5", start_by="2026-10-05"))["start_by"] == 5.0
    focus = item("p6", links=({"type": "about", "target": "vault_exosuit"},))
    assert points_of(focus, focus_projects=frozenset({"vault_exosuit"}))["focus"] == 5.0
    started = item("p7")
    note = OpenNote(session_id="deck", next_step="open it", updated_at=NOW, task_id=started.id)
    assert points_of(replace(started, data={**started.data, "started_at": NOW.isoformat()}), notes=(note,))["started"] == 4.0
    goal = item("g1", title="move into robotics", size="goal", state="waiting")
    assert points_of(item("p8", links=({"type": "part_of", "target": goal.id},)), goal)["goal"] == 1.0
    blocker = item("p9")
    waits = item("pa", state="blocked", links=({"type": "blocked_by", "target": blocker.id},))
    assert points_of(blocker, waits)["blocking"] == 8.0


def test_a_start_by_date_counts_only_once_it_has_come():
    assert "start_by" not in points_of(item("a1", start_by="2026-10-06"))
    (only,) = rank(wrap(item("a2", start_by="2026-10-02")), TODAY, now=NOW)
    assert only.rules == ("start by Fri 2 Oct, now passed",)


def test_age_counts_whole_days_up_to_a_year_from_the_first_version():
    assert points_of(item("a1", saved=NOW - timedelta(days=73, hours=5)))["age"] == round(2.0 * 73 / 365, 2)
    assert points_of(item("a2", saved=NOW - timedelta(days=900)))["age"] == 2.0
    assert "age" not in points_of(item("a3", saved=NOW - timedelta(hours=23)))
    # A change saves a new version; age still runs from the first one, which its ID carries.
    changed = item("a4", saved=NOW - timedelta(hours=1), supersedes=("brn_20260706_010000_abcd",))
    assert first_saved(changed) == datetime(2026, 7, 6, 1, 0, tzinfo=UTC)
    assert points_of(changed)["age"] == round(2.0 * 90 / AGE_FULL_AFTER_DAYS, 2)  # 90 days and 23 hours


def test_how_long_a_task_takes_adds_no_points():
    """"No, Taskwarrior's way" (Nic, 28 Sep): the two-minute rule catches tiny tasks instead."""

    short = item("a1", padded_minutes=3, saved=NOW - timedelta(days=2))
    long = item("a2", padded_minutes=270, saved=NOW - timedelta(days=2))
    assert points_of(short) == points_of(long)


def test_reasons_are_the_biggest_contributors_first_with_the_full_breakdown():
    promise = item("a1", title="Send the tenancy agreement", due="2026-10-05", at="17:00", owed_to="the landlord",
                   stake=("high", "we lose the flat"), confirmed=True, saved=at(1))
    (only,) = rank(wrap(promise), TODAY, now=NOW)
    assert only.rules == (
        "we lose the flat", "due today at 5 pm", "the landlord is waiting",
    )
    breakdown = only.fields_used["points"]
    assert list(breakdown) == ["total", "due", "waiting", "age", "stake"]
    assert breakdown["total"] == pytest.approx(sum(value for name, value in breakdown.items() if name != "total"), abs=0.02)
    assert only.points == breakdown["total"]
    assert only.fields_used["stake"]["level"] == "changes_life"


def test_a_task_with_nothing_to_rank_it_by_says_so():
    (only,) = rank(wrap(item("a1", saved=at(1))), TODAY, now=NOW)
    assert only.rules == ("no date or stake yet; saved Thu 1 Oct",)


# --- Blocking and inheritance (acceptance case 8) -----------------------------------------------


def test_a_blocker_never_ranks_below_what_waits_on_it_all_the_way_down():
    """Acceptance case 8: buying boxes, which blocks clearing the storeroom, never ranks below
    the storeroom; and what blocks the boxes never ranks below the boxes."""

    tape = item("t1", title="Buy tape", saved=NOW)
    boxes = item("b1", title="Buy boxes", links=({"type": "blocked_by", "target": tape.id},), saved=NOW)
    storeroom = item("s1", title="Clear the storeroom", state="blocked", due="2026-10-05", owed_to="the landlord",
                     stake=("high", "we lose the deposit"), confirmed=True,
                     links=({"type": "blocked_by", "target": boxes.id},), saved=at(1))
    ranked = rank(wrap(storeroom, boxes, tape), TODAY, now=NOW)
    # W7: only the up tape competes; blocked descendants still pass urgency up the chain.
    assert [candidate.title for candidate in ranked] == ["Buy tape"]
    inherited = ranked[0].fields_used
    assert inherited["inherited_from"] == "Buy boxes"
    assert inherited["points"]["inherited"] > 0
    storeroom_due = 12 * due_fraction(datetime.combine(TODAY, END_OF_DAY, tzinfo=SINGAPORE), NOW)
    assert inherited["points"]["total"] >= storeroom_due + 8 + 2 * INHERIT_MARGIN


def test_a_blocker_that_already_scores_higher_keeps_its_own_points():
    blocker = item("a1", title="Pay the deposit", due="2026-10-05", stake=("high", "we lose the flat"), confirmed=True)
    waits = item("a2", title="Move in", state="blocked", links=({"type": "blocked_by", "target": blocker.id},))
    (top,) = rank(wrap(blocker, waits), TODAY, now=NOW)
    assert top.title == "Pay the deposit"
    assert "inherited_from" not in top.fields_used and "inherited" not in top.fields_used["points"]


def test_a_loop_of_tasks_waiting_on_each_other_still_ranks_the_same_every_time():
    first = item("a1", title="first", due="2026-10-06")
    second = item("a2", title="second", links=({"type": "blocked_by", "target": first.id},))
    first = item("a1", title="first", due="2026-10-06", links=({"type": "blocked_by", "target": second.id},))
    # W5 refuses relationship cycles rather than guessing an actionable candidate.
    for records in ((first, second), (second, first)):
        with pytest.raises(ValueError, match="cycle"):
            ranked_titles(*records)


# --- Ties and determinism (acceptance case 10) --------------------------------------------------


def test_ties_go_to_the_task_longest_without_a_change_then_the_record_id():
    older = item("b2", title="older", saved=at(4, hour=7))
    newer = item("a1", title="newer", saved=at(4, hour=8))
    same = item("b1", title="same moment as older", saved=at(4, hour=7))
    assert ranked_titles(newer, same, older) == ["same moment as older", "older", "newer"]


def test_the_same_records_at_the_same_moment_always_give_the_same_order():
    """Acceptance case 10: Claude, ChatGPT and Diablo all call this code with the same records."""

    records = [
        item(f"x{index:02d}", title=f"task {index}", due=f"2026-10-{5 + index % 9:02d}" if index % 3 else None,
             owed_to="Sam" if index % 4 == 0 else None, stake=(("minor", "little"), ("real_cost", "a fee"))[index % 2],
             saved=NOW - timedelta(days=index))
        for index in range(20)
    ]
    expected = ranked_titles(*records)
    for seed in range(5):
        shuffled = records[:]
        random.Random(seed).shuffle(shuffled)
        assert ranked_titles(*shuffled) == expected


# --- The PRD's eleven examples (section 5), with both columns -----------------------------------

EXAMPLES = {
    "P1 a small late task, or a big one due tomorrow": (
        [
            item("p1a", title="Submit the visa form", due="2026-10-06", stake=("high", "no visa, no trip"), confirmed=True, saved=at(1)),
            item("p1b", title="Return the library book", due="2026-10-04", stake=("minor", "a small fine"), saved=at(20, month=9)),
        ],
        {}, "Return the library book", "Submit the visa form",
    ),
    "P2 someone waiting, due in 2 days": (
        [
            item("p2a", title="Reply to the landlord", due="2026-10-07", owed_to="the landlord", stake=("real_cost", "the renewal lapses"), saved=at(2)),
            item("p2b", title="Wipe down the kitchen", due="2026-10-05", stake=("minor", "a messy kitchen"), saved=at(4)),
        ],
        {}, "Wipe down the kitchen", "Reply to the landlord",
    ),
    "P3 a boring promise, or a fun focus-project task": (
        [
            item("p3a", title="Send Sam the trip photos", due="2026-10-10", owed_to="Sam", stake=("minor", "Sam waits longer"), saved=at(1)),
            item("p3b", title="Play with the robot arm kit", links=({"type": "about", "target": "robotics"},), stake=("minor", "nothing"), saved=at(28, month=9)),
        ],
        {"focus_projects": frozenset({"robotics"})}, "Play with the robot arm kit", "Send Sam the trip photos",
    ),
    "P4 two tasks due today, at 9 am and at 5 pm": (
        [
            item("p4a", title="Post the parcel", due="2026-10-05", at="17:00", stake=("minor", "it arrives later"), saved=at(1)),
            item("p4b", title="Hand in the safety form", due="2026-10-05", at="09:00", stake=("minor", "a later slot"), saved=at(1)),
        ],
        {}, "Post the parcel", "Hand in the safety form",
    ),
    "P5 1 day late, or 10 days late": (
        [
            item("p5a", title="Reply to the plumber", due="2026-10-04", stake=("minor", "a later visit"), saved=at(1)),
            item("p5b", title="Pay the phone bill", due="2026-09-25", stake=("minor", "a reminder letter"), saved=at(1)),
        ],
        {}, "Reply to the plumber", "Pay the phone bill",
    ),
    "P6 a window closing today, or your own pick": (
        [
            item("p6a", title="Reorganise the bookshelf", stake=("minor", "nothing"), saved=at(30, month=9)),
            item("p6b", title="Use the vouchers", window=("2026-10-01", "2026-10-05"), stake=("real_cost", "S$50 of vouchers lapse"), saved=at(30, month=9)),
        ],
        {"picked_today": frozenset({"brn_20261005_000000_p6a"})}, "Use the vouchers", "Use the vouchers",
    ),
    "P7 six overdue chores, or losing the flat": (
        [
            *(
                item(f"p7{index}", title=f"Overdue chore {index}", due=f"2026-10-0{index % 3 + 2}", stake=("minor", "a mess"), saved=at(25, month=9))
                for index in range(6)
            ),
            item("p7f", title="Send the signed tenancy agreement", due="2026-10-05", at="17:00", owed_to="the landlord",
                 stake=("high", "we lose the flat"), confirmed=True, saved=at(1)),
        ],
        {}, "Send the signed tenancy agreement", "Send the signed tenancy agreement",
    ),
    "P8 importance you haven't confirmed": (
        [
            item("p8a", title="Tidy the photos folder", stake=("minor", "a messy folder"), saved=at(20, month=9)),
            item("p8b", title="Cancel the gym trial", stake=("high", "S$120 if not cancelled"), confirmed=False, saved=at(2)),
        ],
        {}, "Tidy the photos folder", "Tidy the photos folder",
    ),
    "P9 a 3-hour chore, or a 10-minute claim, both due today": (
        [
            item("p9a", title="Clear out the storeroom", due="2026-10-05", padded_minutes=270, stake=("minor", "clutter"), saved=at(30, month=9)),
            item("p9b", title="Submit the S$400 claim", due="2026-10-05", padded_minutes=15, stake=("real_cost", "S$400 unclaimed"), saved=at(3)),
        ],
        {}, "Clear out the storeroom", "Clear out the storeroom",
    ),
    "P10 the kitchen due today, or the job you want, no date": (
        [
            item("paa", title="Wipe down the kitchen", due="2026-10-05", stake=("minor", "a messy kitchen"), saved=at(4)),
            item("pab", title="Apply for the robotics role", stake=("high", "the role closes to me"), confirmed=True, saved=at(2)),
        ],
        {}, "Wipe down the kitchen", "Apply for the robotics role",
    ),
    "P11 a 2-hour job, or a 2-minute reply, neither dated": (
        [
            item("pba", title="Reply to Jo's text", padded_minutes=3, stake=("minor", "Jo waits"), saved=at(2)),
            item("pbb", title="Sort the photo backups", padded_minutes=180, stake=("minor", "nothing"), saved=at(29, month=9)),
        ],
        {}, "Sort the photo backups", "Sort the photo backups",
    ),
}


# Recomputed W7 totals in ranked order. Pins can outrank higher numeric totals.
EXAMPLE_TOTALS = {
    "P1": (8.05, 9.03),
    "P2": (15.59, 8.50),
    "P3": (14.23, 5.03),
    "P4": (8.80, 8.65),
    "P5": (12.02, 8.97),
    "P6": (8.52, 8.02),
    "P7": (16.65, 9.92, 9.92, 9.46, 9.46, 9.00, 9.00),
    "P8": (0.08, 0.01),
    "P9": (8.52, 8.50),
    "P10": (0.01, 8.50),
    "P11": (0.03, 0.01),
}


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_the_prds_eleven_examples(name: str):
    """PRD §5's table: "Today's top pick" under the nine rules, "With points" now."""

    records, kwargs, by_rules, by_points = EXAMPLES[name]
    assert ranked_titles(*records, ranking=RANKING_RULES, **kwargs)[0] == by_rules
    ranked = rank(wrap(*records), TODAY, now=NOW, **kwargs)
    assert ranked[0].title == by_points
    assert tuple(candidate.points for candidate in ranked) == EXAMPLE_TOTALS[name.split()[0]]


def test_graded_dates_put_9_am_above_5_pm_the_same_day():
    """Acceptance case 6, with everything else equal."""

    nine = item("a2", title="9 am", due="2026-10-05", at="09:00", saved=at(1))
    five = item("a1", title="5 pm", due="2026-10-05", at="17:00", saved=at(1))
    assert ranked_titles(five, nine) == ["9 am", "5 pm"]


def test_a_start_by_date_lifts_a_long_task_from_that_day():
    """Acceptance case 7: a 6-hour task due Friday with a start-by date of Wednesday gains points
    from Wednesday."""

    report = item("a1", title="Write the report", due="2026-10-09", start_by="2026-10-07", padded_minutes=540, saved=at(1))
    tuesday, wednesday = datetime(2026, 10, 6, 9, 0, tzinfo=SINGAPORE), datetime(2026, 10, 7, 9, 0, tzinfo=SINGAPORE)
    assert "start_by" not in points_of(report, now=tuesday)
    assert points_of(report, now=wednesday)["start_by"] == 5.0
    (only,) = rank(wrap(report), date(2026, 10, 7), now=wednesday)
    assert only.rules[-1] == "start by today"


def test_proposed_stakes_change_no_points_until_confirmed():
    for level in ("changes_life", "hurts_weeks", "nuisance", "nothing_much"):
        guess = item("a1", stake=(level, "a cost"))
        assert "stake" not in points_of(guess)
        kept = replace(guess, data={**guess.data, "stake": {**guess.data["stake"], "confirmed": True}})
        assert points_of(kept)["stake"] == (8.0 if level == "hurts_weeks" else 0.0)


def test_hurts_weeks_is_within_half_a_point_of_due_in_36_hours():
    due = NOW + timedelta(hours=36)
    assert abs(POINTS["hurts_weeks"] - POINTS["due"] * due_fraction(due, NOW)) <= 0.5


def test_a_life_change_is_pinned_above_every_points_total_only_when_up():
    venue_rental = item("pin", title="Venue rental", stake=("changes_life", "about 40 display slots a month"), confirmed=True)
    urgent = item("urgent", title="Urgent", due="2026-09-01", owed_to="Sam", start_by="2026-09-01", saved=NOW-timedelta(days=400))
    assert ranked_titles(urgent, venue_rental)[0] == "Venue rental"
    (pick, *_) = rank(wrap(urgent, venue_rental), TODAY, now=NOW)
    assert pick.rules[0] == "about 40 display slots a month"
    waiting = replace(venue_rental, data={**venue_rental.data, "needs": {"from": "bank", "what": "answer"}})
    assert ranked_titles(urgent, waiting) == ["Urgent"]


def test_plain_yes_parts_inherit_the_pin_but_review_marked_parts_do_not():
    parent = item("parent", title="Venue rental", stake=("changes_life", "about 40 display slots a month"), confirmed=True)
    parts = [replace(item(f"part{i}", title=f"Part {i}"), data={"level": "accepted", "size": "task", "parent": parent.id, "order": i}) for i in range(1, 4)]
    urgent = item("urgent", title="Urgent", due="2026-09-01")
    assert ranked_titles(parent, urgent, *parts)[:3] == ["Part 1", "Part 2", "Part 3"]
    marked = replace(parts[0], data={**parts[0].data, "review_marks": [{"mark": "agents_wording", "part": "title"}]})
    assert ranked_titles(parent, urgent, marked, *parts[1:])[-1] == "Part 1"


def work_date(day, **extra):
    return {"words": day, "read_by": "code", "read_on": TODAY.isoformat(), "day": day, **extra}


def test_planned_day_is_picked_on_its_day_but_never_due():
    planned = replace(item("planned"), data={"level": "accepted", "size": "task", "dates": {"planned": work_date(TODAY.isoformat())}})
    assert points_of(planned) == {"total": 8.0, "picked": 8.0}
    assert "picked" not in points_of(planned, now=NOW+timedelta(days=1))
    assert due_moment(planned, TODAY) is None


def test_proposed_start_by_earns_nothing_until_kept():
    dates = {"deadline": work_date("2026-10-09"), "start_by": work_date(TODAY.isoformat(), confirmed=False)}
    proposed = replace(item("start"), data={"level": "accepted", "size": "task", "dates": dates})
    assert "start_by" not in points_of(proposed)
    kept = replace(proposed, data={**proposed.data, "dates": {**dates, "start_by": {**dates["start_by"], "confirmed": True}}})
    assert points_of(kept)["start_by"] == 5.0


# --- Which ranking -------------------------------------------------------------------------------


def test_rank_refuses_a_ranking_it_does_not_know():
    with pytest.raises(ValueError, match="ranking must be one of"):
        rank((), TODAY, ranking="urgency")


def test_the_way_back_ranks_by_the_nine_rules_in_both_asks():
    """Accepted - Nic, 28 Sep 2026 ("Yes, keep a way back"): `[orientation] ranking = "rules"`."""

    kitchen = item("a1", title="Wipe down the kitchen", due="2026-10-05", stake=("minor", "mess"), saved=at(4))
    landlord = item("a2", title="Reply to the landlord", due="2026-10-07", owed_to="the landlord",
                    stake=("real_cost", "the renewal lapses"), saved=at(2))

    def asks(ranking: str) -> str:
        review = ReviewInputs(
            items=wrap(kitchen, landlord), calendar_events=(), calendar_marked_ids=frozenset(), open_requests=(),
            history=(), source_status={}, now=NOW, ranking=ranking,
        )
        return what_next(OrientationInputs(review=review), NOW).top_pick.title

    assert asks(RANKING_POINTS) == "Reply to the landlord"
    assert asks(RANKING_RULES) == "Wipe down the kitchen"



def test_planned_period_counts_as_picked_only_inside_its_days():
    planned = {"words": "this week", "read_by": "code", "read_on": TODAY.isoformat(), "start": "2026-10-05", "end": "2026-10-07"}
    task = replace(item("period"), data={"level": "accepted", "size": "task", "dates": {"planned": planned}})
    assert points_of(task)["picked"] == 8.0
    assert "picked" not in points_of(task, now=NOW-timedelta(days=1))
    assert "picked" not in points_of(task, now=NOW+timedelta(days=3))
    assert due_moment(task, TODAY) is None


def test_review_marked_parts_do_not_inherit_the_parent_deadline():
    parent = replace(item("parent"), data={"level": "accepted", "size": "task", "dates": {"deadline": work_date("2026-10-06")}})
    child = replace(item("child"), data={"level": "accepted", "size": "task", "parent": parent.id,
                    "review_marks": [{"mark": "agents_wording", "part": "title"}]})
    assert "due" not in points_of(child, parent)
    accepted = replace(child, data={key: value for key, value in child.data.items() if key != "review_marks"})
    assert "due" in points_of(accepted, parent)


def test_retired_importance_never_adds_points_or_pins():
    task = replace(item("old"), data={"level": "accepted", "size": "task", "importance": {"level": "high", "confirmed": True, "reason": "old"}})
    assert points_of(task) == {"total": 0.0}
