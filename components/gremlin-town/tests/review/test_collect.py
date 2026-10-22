from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain_core.review import CalendarEvent, HistoryRow

from gremlin_town.review.calendar_link import marker
from gremlin_town.review.collect import (
    ReviewSources,
    SourceUnavailable,
    collect_moments_candidates,
    collect_review_inputs,
)

NOW = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT


def test_all_sources_absent_are_not_configured() -> None:
    inputs = collect_review_inputs(ReviewSources(), history=(), now=NOW)
    assert inputs.items == ()
    assert inputs.calendar_events == ()
    assert inputs.open_requests == ()
    assert inputs.source_status == {"brain": "not_configured", "calendar": "not_configured", "requests": "not_configured"}


def test_source_unavailable_reason_is_captured_exactly() -> None:
    def broken_calendar(start, end):
        raise SourceUnavailable("token_revoked")

    inputs = collect_review_inputs(ReviewSources(calendar_events=broken_calendar), history=(), now=NOW)
    assert inputs.source_status["calendar"] == "token_revoked"


def test_unexpected_exception_is_captured_not_raised() -> None:
    def broken_requests():
        raise RuntimeError("boom")

    inputs = collect_review_inputs(ReviewSources(open_requests=broken_requests), history=(), now=NOW)
    assert inputs.source_status["requests"] == "RuntimeError: boom"


def test_working_sources_populate_inputs_and_marked_ids() -> None:
    event = CalendarEvent(
        title="Send Sam the deck",
        start=NOW,
        end=NOW + timedelta(hours=1),
        description=f"notes {marker('brn_deck')}",
    )

    inputs = collect_review_inputs(
        ReviewSources(calendar_events=lambda start, end: (event,)),
        history=(),
        now=NOW,
    )
    assert inputs.calendar_events == (event,)
    assert inputs.calendar_marked_ids == frozenset({"brn_deck"})
    assert inputs.source_status["brain"] == "not_configured"


def test_preferences_reach_the_review_for_coming_up_and_a_failed_read_is_named() -> None:
    """N16: Nic's focus projects rank the review's "Coming up". No reader says nothing; a failed
    read is under "Couldn't see" and the review goes on without them."""

    marker_record = object()
    inputs = collect_review_inputs(ReviewSources(brain_preferences=lambda: (marker_record,)), history=(), now=NOW)
    assert inputs.preferences == (marker_record,)
    assert "preferences" not in inputs.source_status

    assert collect_review_inputs(ReviewSources(), history=(), now=NOW).preferences == ()

    def broken():
        raise SourceUnavailable("github_unavailable")

    failed = collect_review_inputs(ReviewSources(brain_preferences=broken), history=(), now=NOW)
    assert failed.preferences == ()
    assert failed.source_status["preferences"] == "github_unavailable"


def test_task_sitreps_reach_the_review_for_focus_projects_and_a_failed_read_is_named() -> None:
    """PRD §4.10: a focus project with no next action is flagged only while its sitreps are read.
    No reader leaves them unread (None) and says nothing; a failed read is under "Couldn't see"."""

    summary = object()
    inputs = collect_review_inputs(ReviewSources(task_sitreps=lambda: (summary,)), history=(), now=NOW)
    assert inputs.project_tasks == (summary,)
    assert "project summaries" not in inputs.source_status
    assert collect_review_inputs(ReviewSources(), history=(), now=NOW).project_tasks is None

    def broken():
        raise SourceUnavailable("github_unavailable")

    failed = collect_review_inputs(ReviewSources(task_sitreps=broken), history=(), now=NOW)
    assert failed.project_tasks is None
    assert failed.source_status["project summaries"] == "github_unavailable"


def test_moments_candidates_empty_history_never_calls_reader() -> None:
    def should_not_run():
        raise AssertionError("must not be called with no candidate paths")

    result, status = collect_moments_candidates(
        ReviewSources(brain_items=should_not_run), history=(), now=NOW
    )
    assert result == ()
    assert status is None


def test_moments_candidates_reads_full_index_for_shown_waiting_without_old_paths() -> None:
    seen_calls: list[str] = []

    def reader():
        seen_calls.append("full snapshot")
        return ()

    history = (
        HistoryRow(
            item_ref="brn_pick",
            channel="review",
            rule="pick_or_park",
            record_sha256="a" * 64,
            record_state="open",
            surfaced_at=NOW - timedelta(hours=1),
            response="picked_today",
            responded_at=NOW - timedelta(hours=1),
            record_path="records/item/2026/09/brn_pick.md",
        ),
        HistoryRow(
            item_ref="brn_wait",
            channel="review",
            rule="waiting_on_item",
            record_sha256="b" * 64,
            record_state="waiting",
            surfaced_at=NOW - timedelta(hours=1),
            record_path="records/item/2026/09/brn_wait.md",
        ),
        HistoryRow(
            item_ref="brn_yesterday_pick",
            channel="review",
            rule="pick_or_park",
            record_sha256="c" * 64,
            record_state="open",
            surfaced_at=NOW - timedelta(days=1),
            response="picked_today",
            responded_at=NOW - timedelta(days=1),
            record_path="records/item/2026/09/brn_yesterday_pick.md",
        ),
    )
    collect_moments_candidates(ReviewSources(brain_items=reader), history=history, now=NOW)
    assert seen_calls == ["full snapshot"]


def test_moments_candidates_reader_unavailable_is_captured() -> None:
    history = (
        HistoryRow(
            item_ref="brn_wait",
            channel="review",
            rule="waiting_on_item",
            record_sha256="b" * 64,
            record_state="waiting",
            surfaced_at=NOW,
            record_path="records/item/2026/09/brn_wait.md",
        ),
    )

    def broken():
        raise SourceUnavailable("credential_invalid")

    result, status = collect_moments_candidates(
        ReviewSources(brain_items=broken), history=history, now=NOW
    )
    assert result == ()
    assert status == "credential_invalid"


def test_moments_candidates_missing_reader_is_not_configured() -> None:
    history = (
        HistoryRow(
            item_ref="brn_wait",
            channel="review",
            rule="waiting_on_item",
            record_sha256="b" * 64,
            record_state="waiting",
            surfaced_at=NOW,
            record_path="records/item/2026/09/brn_wait.md",
        ),
    )
    result, status = collect_moments_candidates(ReviewSources(), history=history, now=NOW)
    assert result == ()
    assert status == "not_configured"
def test_morning_reads_complete_snapshot_without_legacy_marker_notes_or_facts():
    from dataclasses import replace
    from tests.review.test_run import make_item
    from brain_core.review import ItemRecord
    from gremlin_town.review.collect import collect_morning_inputs

    def unused(*args):
        raise AssertionError("work-02 does not read legacy clarification/marker sources")

    parent = ItemRecord(make_item(id="brn_parent", title="Parent"), "parent.md")
    done = ItemRecord(replace(make_item(id="brn_done", title="Done part"), state="done",
                              data={"level": "accepted", "parent": "brn_parent"}), "done.md")
    sources = ReviewSources(brain_items=lambda: (parent, done), brain_facts=unused, open_notes=unused,
                            task_sitreps=unused, calendar_events=lambda start, end: (),
                            calendar_adds_timed=True)
    result = collect_morning_inputs(sources, (), NOW)
    assert result.items == (parent, done)
    assert not hasattr(result, "calendar_marked_ids")


def test_morning_collector_passes_only_the_explicit_previous_review_timestamp():
    from datetime import timedelta
    from gremlin_town.review.collect import collect_morning_inputs

    sources = ReviewSources()
    assert collect_morning_inputs(sources, (), NOW).previous_review_at is None
    previous = NOW - timedelta(hours=7)
    assert collect_morning_inputs(sources, (), NOW, previous_review_at=previous).previous_review_at == previous


def test_morning_bootstrap_maps_old_physical_shown_refs_without_changing_raw_pins():
    from dataclasses import replace
    from brain_core.review import ItemRecord, MorningBootstrap
    from gremlin_town.review.collect import collect_morning_inputs
    from tests.review.test_run import make_item

    raw = replace(make_item(id="brn_latest", title="Edited old thing"), supersedes=("brn_first", "brn_middle"))
    entry = ItemRecord(raw, "records/raw-latest.md")
    bootstrap = MorningBootstrap("a" * 40, frozenset(("brn_migrated",)))
    facts = collect_morning_inputs(ReviewSources(brain_items=lambda: (entry,)), (), NOW,
        bootstrap=bootstrap, shown_item_refs=frozenset(("brn_middle", "unrelated")))
    assert facts.bootstrap is bootstrap
    assert facts.shown_first_ids == frozenset(("brn_first", "unrelated"))
    assert facts.items == (entry,) and facts.items[0].record is raw
    assert facts.items[0].path == entry.path and facts.items[0].sha256 == raw.sha256
