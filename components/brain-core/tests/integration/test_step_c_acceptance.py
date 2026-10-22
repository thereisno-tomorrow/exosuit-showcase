"""PRD acceptance tests 2 ("kinds stay kinds") and 8 ("limits"), Brain Core half.

Drives the protocol dispatcher (brain_core.operations.dispatch) against a temporary Brain
Home root, with a fixed now of Sat 26 Sep 2026, 21:00 +08:00. See
docs/product/steps/c-record-format.md §9.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import brain_core.operations as operations
from brain_core import language
from brain_core.errors import BrainError, ErrorCode
from brain_core.protocol import ProtocolRequest
from brain_core.records import RECORD_SCHEMA_V2, new_record_id

NOW = datetime(2026, 9, 26, 13, 0, 0, tzinfo=UTC)  # 21:00 +08:00
NOW_ISO = NOW.isoformat()
ACTOR = "chatgpt-web"
OTHER_ACTOR = "damien"

_entropy_counter = 0


def _entropy() -> str:
    global _entropy_counter
    _entropy_counter += 1
    return f"{_entropy_counter:06x}"


def request(operation: str, **parameters: object) -> ProtocolRequest:
    return ProtocolRequest("1", "step-c-acceptance", operation, dict(parameters))


def dispatch(operation: str, **parameters: object) -> dict[str, object]:
    return operations.dispatch(request(operation, **parameters))


def excerpt_evidence(words: str) -> dict[str, object]:
    return {
        "source_type": "session_message",
        "locator": "session:step-c:1",
        "content_sha256": hashlib.sha256(words.encode("utf-8")).hexdigest(),
        "excerpt": words,
        "session_id": "step-c",
        "actor": ACTOR,
    }


def source(
    *,
    said_by: str,
    came_by: str,
    words: str | None,
    recorded_by: str = ACTOR,
    speaker: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {"said_by": said_by, "came_by": came_by, "recorded_by": recorded_by}
    if speaker is not None:
        value["speaker"] = speaker
    if words is not None:
        value["words"] = words
    return value


def owner_source(words: str) -> dict[str, object]:
    return source(said_by="owner", came_by="direct", words=words, recorded_by=ACTOR)


def agent_source(words: str, *, speaker: str = "chatgpt-web") -> dict[str, object]:
    return source(said_by="agent", came_by="agent_guess", words=words, recorded_by=ACTOR, speaker=speaker)


def item_mapping(
    *,
    title: str,
    state: str,
    data: dict[str, object],
    words: str,
    said_by: str,
    came_by: str,
    speaker: str | None = None,
) -> dict[str, object]:
    return {
        "schema": RECORD_SCHEMA_V2,
        "id": new_record_id(NOW, _entropy()),
        "kind": "item",
        "title": title,
        "state": state,
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
        "sensitivity": "private",
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [excerpt_evidence(words)],
        "supersedes": [],
        "dedupe_key": None,
        "data": data,
        "body": "",
        "source": source(said_by=said_by, came_by=came_by, words=words, recorded_by=ACTOR, speaker=speaker),
    }


@pytest.fixture()
def brain_home(tmp_path: Path) -> str:
    (tmp_path / "records").mkdir(parents=True)
    return str(tmp_path)


# ---------------------------------------------------------------------------
# PRD test 2, "kinds stay kinds"
# ---------------------------------------------------------------------------


def test_2_case_1_possible_item_is_an_idea_and_not_actionable(brain_home: str) -> None:
    created = dispatch(
        "record.create",
        brain_home=brain_home,
        actor=ACTOR,
        record=item_mapping(
            title="learn piano",
            state="open",
            data={"level": "possible", "size": "task"},
            words="maybe I should learn piano",
            said_by="owner",
            came_by="direct",
        ),
    )
    record_id = created["record"]["id"]

    ideas = dispatch("record.list", brain_home=brain_home, kind="item", word="idea")
    assert ideas["count"] == 1
    assert ideas["records"][0]["id"] == record_id

    actionable = dispatch("record.list", brain_home=brain_home, kind="item", actionable=True)
    assert actionable["count"] == 0


def test_2_case_2_and_3_accepted_needs_owner_words(brain_home: str) -> None:
    created = dispatch(
        "record.create",
        brain_home=brain_home,
        actor=ACTOR,
        record=item_mapping(
            title="learn piano",
            state="open",
            data={"level": "possible", "size": "task"},
            words="maybe I should learn piano",
            said_by="owner",
            came_by="direct",
        ),
    )
    record_id = created["record"]["id"]
    sha256 = created["receipt"]["content_sha256"]

    before = dispatch("record.list", brain_home=brain_home, current=False)

    # Case 2: an agent raising it to accepted is refused, and nothing is written.
    with pytest.raises(BrainError) as raised:
        dispatch(
            "record.transition",
            brain_home=brain_home,
            record_id=record_id,
            data={"level": "accepted"},
            source=agent_source("I bet Nic wants to learn piano"),
            actor=ACTOR,
            expected_sha256=sha256,
            evidence=excerpt_evidence("I bet Nic wants to learn piano"),
            now=NOW_ISO,
        )
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS

    after = dispatch("record.list", brain_home=brain_home, current=False)
    assert after == before

    # Case 3: an agent creating the same words directly at accepted is refused too.
    with pytest.raises(BrainError) as raised:
        dispatch(
            "record.create",
            brain_home=brain_home,
            actor=ACTOR,
            record=item_mapping(
                title="learn piano",
                state="open",
                data={"level": "accepted", "size": "task"},
                words="maybe I should learn piano",
                said_by="agent",
                came_by="agent_guess",
                speaker="chatgpt-web",
            ),
        )
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS


def test_connected_source_item_accepted_needs_owner_words_suggested_is_accepted(brain_home: str) -> None:
    """Decision 20260926-email-derived-items-enter-brain-only-as-hub-scre-e811822778e8: an
    email-derived item (``said_by: other``, ``came_by: connected_source``) is R1/R2's "not
    owner words" case, so creating it directly at ``level: accepted`` is refused
    NEEDS_OWNER_WORDS, the same as any other non-owner source. The same item at
    ``level: suggested`` - the level a hub screener may use - is accepted."""

    words = "can you send the deck by Friday?"

    with pytest.raises(BrainError) as raised:
        dispatch(
            "record.create",
            brain_home=brain_home,
            actor=ACTOR,
            record=item_mapping(
                title="send the deck",
                state="open",
                data={"level": "accepted", "size": "task"},
                words=words,
                said_by="other",
                came_by="connected_source",
                speaker="sam@example.com",
            ),
        )
    assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS

    created = dispatch(
        "record.create",
        brain_home=brain_home,
        actor=ACTOR,
        record=item_mapping(
            title="send the deck",
            state="open",
            data={"level": "suggested", "size": "task"},
            words=words,
            said_by="other",
            came_by="connected_source",
            speaker="sam@example.com",
        ),
    )
    assert created["record"]["data"]["level"] == "suggested"
    assert created["record"]["source"]["came_by"] == "connected_source"
    assert created["record"]["source"]["said_by"] == "other"


def test_2_case_4_accepted_promise_with_resolved_timing_is_actionable(brain_home: str) -> None:
    resolved = dispatch("time.resolve", words="Friday", now=NOW_ISO)
    assert resolved["resolved"] == "2026-10-02"

    words = "I'll pay the electricity bill Friday"
    created = dispatch(
        "record.create",
        brain_home=brain_home,
        actor=ACTOR,
        record=item_mapping(
            title="pay the electricity bill",
            state="open",
            data={
                "level": "accepted",
                "size": "task",
                "owed_to": "electricity company",
                "timing": {"due": resolved["resolved"], "resolved_from": {"words": "Friday", "today": resolved["today"]}},
            },
            words=words,
            said_by="owner",
            came_by="direct",
        ),
    )
    assert created["record"]["data"]["timing"]["due"] == "2026-10-02"

    promises = dispatch("record.list", brain_home=brain_home, kind="item", word="promise")
    assert promises["count"] == 1
    assert promises["records"][0]["id"] == created["record"]["id"]

    actionable = dispatch("record.list", brain_home=brain_home, kind="item", actionable=True)
    assert actionable["count"] == 1
    assert actionable["records"][0]["id"] == created["record"]["id"]


def test_2_case_5_invented_deadline_is_refused(brain_home: str) -> None:
    words = "I'll pay the electricity bill Friday"
    with pytest.raises(BrainError) as raised:
        dispatch(
            "record.create",
            brain_home=brain_home,
            actor=ACTOR,
            record=item_mapping(
                title="pay the electricity bill",
                state="open",
                data={
                    "level": "accepted",
                    "size": "task",
                    "owed_to": "electricity company",
                    "timing": {"due": "2026-10-02"},  # no resolved_from: not from the words
                },
                words=words,
                said_by="owner",
                came_by="direct",
            ),
        )
    assert raised.value.code is ErrorCode.TIME_NOT_FROM_WORDS


# ---------------------------------------------------------------------------
# PRD test 8, "limits", Brain Core half
# ---------------------------------------------------------------------------


def test_8_case_1_tiers() -> None:
    for action in ("read", "research", "draft", "link", "mark_waiting", "mark_blocked"):
        assert language.may_act(action) == "yes"
    for action in ("send", "spend", "delete", "publish", "security"):
        assert language.may_act(action) == "ask"
    assert language.may_act("do_something_unlisted") == "ask"


def test_8_case_2_and_3_agent_limits_and_stale_hash(brain_home: str) -> None:
    words = "I'll pay the electricity bill Friday"
    created = dispatch(
        "record.create",
        brain_home=brain_home,
        actor=ACTOR,
        record=item_mapping(
            title="pay the electricity bill",
            state="open",
            data={
                "level": "accepted",
                "size": "task",
                "owed_to": "electricity company",
                "timing": {"due": "2026-10-02", "resolved_from": {"words": "Friday", "today": "2026-09-26"}},
            },
            words=words,
            said_by="owner",
            came_by="direct",
        ),
    )
    record_id = created["record"]["id"]
    sha0 = created["receipt"]["content_sha256"]

    def transition(*, record_id: str, sha256: str, state: str | None = None, data: dict[str, object] | None = None, said_by: str, came_by: str = "agent_guess", recorded_by: str = ACTOR) -> dict[str, object]:
        return dispatch(
            "record.transition",
            brain_home=brain_home,
            record_id=record_id,
            state=state,
            data=data,
            source=source(said_by=said_by, came_by=came_by, words="agent update", recorded_by=recorded_by, speaker="chatgpt-web" if said_by == "agent" else None),
            actor=recorded_by,
            expected_sha256=sha256,
            evidence=excerpt_evidence("agent update"),
            now=NOW_ISO,
        )

    # An agent can mark it waiting, then blocked.
    waiting = transition(record_id=record_id, sha256=sha0, state="waiting", said_by="agent")
    record_id, sha1 = waiting["record"]["id"], waiting["receipt"]["content_sha256"]

    blocked = transition(record_id=record_id, sha256=sha1, state="blocked", said_by="agent")
    record_id, sha2 = blocked["record"]["id"], blocked["receipt"]["content_sha256"]

    # ...but not dropped, parked, or no_longer_needed.
    for state, extra_data in (
        ("dropped", {"reason": "no longer relevant"}),
        ("parked", {"parked_until": "2026-10-10"}),
        ("no_longer_needed", {}),
    ):
        with pytest.raises(BrainError) as raised:
            transition(record_id=record_id, sha256=sha2, state=state, data=extra_data, said_by="agent")
        assert raised.value.code is ErrorCode.NEEDS_OWNER_WORDS

    # ...and not done without a check by someone other than the claiming actor.
    with pytest.raises(BrainError) as raised:
        transition(record_id=record_id, sha256=sha2, state="done", data={"check": {"locator": "gt:request:1", "checked_by": ACTOR}}, said_by="agent")
    assert raised.value.code is ErrorCode.DONE_NEEDS_CHECK

    # Done, checked by someone else, succeeds.
    done = transition(record_id=record_id, sha256=sha2, state="done", data={"check": {"locator": "gt:request:1", "checked_by": OTHER_ACTOR}}, said_by="agent")
    sha3 = done["receipt"]["content_sha256"]
    assert done["record"]["state"] == "done"

    # Case 3: a change naming a stale SHA-256 (sha2, now superseded by the done record) is refused.
    with pytest.raises(BrainError) as raised:
        transition(record_id=record_id, sha256=sha2, state="waiting", said_by="owner", came_by="direct")
    assert raised.value.code is ErrorCode.RECORD_CHANGED

    # Two changes from the same read cannot both land: replay the waiting -> blocked read.
    with pytest.raises(BrainError) as raised:
        dispatch(
            "record.transition",
            brain_home=brain_home,
            record_id=waiting["record"]["id"],
            state="parked",
            data={"parked_until": "2026-10-10"},
            source=owner_source("park it"),
            actor=ACTOR,
            expected_sha256=sha1,  # sha1 was already spent to create the blocked record (sha2)
            evidence=excerpt_evidence("park it"),
            now=NOW_ISO,
        )
    assert raised.value.code is ErrorCode.RECORD_CHANGED


def test_8_case_4_no_delete_operation_exists() -> None:
    assert "record.delete" not in operations._HANDLERS
    assert not any("delete" in operation for operation in operations._HANDLERS)
