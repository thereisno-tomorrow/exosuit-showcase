"""Pure recall scoring for Brain search (memory memo item M8; P6).

The ranking under test lives in ``gremlin_town.mcp.github_brain_provider``
(``GitHubBrainProvider._search``: title and alias hits outrank body-only hits,
over Brain Core's validated record shapes) - not in Brain Core, which stays a
deterministic library with no GT dependency. This module therefore lives in
GT, next to the code it measures, and stays importable by both:

- the CI test (``tests/benchmarks/test_recall.py``), which builds a fixture
  snapshot through Brain Core's ``FakeGitHub`` and calls ``provider_search``
  to get a ``SearchFn`` bound to the real ``GitHubBrainProvider.search``;
- the live runner (``gremlin_town.benchmarks.recall_cli``), which calls the
  same ``memory.search`` operation over HTTP through a hosted hub.

``run`` itself takes a plain ``SearchFn`` and never imports ``anyio``,
``httpx`` or any provider/transport type, so the scoring logic is exercised
identically in both places and is trivial to unit test on its own with a
hand-written fake.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# query, limit -> ranked record IDs, most relevant first (whatever memory.search
# would return, truncated to `limit`).
SearchFn = Callable[[str, int], Sequence[str]]


@dataclass(frozen=True, slots=True)
class RecallQuestion:
    """One fixture question: phrasing plus every record ID that answers it."""

    id: str
    text: str
    expected_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuestionOutcome:
    """What actually came back for one question, and whether it hit."""

    question_id: str
    text: str
    expected_record_ids: tuple[str, ...]
    returned_record_ids: tuple[str, ...]
    hit_at_1: bool
    hit_at_5: bool


@dataclass(frozen=True, slots=True)
class RecallReport:
    total: int
    recall_at_1: float
    recall_at_5: float
    misses: tuple[str, ...]  # question IDs with no expected record in the top 5
    outcomes: tuple[QuestionOutcome, ...]


def run(search: SearchFn, questions: Sequence[RecallQuestion], *, k: int = 5) -> RecallReport:
    """Score ``search`` against ``questions``. Pure and deterministic: the report

    depends only on what ``search`` returns for each question's text, never on
    a clock, randomness or hidden state. ``search`` itself may do I/O (a fake
    GitHub-backed provider, or a real HTTP call for the live runner); this
    function does not, so plugging in a different ``search`` is the whole
    mechanism for retargeting the same 20 questions at a fixture or a hub.

    recall@1 counts a question as a hit only when its very first result is an
    expected record. recall@5 (or whatever ``k`` is) counts a hit as soon as
    any expected record appears anywhere in the top ``k`` - a question can
    name more than one correct record (e.g. two people both waiting on Nic),
    and only one needs to surface. ``misses`` lists the question IDs that
    missed at ``k``, for a human to scan without re-deriving them.
    """

    if k < 1:
        raise ValueError("k must be at least 1")
    outcomes: list[QuestionOutcome] = []
    for question in questions:
        returned = tuple(search(question.text, k))[:k]
        expected = set(question.expected_record_ids)
        hit_at_5 = any(record_id in expected for record_id in returned)
        hit_at_1 = bool(returned) and returned[0] in expected
        outcomes.append(
            QuestionOutcome(
                question_id=question.id,
                text=question.text,
                expected_record_ids=question.expected_record_ids,
                returned_record_ids=returned,
                hit_at_1=hit_at_1,
                hit_at_5=hit_at_5,
            )
        )
    total = len(outcomes)
    recall_at_1 = sum(outcome.hit_at_1 for outcome in outcomes) / total if total else 0.0
    recall_at_5 = sum(outcome.hit_at_5 for outcome in outcomes) / total if total else 0.0
    misses = tuple(outcome.question_id for outcome in outcomes if not outcome.hit_at_5)
    return RecallReport(total, recall_at_1, recall_at_5, misses, tuple(outcomes))


def load_questions(path: str | Path) -> tuple[RecallQuestion, ...]:
    """Load the questions file (JSON; see its own ``_doc`` header for the shape).

    JSON, not YAML: GT has no YAML dependency (only Brain Core carries
    ``ruamel.yaml``, and this module must not import GT-only or BC-only
    tooling that the other side of the benchmark cannot also use), and
    the goal's own rule is YAML only where a component already depends on
    it - otherwise JSON.
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_questions = payload["questions"] if isinstance(payload, dict) else payload
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError(f"{path}: expected a non-empty 'questions' list")
    questions: list[RecallQuestion] = []
    seen_ids: set[str] = set()
    for entry in raw_questions:
        question_id = str(entry["id"])
        if question_id in seen_ids:
            raise ValueError(f"{path}: duplicate question id {question_id!r}")
        seen_ids.add(question_id)
        expected = tuple(str(record_id) for record_id in entry["expected_record_ids"])
        if not expected:
            raise ValueError(f"{path}: question {question_id!r} names no expected_record_ids")
        questions.append(RecallQuestion(question_id, str(entry["text"]), expected))
    return tuple(questions)


def provider_search(provider: Any, authorization: Any, *, query_type: type | None = None) -> SearchFn:
    """Adapt a real ``GitHubBrainProvider`` into a ``SearchFn`` via ``memory.search``.

    Kept import-light (types passed in, not imported at module scope) so a
    caller that only wants the pure ``run``/``load_questions`` half - the live
    CLI does not need this adapter at all - never pays for importing
    ``anyio`` or the MCP provider machinery. ``query_type`` defaults to
    ``gremlin_town.mcp.brain_runtime.MemorySearchQuery`` when omitted.
    """

    import anyio

    if query_type is None:
        from gremlin_town.mcp.brain_runtime import MemorySearchQuery as query_type  # noqa: N812

    def search(query: str, limit: int) -> tuple[str, ...]:
        result = anyio.run(provider.search, authorization, query_type(query, limit))
        return tuple(match.record_id for match in result.matches)

    return search
