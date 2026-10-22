"""How near Brain Home is to the limits of the scans that read it (storage-limit plan §4, §5)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from brain_core.capacity import (
    HEADS_UP,
    OK,
    OVER,
    URGENT,
    Scan,
    headroom,
    over_sentence,
    records_name,
    scan_headroom,
    tree_headroom,
    worst,
)


@dataclass(frozen=True)
class Counts:
    live_files: dict[str, int] = field(default_factory=dict)
    live_bytes: dict[str, int] = field(default_factory=dict)
    tree_entries: int = 0


TASKS = Scan(frozenset({"item"}), 5000, 32 * 1024 * 1024, stops="the review stops seeing your tasks")


@pytest.mark.parametrize(
    ("files", "level"),
    [(2450, OK), (2500, HEADS_UP), (3950, HEADS_UP), (4000, URGENT), (5000, URGENT), (5050, OVER)],
)
def test_levels_at_49_50_79_80_100_and_101_percent(files: int, level: str) -> None:
    assert scan_headroom(TASKS, Counts({"item": files}, {"item": 0})).level == level


def test_a_heads_up_an_urgent_and_an_over_say_so_in_plain_words() -> None:
    heads_up = scan_headroom(TASKS, Counts({"item": 2600}, {"item": 0}))
    urgent = scan_headroom(TASKS, Counts({"item": 4100}, {"item": 0}))
    over = scan_headroom(TASKS, Counts({"item": 5013}, {"item": 0}))
    fine = scan_headroom(TASKS, Counts({"item": 47}, {"item": 85_543}))

    assert heads_up.sentence == (
        "your task records are at 2,600 of the 5,000 the hub reads at once. Nothing is lost. Past 5,000, the review "
        "stops seeing your tasks; ask for the storage tidy-up (old versions moved into Brain Home's archive) before then."
    )
    assert urgent.sentence == (
        "your task records are at 4,100 of 5,000. Past 5,000, the review stops seeing your tasks. "
        "Ask for the storage tidy-up now."
    )
    assert over.sentence == (
        "Brain Home holds 5,013 task records and the hub reads at most 5,000 at once. Nothing is lost. "
        "Ask for the storage tidy-up now."
    )
    assert fine.sentence == "your task records are at 47 of 5,000."
    assert (fine.files, fine.bytes, fine.measure) == (47, 85_543, "files")


def test_bytes_can_bind_before_files() -> None:
    entry = scan_headroom(TASKS, Counts({"item": 100}, {"item": 27 * 1024 * 1024}))

    assert (entry.level, entry.measure) == (URGENT, "bytes")
    assert entry.sentence == (
        "your task records take 27.0 MB of 32.0 MB. Past 32.0 MB, the review stops seeing your tasks. "
        "Ask for the storage tidy-up now."
    )


def test_combined_scans_add_their_kinds_and_reach_a_cap_first() -> None:
    listed = Scan(frozenset({"item", "fact"}), 5000, 32 * 1024 * 1024)
    counts = Counts({"item": 2000, "fact": 600, "preference": 3000}, {"item": 1, "fact": 2})

    both = scan_headroom(listed, counts)
    alone = scan_headroom(TASKS, counts)

    assert (both.files, both.bytes, both.level, both.name) == (2600, 3, HEADS_UP, "task and fact records")
    assert alone.level == OK


def test_a_scan_without_a_byte_cap_is_judged_by_files_only() -> None:
    decisions = Scan(frozenset({"decision"}), 1000, None)
    entry = scan_headroom(decisions, Counts({"decision": 2}, {"decision": 10**12}))

    assert (entry.level, entry.measure, entry.max_bytes) == (OK, "files", None)


def test_the_tree_has_its_own_levels() -> None:
    assert tree_headroom(9_999).level == OK
    assert tree_headroom(10_000).level == HEADS_UP
    assert tree_headroom(19_999).level == HEADS_UP
    assert tree_headroom(20_000).level == URGENT
    assert tree_headroom(26_001).level == OVER
    assert tree_headroom(12_000).sentence == (
        "Brain Home's file list has 12,000 entries; GitHub lists it whole only up to about 26,000, and past that "
        "every Brain Home read stops. Nothing is lost. Ask for the hub to stop listing the archive before then."
    )


def test_headroom_lists_every_scan_then_the_tree_and_worst_picks_the_fullest() -> None:
    notes = Scan(frozenset({"continuity"}), 5000, 32 * 1024 * 1024)
    counts = Counts({"item": 2600, "continuity": 4200}, {}, tree_entries=225)

    entries = headroom(counts, (TASKS, notes))

    assert [entry.name for entry in entries] == ["task records", "note records", "Brain Home's file list"]
    assert worst(entries).name == "note records"
    assert worst(headroom(Counts({"item": 47}, {}, tree_entries=225), (TASKS,))) is None


def test_records_name_uses_plain_words_in_a_fixed_order() -> None:
    assert records_name({"item"}) == "task records"
    assert records_name({"fact", "item"}) == "task and fact records"
    assert records_name({"capture", "commitment", "item", "fact"}) == "task, fact, capture and commitment records"
    assert records_name({"sitrep", "focus_copy"}) == "project summary records"
    assert records_name({"continuity"}) == "note records"


def test_over_sentence_for_a_byte_cap_stopped_early_says_more_than() -> None:
    assert over_sentence("note records", 40_000_000, 32 * 1024 * 1024, measure="bytes", at_least=True) == (
        "Brain Home's note records take more than 32.0 MB and the hub reads at most 32.0 MB at once. "
        "Nothing is lost. Ask for the storage tidy-up now."
    )


def test_a_scan_needs_kinds_and_positive_caps() -> None:
    with pytest.raises(ValueError):
        Scan(frozenset(), 10)
    with pytest.raises(ValueError):
        Scan(frozenset({"item"}), 0)
    with pytest.raises(ValueError):
        Scan(frozenset({"item"}), 10, 0)


def test_a_save_stopped_by_a_limit_says_nothing_was_saved() -> None:
    assert over_sentence("task records", 5013, 5000, nothing_saved=True) == (
        "Brain Home holds 5,013 task records and the hub reads at most 5,000 at once. Nothing was saved; "
        "what's already saved is safe. Ask for the storage tidy-up now."
    )


def test_the_tree_points_at_its_own_fix_not_the_tidy_up() -> None:
    for entries in (12_000, 21_000, 27_000):
        sentence = tree_headroom(entries).sentence
        assert "stop listing the archive" in sentence and "tidy-up" not in sentence
