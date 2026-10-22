"""Ciel rebuild slice 3 (ROA-65), Brain Core half: importance (N5) and focus projects (N4).

Drives the protocol dispatcher against a temporary Brain Home root, like the step C acceptance
tests. The rebuild plan's done-when: a proposal saves unconfirmed, Nic's yes confirms it, an
agent can't confirm on its own, and focus projects save only in his words. See
docs/product/2026-09-28-ciel-rebuild-plan.md (slice 3) and docs/product/steps/c-record-format.md §13.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from brain_core import language
from brain_core.errors import BrainError, ErrorCode
from brain_core.records import RECORD_SCHEMA_V2, RecordStore, new_record_id

from test_step_c_acceptance import (  # noqa: E402 - pytest puts tests/integration on sys.path
    ACTOR,
    NOW,
    NOW_ISO,
    OTHER_ACTOR,
    _entropy,
    agent_source,
    dispatch,
    excerpt_evidence,
    item_mapping,
    owner_source,
)


@pytest.fixture()
def brain_home(tmp_path: Path) -> str:
    (tmp_path / "records").mkdir(parents=True)
    return str(tmp_path)


def _files(brain_home: str) -> list[bytes]:
    return sorted(path.read_bytes() for path in Path(brain_home).rglob("*.md"))


def _high(*, confirmed: bool, proposed_by: str = OTHER_ACTOR) -> dict[str, object]:
    return {"level": "high", "reason": "the visa lapses and the trip is off", "proposed_by": proposed_by, "confirmed": confirmed}


def test_a_proposal_saves_unconfirmed_and_only_nics_yes_confirms_it(brain_home: str) -> None:
    words = "I need to renew my passport before the Japan trip"
    created = dispatch(
        "record.create",
        brain_home=brain_home,
        actor=ACTOR,
        record=item_mapping(
            title="renew my passport", state="open", data={"level": "accepted", "size": "task"},
            words=words, said_by="owner", came_by="direct",
        ),
    )

    # Diablo proposes high importance with its reason: saved, unconfirmed.
    guess = "Slipping means no Japan trip"
    proposed = dispatch(
        "record.transition",
        brain_home=brain_home,
        record_id=created["record"]["id"],
        data={"importance": _high(confirmed=False)},
        source={**agent_source(guess, speaker=OTHER_ACTOR), "recorded_by": OTHER_ACTOR},
        actor=OTHER_ACTOR,
        expected_sha256=created["receipt"]["content_sha256"],
        evidence=excerpt_evidence(guess),
        now=(NOW + timedelta(seconds=1)).isoformat(),
        entropy=_entropy(),
    )
    assert proposed["record"]["data"]["importance"] == _high(confirmed=False)

    # An agent can't confirm it on its own, and a refusal writes nothing.
    before = _files(brain_home)
    with pytest.raises(BrainError) as raised:
        dispatch(
            "record.transition",
            brain_home=brain_home,
            record_id=proposed["record"]["id"],
            data={"importance": _high(confirmed=True)},
            source={**agent_source(guess, speaker=OTHER_ACTOR), "recorded_by": OTHER_ACTOR},
            actor=OTHER_ACTOR,
            expected_sha256=proposed["receipt"]["content_sha256"],
            evidence=excerpt_evidence(guess),
            now=(NOW + timedelta(seconds=2)).isoformat(),
            entropy=_entropy(),
        )
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS
    assert _files(brain_home) == before

    # Nic's yes, told to a different agent, confirms it; the proposer stays Diablo.
    confirmed = dispatch(
        "record.transition",
        brain_home=brain_home,
        record_id=proposed["record"]["id"],
        data={"importance": _high(confirmed=True)},
        source=owner_source("yes, that one's important"),
        actor=ACTOR,
        expected_sha256=proposed["receipt"]["content_sha256"],
        evidence=excerpt_evidence("yes, that one's important"),
        now=(NOW + timedelta(seconds=3)).isoformat(),
        entropy=_entropy(),
    )
    assert confirmed["record"]["data"]["importance"] == _high(confirmed=True)
    assert confirmed["record"]["source"]["words"] == "yes, that one's important"


def _preference(words: str, projects: list[str], *, said_by: str, at_seconds: int) -> dict[str, object]:
    moment = NOW + timedelta(seconds=at_seconds)
    source = owner_source(words) if said_by == "owner" else agent_source(words)
    return {
        "schema": RECORD_SCHEMA_V2,
        "id": new_record_id(moment, _entropy()),
        "kind": "preference",
        "title": "Focus projects",
        "state": "active",
        "created_at": moment.isoformat(),
        "updated_at": moment.isoformat(),
        "sensitivity": "private",
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [excerpt_evidence(words)],
        "supersedes": [],
        "dedupe_key": None,
        "data": {"scope": "personal", "stability": "durable", "evidence": [], "correction_history": [], "focus_projects": projects},
        "body": words,
        "source": source,
    }


def test_focus_projects_save_only_in_nics_words_and_the_newest_wins(brain_home: str) -> None:
    with pytest.raises(BrainError) as raised:
        dispatch(
            "record.create", brain_home=brain_home, actor=ACTOR,
            record=_preference("Nic seems focused on Exosuit", ["vault_exosuit"], said_by="agent", at_seconds=0),
        )
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS
    assert _files(brain_home) == []

    dispatch(
        "record.create", brain_home=brain_home, actor=ACTOR,
        record=_preference("my focus is Exosuit", ["vault_exosuit"], said_by="owner", at_seconds=1),
    )
    newest = dispatch(
        "record.create", brain_home=brain_home, actor=ACTOR,
        record=_preference("focus on Exosuit and the job search now", ["vault_exosuit", "job-search"], said_by="owner", at_seconds=2),
    )

    chosen = language.focus_preference(RecordStore(brain_home).current())
    assert chosen is not None and chosen.id == newest["record"]["id"]
    assert chosen.data["focus_projects"] == ["vault_exosuit", "job-search"]
