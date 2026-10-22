from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from brain_core.errors import BrainError, ErrorCode
from brain_core.language import (
    TIER_1_ACTIONS,
    TIER_3_ACTIONS,
    TIERS,
    _owner_words,
    actionable,
    check_change,
    DEFAULT_WORKING_WINDOW,
    focus_preference,
    working_window,
    working_window_preference,
    is_bare_reply,
    is_save_instruction_only,
    may_act,
    plain_word,
)
from brain_core.records import RECORD_SCHEMA_V1, RECORD_SCHEMA_V2, Evidence, Record

NOW = datetime(2026, 9, 26, 13, 0, 0, tzinfo=UTC)
ACTOR = "chatgpt-web"
OTHER_ACTOR = "damien"

EVIDENCE = (
    Evidence(
        source_type="session_message",
        locator="session:test:1",
        content_sha256="a" * 64,
        excerpt="I'll pay the electricity bill Friday",
    ),
)


def owner_source(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "said_by": "owner",
        "came_by": "direct",
        "words": "I'll pay the electricity bill Friday",
        "recorded_by": ACTOR,
    }
    value.update(changes)
    return value


def agent_source(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "said_by": "agent",
        "came_by": "agent_guess",
        "speaker": "chatgpt-web",
        "words": "maybe pay the bill",
        "recorded_by": ACTOR,
    }
    value.update(changes)
    return value


def item(
    *,
    level: str = "possible",
    state: str = "open",
    source: dict[str, object] | None = None,
    data: dict[str, object] | None = None,
    schema: str = RECORD_SCHEMA_V2,
) -> Record:
    merged_data: dict[str, object] = {"level": level, "size": "task", "timing": {}}
    if data:
        merged_data.update(data)
    return Record(
        schema=schema,
        id="brn_20260926_130000_a1b2",
        kind="item",
        title="pay the electricity bill",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=EVIDENCE,
        supersedes=(),
        dedupe_key=None,
        data=merged_data,
        body="",
        source=source if source is not None else owner_source(),
        links=(),
    )


def preference(*, source: dict[str, object] | None = None) -> Record:
    return Record(
        schema=RECORD_SCHEMA_V2,
        id="brn_20260926_130000_c3d4",
        kind="preference",
        title="concise receipts",
        state="active",
        created_at=NOW,
        updated_at=NOW,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=EVIDENCE,
        supersedes=(),
        dedupe_key=None,
        data={"scope": "communication", "stability": "durable", "evidence": [], "correction_history": []},
        body="",
        source=source if source is not None else owner_source(),
        links=(),
    )


def fact(*, source: dict[str, object] | None = None) -> Record:
    return Record(
        schema=RECORD_SCHEMA_V2,
        id="brn_20260926_130000_e5f6",
        kind="fact",
        title="Nic prefers concise receipts",
        state="current",
        created_at=NOW,
        updated_at=NOW,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=EVIDENCE,
        supersedes=(),
        dedupe_key=None,
        data={"observed_at": NOW.isoformat(), "valid_until": None},
        body="",
        source=source if source is not None else owner_source(),
        links=(),
    )


def test_owner_words_rejects_a_missing_or_empty_source() -> None:
    assert _owner_words(None) is False
    assert _owner_words({}) is False


def test_may_act_tiers() -> None:
    for action in TIER_1_ACTIONS:
        assert may_act(action) == "yes"
    for action in TIER_3_ACTIONS:
        assert may_act(action) == "ask"
    assert may_act("something_unlisted") == "ask"
    assert set(TIERS["tier_1"]) == TIER_1_ACTIONS
    assert set(TIERS["tier_3"]) == TIER_3_ACTIONS


@pytest.mark.parametrize(
    ("data", "word"),
    [
        ({"level": "possible"}, "idea"),
        ({"level": "suggested"}, "suggestion"),
        ({"level": "accepted", "size": "goal"}, "goal"),
        ({"level": "accepted", "size": "task", "repeat": "weekly:sun"}, "routine"),
        ({"level": "accepted", "size": "task", "repeat": None, "owed_to": "electricity company"}, "promise"),
        ({"level": "accepted", "size": "task", "repeat": None, "owed_to": None}, "task"),
    ],
)
def test_plain_word(data: dict[str, object], word: str) -> None:
    assert plain_word(data) == word


def test_actionable_includes_only_accepted_open_waiting_or_blocked_items() -> None:
    accepted_open = item(level="accepted", state="open")
    accepted_done = item(level="accepted", state="done", data={"check": {"locator": "x", "checked_by": "y"}})
    accepted_waiting = item(level="accepted", state="waiting")
    suggested = item(level="suggested", state="open")
    not_an_item = preference()
    records = (accepted_open, accepted_done, accepted_waiting, suggested, not_an_item)
    assert actionable(records) == (accepted_open, accepted_waiting)


def test_check_change_skips_v1_records() -> None:
    v1 = item(schema=RECORD_SCHEMA_V1, source=None)
    check_change(None, v1, actor=ACTOR)  # must not raise


def test_check_change_actor_mismatch() -> None:
    proposed = item(source=owner_source(recorded_by="someone-else"))
    with pytest.raises(BrainError) as raised:
        check_change(None, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.ACTOR_MISMATCH


def test_check_change_preference_requires_owner_words() -> None:
    check_change(None, preference(), actor=ACTOR)  # owner source: fine
    with pytest.raises(BrainError) as raised:
        check_change(None, preference(source=agent_source()), actor=ACTOR)
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS


def test_check_change_accepts_create_at_accepted_from_owner_words() -> None:
    proposed = item(level="accepted", source=owner_source())
    check_change(None, proposed, actor=ACTOR)


def test_check_change_refuses_create_at_accepted_from_agent_guess() -> None:
    proposed = item(level="accepted", source=agent_source())
    with pytest.raises(BrainError) as raised:
        check_change(None, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS


def test_check_change_refuses_raising_level_from_agent_source() -> None:
    previous = item(level="possible", source=owner_source())
    proposed = item(level="accepted", source=agent_source())
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS


def test_check_change_allows_raising_level_from_owner_words() -> None:
    previous = item(level="possible", source=owner_source())
    proposed = item(level="suggested", source=owner_source())
    check_change(previous, proposed, actor=ACTOR)


def test_check_change_refuses_lowering_level_from_agent_source() -> None:
    previous = item(level="accepted", source=owner_source())
    proposed = item(level="suggested", source=agent_source())
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS


def test_check_change_allows_unchanged_level_from_an_agent() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(level="accepted", state="waiting", source=agent_source())
    check_change(previous, proposed, actor=ACTOR)


@pytest.mark.parametrize("state", ["dropped", "parked", "no_longer_needed"])
def test_check_change_refuses_restricted_states_from_an_agent(state: str) -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(
        level="accepted",
        state=state,
        source=agent_source(),
        data={"reason": "no longer relevant", "parked_until": "2026-10-02"},
    )
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS


@pytest.mark.parametrize("state", ["dropped", "parked", "no_longer_needed"])
def test_check_change_allows_restricted_states_from_the_owner(state: str) -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(
        level="accepted",
        state=state,
        source=owner_source(),
        data={"reason": "no longer relevant", "parked_until": "2026-10-02"},
    )
    check_change(previous, proposed, actor=ACTOR)


def test_check_change_refuses_dropped_without_reason() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(level="accepted", state="dropped", source=owner_source(), data={"reason": None})
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.REASON_REQUIRED


def test_check_change_done_needs_check_by_someone_else() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    claimed_by_self = item(
        level="accepted",
        state="done",
        source=agent_source(),
        data={"check": {"locator": "gt:request:1", "checked_by": ACTOR}},
    )
    with pytest.raises(BrainError) as raised:
        check_change(previous, claimed_by_self, actor=ACTOR)
    assert raised.value.code is ErrorCode.DONE_NEEDS_CHECK

    checked_by_other = item(
        level="accepted",
        state="done",
        source=agent_source(),
        data={"check": {"locator": "gt:request:1", "checked_by": OTHER_ACTOR}},
    )
    check_change(previous, checked_by_other, actor=ACTOR)


@pytest.mark.parametrize("state,original,changed", [
    ("parked", {"parked_until": "2026-10-02"}, {"parked_until": "2026-06-01"}),
    ("dropped", {"reason": "no longer relevant"}, {"reason": "an agent's replacement reason"}),
    ("done", {"check": {"locator": "original", "checked_by": OTHER_ACTOR}},
     {"check": {"locator": "replacement", "checked_by": ACTOR}}),
])
@pytest.mark.parametrize("message", [None, "maybe pay the bill"])
def test_closed_state_details_cannot_be_changed_by_agent(state, original, changed, message):
    previous = item(level="accepted", state=state, data=original)
    proposed = replace(previous, source=agent_source(), data={**previous.data, **changed})
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR, message=message)
    expected = ErrorCode.DONE_NEEDS_CHECK if state == "done" and message is None else ErrorCode.NEEDS_OWNER_WORDS
    assert raised.value.code is expected


@pytest.mark.parametrize("state,data", [
    ("parked", {"parked_until": "2026-10-02"}),
    ("dropped", {"reason": "no longer relevant"}),
    ("done", {"check": {"locator": "original", "checked_by": OTHER_ACTOR}}),
    ("no_longer_needed", {}),
])
@pytest.mark.parametrize("message", [None, "maybe pay the bill"])
def test_closed_state_unchanged_details_can_be_carried_by_agent(state, data, message):
    previous = item(level="accepted", state=state, data=data)
    check_change(previous, replace(previous, source=agent_source()), actor=ACTOR, message=message)


@pytest.mark.parametrize("state,original,changed", [
    ("parked", {"parked_until": "2026-10-02"}, {"parked_until": "2026-06-01"}),
    ("dropped", {"reason": "no longer relevant"}, {"reason": "an agent's replacement reason"}),
    ("done", {"check": {"locator": "original", "checked_by": OTHER_ACTOR}},
     {"check": {"locator": "replacement", "checked_by": ACTOR}}),
])
def test_legacy_revise_refuses_closed_details_and_leaves_no_partial_version(tmp_path, state, original, changed):
    from hashlib import sha256
    from brain_core.records import RecordStore, record_from_mapping
    previous = record_from_mapping(item(level="accepted", state=state, data=original).as_dict())
    store = RecordStore(tmp_path)
    receipt = store.create(previous)
    words = "maybe pay the bill"
    with pytest.raises(BrainError) as raised:
        store.revise(previous.id, now=NOW + timedelta(seconds=1), data=changed,
                     expected_sha256=receipt.content_sha256, actor=ACTOR, source=agent_source(),
                     evidence=(Evidence(source_type="session_message", locator="message", excerpt=words,
                                        content_sha256=sha256(words.encode()).hexdigest()),))
    assert raised.value.code is (ErrorCode.DONE_NEEDS_CHECK if state == "done" else ErrorCode.NEEDS_OWNER_WORDS)
    assert store.all() == (previous,)


def test_same_done_state_self_check_is_refused_even_with_owner_words():
    previous = item(level="accepted", state="done", data={"check": {"locator": "original", "checked_by": OTHER_ACTOR}})
    proposed = replace(previous, data={**previous.data, "check": {"locator": "replacement", "checked_by": ACTOR}})
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR, message=proposed.source["words"])
    assert raised.value.code is ErrorCode.DONE_NEEDS_CHECK


@pytest.mark.parametrize("state,original,changed", [
    ("parked", {"parked_until": "2026-10-02"}, {"parked_until": "2026-06-01"}),
    ("dropped", {"reason": "no longer relevant"}, {"reason": "an agent's replacement reason"}),
    ("done", {"check": {"locator": "original", "checked_by": OTHER_ACTOR}},
     {"check": {"locator": "replacement", "checked_by": ACTOR}}),
])
def test_dropped_question_cannot_authorise_closed_defining_field_changes(state, original, changed):
    previous = item(level="accepted", state=state, data=original)
    proposed = dropped_question_record(previous, "save it")
    check_change(previous, proposed, actor=ACTOR, message="save it", dropped_question=True)
    with pytest.raises(BrainError) as raised:
        check_change(previous, replace(proposed, data={**proposed.data, **changed}), actor=ACTOR,
                     message="save it", dropped_question=True)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_check_change_refuses_timing_not_from_words() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(
        level="accepted",
        state="open",
        source=owner_source(),
        data={"timing": {"due": "2026-10-02"}},
    )
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.TIME_NOT_FROM_WORDS


def test_check_change_refuses_timing_words_not_in_source() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(
        level="accepted",
        state="open",
        source=owner_source(words="I'll pay the electricity bill Friday"),
        data={"timing": {"due": "2026-10-02", "resolved_from": {"words": "next Tuesday", "today": "2026-09-26"}}},
    )
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.TIME_NOT_FROM_WORDS


def test_check_change_accepts_timing_words_taken_word_by_word_from_source() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(
        level="accepted",
        state="open",
        source=owner_source(words="tmr need to bring banner to printer at 12pm"),
        data={"timing": {"due": "2026-09-30", "at": "12:00", "resolved_from": {"words": "tmr at 12pm", "today": "2026-09-29"}}},
    )
    check_change(previous, proposed, actor=ACTOR)


def test_check_change_does_not_recheck_timing_carried_over_unchanged() -> None:
    timing = {"due": "2026-10-02", "resolved_from": {"words": "Friday", "today": "2026-09-26"}}
    previous = item(level="accepted", state="open", source=owner_source(), data={"timing": timing})
    proposed = item(
        level="accepted",
        state="waiting",
        source=agent_source(words="mark it waiting"),
        data={"timing": timing},
    )
    check_change(previous, proposed, actor=ACTOR)  # unrelated words; timing itself is unchanged


def test_check_change_allows_timing_resolved_from_words_in_source() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(
        level="accepted",
        state="open",
        source=owner_source(words="I'll pay the electricity bill Friday"),
        data={"timing": {"due": "2026-10-02", "resolved_from": {"words": "Friday", "today": "2026-09-26"}}},
    )
    check_change(previous, proposed, actor=ACTOR)


def test_check_change_ignores_non_item_non_preference_kinds() -> None:
    fact = Record(
        schema=RECORD_SCHEMA_V2,
        id="brn_20260926_130000_e5f6",
        kind="fact",
        title="Nic prefers concise receipts",
        state="current",
        created_at=NOW,
        updated_at=NOW,
        assertion=None,
        confidence=None,
        sensitivity="private",
        vaults=(),
        owners=(),
        entities=(),
        evidence=EVIDENCE,
        supersedes=(),
        dedupe_key=None,
        data={"observed_at": NOW.isoformat(), "valid_until": None},
        body="",
        source=owner_source(),
        links=(),
    )
    check_change(None, fact, actor=ACTOR)


_SAVE_INSTRUCTION_VARIANTS = [
    "Save it to gtbc",
    "save it to gtbc.",
    "SAVE IT TO GTBC",
    "save   it  to   gtbc",
    "Add that to brain home please",
    "store this in gt",
    "remember it",
    "note down",
    "put that into gremlin town pls",
]

# 2026-09-29 (Nic approved widening R11): Fleet Boss saved three items with the words
# "store in brain home 3, 4, 5" - a save command ending in a list reference, which the bare
# grammar missed. List references (numbers or pointers) at the end or between the verb and
# the destination are still a save command, never Nic's words about an item.
_SAVE_INSTRUCTIONS_WITH_LIST_REFERENCES = [
    "store in brain home 3, 4, 5",
    "save 3, 4 and 5",
    "save #3 to gtbc",
    "add these to brain home",
    "save all of them",
    "save items 3-5 pls",
    "save 3 to 5",
    "save 3 and 4",
    "save number 3",
    "save no. 3 to gt",
    "remember those",
    "log both",
    "add everything to brain home",
    "save all of these",
    "put them in gtbc",
]

_REAL_STATEMENTS_MENTIONING_SAVE = [
    "save the receipts for the tax claim",
    "remember to call mum",
    "I'll pay the electricity bill Friday",
    "store 3 boxes in the garage",
    "add 2 eggs to the shopping list",
    "note that Sam owes me $40",
    "remember to call mum at 3",
]


@pytest.mark.parametrize("words", _SAVE_INSTRUCTION_VARIANTS + _SAVE_INSTRUCTIONS_WITH_LIST_REFERENCES)
def test_is_save_instruction_only_matches_bare_save_commands(words: str) -> None:
    assert is_save_instruction_only(words) is True


@pytest.mark.parametrize("words", _REAL_STATEMENTS_MENTIONING_SAVE)
def test_is_save_instruction_only_rejects_real_statements(words: str) -> None:
    assert is_save_instruction_only(words) is False


@pytest.mark.parametrize("words", _SAVE_INSTRUCTION_VARIANTS + _SAVE_INSTRUCTIONS_WITH_LIST_REFERENCES)
def test_check_change_refuses_save_instruction_words_from_owner(words: str) -> None:
    """ROA-8: ChatGPT recorded its own save command ("Save it to gtbc") as Nic's exact
    words and the record was accepted. R11 refuses it regardless of level or state."""

    proposed = item(level="possible", source=owner_source(words=words))
    with pytest.raises(BrainError) as raised:
        check_change(None, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert "words" in raised.value.message


def test_check_change_allows_save_instruction_words_from_agent_source() -> None:
    """The same words are fine when they are not claimed as Nic's own; R2 still caps an
    agent-sourced item at a suggested level (untested here)."""

    proposed = item(level="suggested", source=agent_source(words="Save it to gtbc"))
    check_change(None, proposed, actor=ACTOR)


@pytest.mark.parametrize("words", _REAL_STATEMENTS_MENTIONING_SAVE)
def test_check_change_allows_real_statements_that_merely_mention_save(words: str) -> None:
    proposed = item(level="accepted", source=owner_source(words=words))
    check_change(None, proposed, actor=ACTOR)


def test_check_change_refuses_save_instruction_words_for_a_fact() -> None:
    """R11 runs before the kind-specific branches, so it also covers facts, not just items."""

    with pytest.raises(BrainError) as raised:
        check_change(None, fact(source=owner_source(words="Save it to gtbc")), actor=ACTOR)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_check_change_refuses_save_instruction_words_for_a_preference() -> None:
    """Without R11 this would sail through the preference branch (said_by is owner) and
    confirm "save it to gtbc" itself as a preference."""

    with pytest.raises(BrainError) as raised:
        check_change(None, preference(source=owner_source(words="Save it to gtbc")), actor=ACTOR)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_check_change_refuses_save_instruction_words_on_a_change() -> None:
    previous = item(level="accepted", state="open", source=owner_source())
    proposed = item(level="accepted", state="waiting", source=owner_source(words="Save it to gtbc"))
    with pytest.raises(BrainError) as raised:
        check_change(previous, proposed, actor=ACTOR)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# R12 and focus projects (Ciel rebuild slice 3; docs/product/steps/c-record-format.md §13).
# ---------------------------------------------------------------------------


def high(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "level": "high",
        "reason": "missing it loses the booking",
        "proposed_by": ACTOR,
        "confirmed": False,
    }
    value.update(changes)
    return value


def test_check_change_lets_an_agent_propose_importance_unconfirmed() -> None:
    check_change(None, item(level="suggested", source=agent_source(), data={"importance": high()}), actor=ACTOR)
    previous = item(level="accepted", source=owner_source())
    check_change(previous, item(level="accepted", source=agent_source(), data={"importance": high()}), actor=ACTOR)


def test_check_change_refuses_an_agent_confirming_importance() -> None:
    confirmed = high(confirmed=True)
    with pytest.raises(BrainError) as raised:
        check_change(None, item(level="suggested", source=agent_source(), data={"importance": confirmed}), actor=ACTOR)
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS

    previous = item(level="accepted", source=owner_source(), data={"importance": high()})
    with pytest.raises(BrainError) as raised:
        check_change(previous, item(level="accepted", source=agent_source(), data={"importance": confirmed}), actor=ACTOR)
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS


def test_check_change_confirms_importance_from_nics_words_keeping_the_proposer() -> None:
    """Nic's yes may reach a different agent than the one that proposed it: confirming alone
    keeps the original proposer, so it need not be the caller."""

    previous = item(level="accepted", source=owner_source(), data={"importance": high(proposed_by=OTHER_ACTOR)})
    proposed = item(
        level="accepted",
        source=owner_source(words="yes"),
        data={"importance": high(proposed_by=OTHER_ACTOR, confirmed=True)},
    )
    check_change(previous, proposed, actor=ACTOR)


def test_check_change_accepts_importance_nic_states_himself_when_saved() -> None:
    check_change(None, item(level="accepted", source=owner_source(), data={"importance": high(confirmed=True)}), actor=ACTOR)


def test_check_change_refuses_a_proposal_in_someone_elses_name() -> None:
    elsewhere = {"importance": high(reason="a different reason", proposed_by=OTHER_ACTOR)}
    cases = (
        (None, item(level="suggested", source=agent_source(), data=elsewhere)),
        (
            item(level="accepted", source=owner_source(), data={"importance": high()}),
            item(level="accepted", source=agent_source(), data=elsewhere),
        ),
    )
    for previous, proposed in cases:
        with pytest.raises(BrainError) as raised:
            check_change(previous, proposed, actor=ACTOR)
        assert raised.value.code is ErrorCode.ACTOR_MISMATCH


def test_check_change_lets_an_agent_restate_or_withdraw_an_unconfirmed_proposal() -> None:
    previous = item(level="accepted", source=owner_source(), data={"importance": high(proposed_by=OTHER_ACTOR)})
    restated = item(level="accepted", source=agent_source(), data={"importance": high(reason="it blocks the move")})
    check_change(previous, restated, actor=ACTOR)
    withdrawn = item(level="accepted", source=agent_source(), data={})
    check_change(previous, withdrawn, actor=ACTOR)


def test_check_change_refuses_an_agent_undoing_confirmed_importance() -> None:
    previous = item(level="accepted", source=owner_source(), data={"importance": high(confirmed=True)})
    for data in ({}, {"importance": high()}, {"importance": high(reason="another", confirmed=True)}):
        with pytest.raises(BrainError) as raised:
            check_change(previous, item(level="accepted", source=agent_source(), data=data), actor=ACTOR)
        assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS
    check_change(previous, item(level="accepted", source=owner_source(), data={}), actor=ACTOR)


def test_check_change_leaves_carried_over_importance_alone() -> None:
    """A change about something else carries importance over unchanged; no owner words and no
    proposer check, even for a confirmed one proposed by another agent."""

    carried = {"importance": high(proposed_by=OTHER_ACTOR, confirmed=True)}
    previous = item(level="accepted", state="open", source=owner_source(), data=carried)
    check_change(previous, item(level="accepted", state="waiting", source=agent_source(), data=carried), actor=ACTOR)


def _focus(record_id: str, projects: list[str], *, at: datetime = NOW, **changes: object) -> Record:
    fields: dict[str, object] = {
        "id": record_id,
        "created_at": at,
        "updated_at": at,
        "data": {**preference().data, "focus_projects": projects},
    }
    fields.update(changes)
    return replace(preference(), **fields)


def test_focus_preference_is_the_newest_current_one_in_nics_words() -> None:
    older = _focus("brn_20260926_130000_f001", ["vault_exosuit"])
    newer = _focus("brn_20260926_140000_f002", ["vault_exosuit", "job search"], at=NOW + timedelta(hours=1))
    no_focus = replace(preference(), id="brn_20260926_150000_f003", created_at=NOW + timedelta(hours=2), updated_at=NOW + timedelta(hours=2))
    assert focus_preference([older, newer, no_focus]) == newer
    assert focus_preference([newer, older]).data["focus_projects"] == ["vault_exosuit", "job search"]
    assert focus_preference([no_focus, item()]) is None
    assert focus_preference([]) is None


def test_focus_preference_skips_replaced_retired_agent_said_and_v1_records() -> None:
    kept = _focus("brn_20260926_130000_f001", ["kept"])
    later = NOW + timedelta(hours=1)
    replaced = _focus("brn_20260926_140000_f002", ["replaced"], at=later)
    successor = _focus("brn_20260926_140000_f003", ["retired"], at=later, state="retired", supersedes=(replaced.id,))
    agent_said = _focus("brn_20260926_140000_f004", ["guessed"], at=later, source=agent_source())
    v1 = _focus("brn_20260926_140000_f005", ["old"], at=later, schema=RECORD_SCHEMA_V1, source=None)
    assert focus_preference([kept, replaced, successor, agent_said, v1]) == kept


def test_focus_preference_may_name_no_projects() -> None:
    cleared = _focus("brn_20260926_140000_f002", [], at=NOW + timedelta(hours=1))
    assert focus_preference([_focus("brn_20260926_130000_f001", ["vault_exosuit"]), cleared]) == cleared


# ---------------------------------------------------------------------------
# Release ciel-03 slice 1 (Ciel and Brain Home PRD §3; ROA-75): guessed stakes and rough lengths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["real_cost", "minor"])
def test_check_change_lets_an_agent_guess_a_stake_unconfirmed(level: str) -> None:
    previous = item(level="accepted")
    guessed = item(level="accepted", source=agent_source(), data={"importance": high(level=level)})
    check_change(previous, guessed, actor=ACTOR)


def test_check_change_refuses_an_agent_lowering_a_confirmed_cant_undo_to_a_guess() -> None:
    previous = item(level="accepted", data={"importance": high(confirmed=True)})
    lowered = item(level="accepted", source=agent_source(), data={"importance": high(level="real_cost")})
    with pytest.raises(BrainError) as raised:
        check_change(previous, lowered, actor=ACTOR)
    assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS


def test_check_change_needs_no_time_words_for_a_rough_length() -> None:
    length = {"timing": {"duration_minutes": 30, "padded_minutes": 45, "estimated_by": ACTOR}}
    check_change(item(level="accepted"), item(level="accepted", source=agent_source(), data=length), actor=ACTOR)
    check_change(None, item(level="suggested", source=agent_source(), data=length), actor=ACTOR)


def test_check_change_still_needs_time_words_for_a_start_by_date() -> None:
    start_by = {"timing": {"start_by": "2026-10-01"}}
    with pytest.raises(BrainError) as raised:
        check_change(item(level="accepted"), item(level="accepted", source=agent_source(), data=start_by), actor=ACTOR)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


def test_check_change_keeps_dates_when_only_the_length_changes() -> None:
    dated = {"due": "2026-10-02", "resolved_from": {"words": "Friday", "today": "2026-09-26"}}
    previous = item(level="accepted", data={"timing": dated})
    lengthened = {"timing": {**dated, "duration_minutes": 60, "padded_minutes": 90, "estimated_by": ACTOR}}
    check_change(previous, item(level="accepted", source=agent_source(words="about an hour"), data=lengthened), actor=ACTOR)


# Step I (PRD §4.3): Nic's working window, chosen as focus projects are.
def _window(record_id: str, window: dict[str, object], *, at: datetime = NOW, **changes: object) -> Record:
    fields: dict[str, object] = {
        "id": record_id,
        "created_at": at,
        "updated_at": at,
        "data": {**preference().data, "working_window": window},
    }
    fields.update(changes)
    return replace(preference(), **fields)


def test_working_window_defaults_to_weekdays_9_to_6_until_nic_saves_one() -> None:
    assert working_window([]) == DEFAULT_WORKING_WINDOW
    assert working_window([preference(), item()]) == DEFAULT_WORKING_WINDOW
    assert DEFAULT_WORKING_WINDOW["mon"] == ("09:00", "18:00")
    assert DEFAULT_WORKING_WINDOW["sat"] is None and DEFAULT_WORKING_WINDOW["sun"] is None


def test_working_window_is_the_newest_in_nics_words_and_a_day_left_out_is_off() -> None:
    older = _window("brn_20260926_130000_w001", {"mon": {"start": "09:00", "end": "18:00"}})
    newer = _window(
        "brn_20260926_140000_w002",
        {"mon": {"start": "08:00", "end": "17:00"}, "sat": {"start": "10:00", "end": "12:00"}, "sun": None},
        at=NOW + timedelta(hours=1),
    )
    assert working_window_preference([older, newer]) == newer
    chosen = working_window([older, newer])
    assert chosen["mon"] == ("08:00", "17:00")
    assert chosen["sat"] == ("10:00", "12:00")
    assert chosen["tue"] is None and chosen["sun"] is None
    assert list(chosen) == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def test_working_window_skips_replaced_retired_and_agent_said_records() -> None:
    kept = _window("brn_20260926_130000_w001", {"mon": {"start": "09:00", "end": "18:00"}})
    later = NOW + timedelta(hours=1)
    replaced = _window("brn_20260926_140000_w002", {"mon": {"start": "07:00", "end": "08:00"}}, at=later)
    successor = _window(
        "brn_20260926_140000_w003", {"mon": None}, at=later, state="retired", supersedes=(replaced.id,),
    )
    agent_said = _window("brn_20260926_140000_w004", {"mon": None}, at=later, source=agent_source())
    assert working_window_preference([kept, replaced, successor, agent_said]) == kept


@pytest.mark.parametrize("words", [
    "save that", "yes save it", "yes do it", "yes", "ok", "3", "1 done", "drop it",
    "do it", " OK! ", "YES, save it.", " 1   DONE! ", "ok save it", "ok do it",
    "store in brain home 3, 4, 5",
])
def test_is_bare_reply_matches_fixed_plan_forms(words: str) -> None:
    assert is_bare_reply(words) is True


@pytest.mark.parametrize("words", [
    "drop the email migration", "yes, it changes my life, about 40 display slots a month",
    "save the receipts for the tax claim", "yes save the receipts for the tax claim",
    "yes do the email migration", "3 boxes", "1 done with the email migration",
    "drop it after the email migration", "", "yep", "sure", "go ahead", "1 finished",
])
def test_is_bare_reply_keeps_named_content_and_unlisted_forms(words: str) -> None:
    assert is_bare_reply(words) is False


def work_date(words="by Friday", **changes):
    return {"words": words, "read_by": "code", "read_on": "2026-09-26", "day": "2026-10-02", **changes}


def work_item(*, dates=None, source=None, **data):
    if dates is not None:
        data["dates"] = dates
    return item(level="accepted", source=source, data=data)


def test_r7_deadline_without_deadline_word_is_refused_unless_owed_to():
    proposed = work_item(dates={"deadline": work_date("Friday")}, source=owner_source(words="Friday"))
    with pytest.raises(BrainError) as raised:
        check_change(None, proposed, actor=ACTOR)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS
    check_change(None, replace(proposed, data={**proposed.data, "owed_to": "Sam"}), actor=ACTOR)


@pytest.mark.parametrize("word", ["by", "before", "due", "latest"])
def test_r7_deadline_words_in_front_pass(word):
    words = f"{word} Friday"
    check_change(None, work_item(dates={"deadline": work_date(words)}, source=owner_source(words=words)), actor=ACTOR)


def test_r7_replaces_deadline_keeps_kind_and_its_old_deadline_word():
    previous = work_item(dates={"deadline": work_date("before December")})
    words = "actually lets say end of november"
    proposed = work_item(dates={"deadline": work_date("end of november", day="2026-11-30")}, source=owner_source(words=words))
    check_change(previous, proposed, actor=ACTOR, replaces="deadline", message=words)
    changed_kind = replace(proposed, data={**proposed.data, "dates": {"planned": proposed.data["dates"]["deadline"]}})
    with pytest.raises(BrainError) as raised:
        check_change(previous, changed_kind, actor=ACTOR, replaces="deadline", message=words)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


def test_r7_replaces_confirmed_agent_deadline_with_code_reading_without_keep():
    previous = work_item(dates={"deadline": work_date(read_by=OTHER_ACTOR, confirmed=True)})
    message = "actually lets say end of november"
    date = work_date("end of november", day="2026-11-30")
    proposed = work_item(dates={"deadline": date}, source=owner_source(words=message))
    assert previous.data["dates"]["deadline"]["confirmed"] is True
    assert date["read_by"] == "code" and "confirmed" not in date
    check_change(previous, proposed, actor=ACTOR, replaces="deadline", message=message)

    changed_kind = replace(proposed, data={**proposed.data, "dates": {"planned": date}})
    with pytest.raises(BrainError) as raised:
        check_change(previous, changed_kind, actor=ACTOR, replaces="deadline", message=message)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS

    for invalid in (
        {**date, "words": "end of december"},
        {**date, "read_by": OTHER_ACTOR, "read_on": "2026-12-01"},
    ):
        with pytest.raises(BrainError) as raised:
            check_change(previous, replace(proposed, data={**proposed.data, "dates": {"deadline": invalid}}), actor=ACTOR, replaces="deadline", message=message)
        assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


def test_r7_carried_equal_date_needs_no_new_words():
    previous = work_item(dates={"deadline": work_date()})
    check_change(previous, work_item(dates=previous.data["dates"], source=agent_source()), actor=ACTOR)


def test_r7_carried_confirmation_only_needs_no_new_date_words():
    previous = work_item(dates={"planned": work_date("Friday", read_by=ACTOR)})
    check_change(previous, work_item(dates={"planned": {**previous.data["dates"]["planned"], "confirmed": True}}, source=owner_source(words="keep this date")), actor=ACTOR)


def test_r7_carried_suggested_date_accepted_into_its_kind():
    date = work_date("Friday", read_by=ACTOR)
    previous = work_item(dates={"suggested": {**date, "kind": "planned", "speaker": "Sam"}})
    check_change(previous, work_item(dates={"planned": date}, source=owner_source(words="accept Sam's date")), actor=ACTOR)


def old_dated_item(**changes):
    timing = {"due": "2026-10-02", "resolved_from": {"words": "Friday", "today": "2026-09-26"}, **changes}
    return work_item(timing=timing)


def test_r7_carried_old_shape_mapping_needs_no_new_words():
    check_change(old_dated_item(), work_item(dates={"deadline": work_date("Friday")}, source=agent_source()), actor=ACTOR)


def test_r7_carried_move_over_rereads_only_kind_not_day_or_words():
    previous = old_dated_item(at="12:00")
    date = work_date("Friday", at="12:00")
    check_change(previous, work_item(dates={"planned": date}, source=agent_source()), actor=ACTOR, move_over=True)
    with pytest.raises(BrainError) as raised:
        check_change(previous, work_item(dates={"planned": {**date, "day": "2026-10-03"}}, source=agent_source()), actor=ACTOR, move_over=True)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


def test_r7_carried_undo_restores_the_named_version_date():
    restored = work_item(dates={"deadline": work_date()})
    previous = work_item(dates={"deadline": work_date("by November", day="2026-11-01")})
    proposed = work_item(dates=restored.data["dates"], source=owner_source(words="undo the date change"))
    check_change(previous, proposed, actor=ACTOR, restored=restored)


def test_r7_carried_undo_restores_old_shape_timing_as_it_was():
    restored = old_dated_item()
    previous = work_item(dates={"planned": work_date()})
    proposed = work_item(timing=restored.data["timing"], source=owner_source(words="undo the date change"))
    check_change(previous, proposed, actor=ACTOR, restored=restored)


def test_r7_old_window_and_start_by_mapping_are_carried():
    previous = old_dated_item(start_by="2026-10-01", window={"start": "2026-09-28", "end": "2026-10-04"})
    proposed = work_item(dates={"deadline": work_date("Friday", day="2026-10-04"), "start_by": work_date("Friday", day="2026-10-01")}, source=agent_source())
    check_change(previous, proposed, actor=ACTOR)


@pytest.mark.parametrize("field", ["dates", "expected", "needs.expected"])
def test_r7_checks_words_for_every_date_object(field):
    date = work_date("invented Friday")
    data = {"dates": {"planned": date}} if field == "dates" else ({"expected": date} if field == "expected" else {"needs": {"from": "Sam", "expected": date}})
    with pytest.raises(BrainError) as raised:
        check_change(None, work_item(**data), actor=ACTOR)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


@pytest.mark.parametrize("key", ["day", "start", "end"])
def test_r7_refuses_agent_date_before_read_on(key):
    date = work_date("Friday", read_by=ACTOR, **{key: "2026-09-25"})
    with pytest.raises(BrainError) as raised:
        check_change(None, work_item(dates={"planned": date}, source=owner_source(words="Friday")), actor=ACTOR)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


def test_r7_named_hub_code_caller_skips_only_deadline_word_gate():
    date = work_date("Friday", read_by="system:lark-bridge")
    proposed = work_item(dates={"deadline": date}, source=owner_source(words="Friday", recorded_by="system:lark-bridge"))
    check_change(None, proposed, actor="system:lark-bridge")
    with pytest.raises(BrainError) as raised:
        check_change(None, replace(proposed, source={**proposed.source, "words": "something else"}), actor="system:lark-bridge")
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS
    with pytest.raises(BrainError) as raised:
        check_change(None, replace(proposed, source=owner_source(words="Friday")), actor=ACTOR)
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


@pytest.mark.parametrize("called", ["goal", "project"])
def test_r13_setting_called_needs_owner_words(called):
    with pytest.raises(BrainError) as raised:
        check_change(work_item(), work_item(called=called, source=agent_source()), actor=ACTOR)
    assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS
    check_change(work_item(), work_item(called=called), actor=ACTOR)


def test_r13_clearing_called_needs_owner_words_but_carrying_does_not():
    previous = work_item(called="goal")
    check_change(previous, work_item(called="goal", source=agent_source()), actor=ACTOR)
    with pytest.raises(BrainError) as raised:
        check_change(previous, work_item(source=agent_source()), actor=ACTOR)
    assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS


def stake(**changes):
    return {"level": "changes_life", "at_stake": "40 display slots a month", "confirmed": False, "proposed_by": ACTOR, **changes}


def test_r14_confirming_or_changing_or_removing_a_confirmed_stake_needs_owner():
    proposed = stake(confirmed=True)
    with pytest.raises(BrainError) as raised:
        check_change(work_item(), work_item(stake=proposed, source=agent_source()), actor=ACTOR)
    assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS
    previous = work_item(stake=proposed)
    for changed in (stake(confirmed=True, level="nuisance"), None):
        with pytest.raises(BrainError) as raised:
            check_change(previous, work_item(stake=changed, source=agent_source()), actor=ACTOR)
        assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS
    check_change(previous, work_item(stake=proposed, source=agent_source()), actor=ACTOR)


def test_r14_new_or_reworded_proposal_must_name_caller_but_confirmation_keeps_proposer():
    earlier = stake(proposed_by=OTHER_ACTOR)
    with pytest.raises(BrainError) as raised:
        check_change(work_item(), work_item(stake=earlier, source=agent_source()), actor=ACTOR)
    assert raised.value.code == ErrorCode.ACTOR_MISMATCH
    check_change(work_item(stake=earlier), work_item(stake={**earlier, "confirmed": True}), actor=ACTOR)
    with pytest.raises(BrainError) as raised:
        check_change(work_item(stake=earlier), work_item(stake={**earlier, "odds": "likely"}, source=agent_source()), actor=ACTOR)
    assert raised.value.code == ErrorCode.ACTOR_MISMATCH


@pytest.mark.parametrize("words", ["invented owner quote", "yes", "3", "1 done", "drop it"])
def test_owner_quote_must_occur_in_message_and_name_something_without_answers(words):
    with pytest.raises(BrainError) as raised:
        check_change(work_item(), work_item(called="goal", source=owner_source(words=words)), actor=ACTOR, message="make the bill a goal")
    assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS


def test_owner_quote_is_matched_word_by_word():
    check_change(work_item(), work_item(called="goal", source=owner_source(words="bill a goal")), actor=ACTOR, message="make the bill into a goal")


def test_answers_question_supplies_date_words_and_reply_remains_owner_words():
    proposed = work_item(dates={"deadline": work_date()}, source=owner_source(words="yes"))
    check_change(work_item(), proposed, actor=ACTOR, message="yes", answers=["Move the bill to by Friday?"])
    with pytest.raises(BrainError) as raised:
        check_change(work_item(), proposed, actor=ACTOR, message="yes")
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


@pytest.mark.parametrize("change", ["called", "level", "dropped", "stake"])
def test_answers_question_names_title_kind_and_stake_for_owner_only_change(change):
    previous = work_item() if change != "level" else item(level="possible")
    proposed = work_item(source=owner_source(words="yes"), **({"called": "goal"} if change == "called" else {"stake": stake(confirmed=True)} if change == "stake" else {"reason": "no longer useful"} if change == "dropped" else {}))
    if change == "dropped":
        proposed = replace(proposed, state="dropped")
    check_change(previous, proposed, actor=ACTOR, message="yes", answers=["For the bill: make it a goal, accept, drop, or confirm changes_life?"])


def test_date_only_change_cannot_quote_owner_words_absent_from_message():
    with pytest.raises(BrainError) as raised:
        check_change(work_item(), work_item(dates={"planned": work_date("Friday")}, source=owner_source(words="Friday")), actor=ACTOR, message="actually Monday")
    assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS
    assert raised.value.details["field"] == "source.words"


def test_replaces_appointment_keeps_time_and_refuses_extra_content_changes():
    previous = work_item(dates={"appointment": work_date("Friday", at="12:00")})
    proposed = work_item(dates={"appointment": work_date("Saturday", day="2026-10-03", at="12:00")}, source=owner_source(words="Saturday"))
    check_change(previous, proposed, actor=ACTOR, replaces="appointment")
    changed_time = work_item(dates={"appointment": work_date("Saturday", day="2026-10-03", at="13:00")}, source=owner_source(words="Saturday"))
    with pytest.raises(BrainError) as raised:
        check_change(previous, changed_time, actor=ACTOR, replaces="appointment")
    assert raised.value.code == ErrorCode.TIME_NOT_FROM_WORDS


def test_door_done_or_undo_needs_a_nonbare_owner_quote_or_answer():
    previous = work_item()
    done = replace(work_item(source=owner_source(words="yes"), check={"locator": "message", "checked_by": "nic"}), state="done")
    for proposed, context in ((done, {}), (work_item(source=owner_source(words="yes")), {"restored": previous})):
        with pytest.raises(BrainError) as raised:
            check_change(previous, proposed, actor=ACTOR, message="yes", **context)
        assert raised.value.code == ErrorCode.NEEDS_OWNER_WORDS
    check_change(previous, done, actor=ACTOR, message="yes", answers=["Bill done?"])


def dropped_question_record(previous, message="yes save it"):
    from copy import deepcopy
    from hashlib import sha256
    from brain_core import work
    mapped = work.write_index((previous,))[work.first_id(previous)]
    data = deepcopy(mapped.data)
    marks = data.setdefault("review_marks", [])
    if len(marks) == 10:
        marks.pop(next((i for i, mark in enumerate(marks) if mark["mark"] == "question"), 0))
    marks.append({"mark": "question", "text": message + " — tick: needs a named owner quote"})
    stamp = NOW + timedelta(seconds=1)
    return replace(mapped, id="brn_20260926_130001_abcdef0123456789", created_at=stamp, updated_at=stamp,
        supersedes=(*previous.supersedes, previous.id), data=data, source=owner_source(words=message),
        evidence=(*previous.evidence, Evidence(source_type="session_message", locator="message",
            content_sha256=sha256(message.encode()).hexdigest(), excerpt=message)))


@pytest.mark.parametrize("message", ["save that", "save it", "yes save it"])
def test_bare_dropped_question_is_only_an_existing_item_question(message):
    previous = work_item()
    proposed = dropped_question_record(previous, message)
    check_change(previous, proposed, actor=ACTOR, message=message, dropped_question=True)
    if is_save_instruction_only(message):
        with pytest.raises(BrainError):
            check_change(previous, proposed, actor=ACTOR, message=message)


def test_dropped_question_maps_old_shape_only_through_w5():
    previous = item(level="accepted", data={"size": "goal", "first_step": "open the form",
        "timing": {"due": "2026-10-02", "resolved_from": {"words": "by Friday", "today": "2026-09-26"}}})
    proposed = dropped_question_record(previous)
    check_change(previous, proposed, actor=ACTOR, message="yes save it", dropped_question=True)
    assert proposed.data["called"] == "goal"
    assert proposed.data["dates"]["deadline"]["day"] == "2026-10-02"


def test_dropped_question_at_cap_preserves_unrelated_marks():
    marks = [{"mark": "agents_wording", "text": "preserve me"},
             *[{"mark": "question", "text": str(n)} for n in range(9)]]
    previous = work_item(review_marks=marks)
    proposed = dropped_question_record(previous)
    check_change(previous, proposed, actor=ACTOR, message="yes save it", dropped_question=True)
    assert proposed.data["review_marks"][0] == marks[0]


def test_dropped_question_at_cap_without_question_evicts_exactly_oldest():
    marks = [{"mark": "agents_wording", "text": str(n)} for n in range(10)]
    previous = work_item(review_marks=marks)
    proposed = dropped_question_record(previous)
    check_change(previous, proposed, actor=ACTOR, message="yes save it", dropped_question=True)
    assert proposed.data["review_marks"][:-1] == marks[1:]
    for forged in (marks[:9] + proposed.data["review_marks"][-1:],
                   marks[2:] + proposed.data["review_marks"][-1:] * 2):
        with pytest.raises(BrainError):
            check_change(previous, replace(proposed, data={**proposed.data, "review_marks": forged}),
                         actor=ACTOR, message="yes save it", dropped_question=True)


@pytest.mark.parametrize("prior", [None, stake(proposed_by=OTHER_ACTOR)])
def test_dropped_confirm_allows_only_actor_unconfirmed_proposal(prior):
    previous = work_item(**({"stake": prior} if prior else {}))
    proposed = dropped_question_record(previous, "save it")
    question = {"mark": "question", "text": "save it — stake: Move needs a non-bare quote from his message or an offered review answer"}
    proposed = replace(proposed, data={**proposed.data, "stake": stake(), "review_marks": [question]})
    check_change(previous, proposed, actor=ACTOR, message="save it", dropped_question=True)
    for forged in (stake(confirmed=True), stake(proposed_by=OTHER_ACTOR, at_stake="changed")):
        with pytest.raises(BrainError):
            check_change(previous, replace(proposed, data={**proposed.data, "stake": forged}),
                         actor=ACTOR, message="save it", dropped_question=True)
    with pytest.raises(BrainError):
        check_change(replace(previous, data={**previous.data, "stake": stake(confirmed=True)}), proposed,
                     actor=ACTOR, message="save it", dropped_question=True)
    with pytest.raises(BrainError):
        check_change(previous, replace(proposed, evidence=(*proposed.evidence[:-1], replace(proposed.evidence[-1], actor=OTHER_ACTOR))),
                     actor=ACTOR, message="save it", dropped_question=True)


@pytest.mark.parametrize("change", ["title", "state", "level", "called", "date", "stake", "next_action",
                                     "body", "links", "removed_mark", "no_question", "agent_source",
                                     "evidence", "message", "actor", "new", "fact", "preference"])
def test_dropped_question_flag_never_authorises_forged_changes(change):
    from copy import deepcopy
    previous = work_item(review_marks=[{"mark": "agents_wording", "text": "preserve me"}])
    proposed = dropped_question_record(previous)
    data = deepcopy(proposed.data)
    kwargs = {"actor": ACTOR, "message": "yes save it", "dropped_question": True}
    if change == "title":
        proposed = replace(proposed, title="different")
    elif change == "state":
        proposed = replace(proposed, state="done")
        data["check"] = {"locator": "message", "checked_by": "nic"}
    elif change == "level":
        data["level"] = "possible"
    elif change == "called":
        data["called"] = "goal"
    elif change == "date":
        data["dates"] = {"deadline": work_date()}
    elif change == "stake":
        data["stake"] = stake(confirmed=True)
    elif change == "next_action":
        data["next_action"] = {"text": "invented", "done": False}
    elif change == "body":
        proposed = replace(proposed, body="invented")
    elif change == "links":
        proposed = replace(proposed, links=({"type": "about", "target": "different"},))
    elif change == "removed_mark":
        data["review_marks"].pop(0)
    elif change == "no_question":
        data["review_marks"] = data["review_marks"][:-1]
    elif change == "agent_source":
        proposed = replace(proposed, source=agent_source(words="yes save it"))
    elif change == "evidence":
        proposed = replace(proposed, evidence=proposed.evidence[:-1])
    elif change == "message":
        kwargs["message"] = "save that"
    elif change == "actor":
        proposed = replace(proposed, source=owner_source(words="yes save it", recorded_by=OTHER_ACTOR))
    elif change == "new":
        previous = None
    elif change in {"fact", "preference"}:
        previous = replace(previous, kind=change)
        proposed = replace(proposed, kind=change)
    proposed = replace(proposed, data=data)
    with pytest.raises(BrainError):
        check_change(previous, proposed, **kwargs)
