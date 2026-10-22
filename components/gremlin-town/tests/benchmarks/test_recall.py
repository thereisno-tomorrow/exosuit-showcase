"""Brain search recall benchmark (memory memo item M8; P6; follow-ons goal
stream 3). Builds the fixture snapshot through Brain Core's FakeGitHub the way
tests/mcp/test_github_everyday.py does, then runs the 20 fixture questions
through the exact ranking memory.search uses
(gremlin_town.mcp.github_brain_provider.GitHubBrainProvider._search).

P6 (Proposed - Nic to confirm): recall@5 >= 0.9 over 20 questions and 60
fixture records is the floor; a miss below it fails CI. Measured on the
fixture snapshot as built here: recall@1 = 1.000, recall@5 = 1.000 (0/20
misses) - comfortably above the floor, since supersession correctly drops
stale versions (printer ink, Jason's fifty dollars, the parking zone) from
the ranked results and every question's title keyword is otherwise unique
in the corpus. See recall_fixtures/questions.json for how to add a question.
"""

from __future__ import annotations

import time

import anyio

from gremlin_town.benchmarks.recall import load_questions, provider_search, run
from gremlin_town.mcp.brain_runtime import MemorySearchQuery

from recall_support import (
    FIXTURES_DIR,
    build_provider,
    fixture_snapshot_files,
    load_fixture_records,
    load_lane_b,
    search_authorization,
)


def _seeded_provider():
    fake = load_lane_b().FakeGitHub()
    fake.seed(fixture_snapshot_files())
    return build_provider(fake)


def test_recall_meets_the_floor_on_the_fixture_snapshot():
    provider = _seeded_provider()
    authorization = search_authorization()
    search = provider_search(provider, authorization)
    questions = load_questions(FIXTURES_DIR / "questions.json")

    started = time.monotonic()
    report = run(search, questions, k=5)
    elapsed = time.monotonic() - started

    assert report.total == 20
    # P6, Proposed - Nic to confirm: the agreed floor.
    assert report.recall_at_5 >= 0.9, (
        f"recall@5={report.recall_at_5:.3f} below the P6 floor; misses={report.misses}"
    )
    # Not a P6 requirement, but true on this fixture and worth pinning: every
    # question's own title keyword is unique enough that the right record is
    # not just present in the top 5 but ranked first.
    assert report.recall_at_1 >= 0.9, (
        f"recall@1={report.recall_at_1:.3f}; misses={report.misses}"
    )
    assert elapsed < 5.0, f"recall benchmark took {elapsed:.2f}s, over the 5s budget"

    # memory.search's snapshot only ever covers capture/commitment/item/fact
    # (GitHubBrainProvider._search excludes "preference"), so none of the 8
    # fixture preferences should ever come back; this would only fire if a
    # future change widened that kind set without this benchmark's fixtures
    # or questions being revisited.
    preference_ids = {
        entry["id"] for entry in load_fixture_records() if entry["kind"] == "preference"
    }
    for outcome in report.outcomes:
        assert not preference_ids.intersection(outcome.returned_record_ids), (
            f"{outcome.question_id!r} returned a preference record: {outcome.returned_record_ids}"
        )


def test_broken_ranking_would_fail_the_recall_floor():
    """Proves the P6 gate above is not vacuous. Swap the real ranking for one

    that asks the real provider for its full candidate list (limit=20, the
    maximum memory.search allows) and returns it reversed - the same
    candidates memory.search would find, in deliberately wrong order - and
    recall@5 collapses to 0.05, far below the 0.9 floor. A regression that
    broke title-priority ranking (or alias/supersession handling) would fail
    the assertion in test_recall_meets_the_floor_on_the_fixture_snapshot the
    same way this deliberately broken stand-in fails here.
    """

    provider = _seeded_provider()
    authorization = search_authorization()
    questions = load_questions(FIXTURES_DIR / "questions.json")

    def reversed_ranking(query: str, limit: int):
        result = anyio.run(provider.search, authorization, MemorySearchQuery(query, 20))
        full_rank_order = tuple(match.record_id for match in result.matches)
        return tuple(reversed(full_rank_order))[:limit]

    broken_report = run(reversed_ranking, questions, k=5)
    assert broken_report.recall_at_5 < 0.9
