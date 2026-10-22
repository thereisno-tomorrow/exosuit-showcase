"""W23b: completion days come from owner words or the completing version."""

from dataclasses import replace
from datetime import date, datetime, timedelta, UTC

import pytest

from brain_core import work
from brain_core.review import ItemRecord, build_morning
from brain_core.weekly import weekly_review
from brain_core.orientation import overview, SECTION_FINISHED_THIS_WEEK, SECTION_IN_PROGRESS
from test_moves import answer, item, run, NOW
from test_orientation import inputs, review_inputs


def entries(*records):
    return tuple(ItemRecord(record=r, path=f"records/item/{r.id}.md") for r in records)


@pytest.mark.parametrize("phrase, day", [
    ("yesterday", "2026-09-29"), ("on monday", "2026-09-28"),
    ("last friday", "2026-09-25"), ("29 September", "2026-09-29"),
])
def test_tick_reads_past_days_before_agents_reading(phrase, day):
    old = item("send invoice")
    message = f"I did send invoice {phrase}"
    out = run([old], message, [{"move": "tick", "thing": old.id, "what": "thing",
        "words": message, "when": phrase, "reading": {"day": "2026-10-10"}}])
    assert out.refusal is None
    done = out.records[0]
    assert done.data["dates"]["done"] == dict(words=phrase, read_by="code", read_on="2026-09-30", day=day)
    assert work.done_day_of(done, work.index_things([old, done])).isoformat() == day
    assert out.reply["things"][0]["done_day"] == day
    from brain_core.presentation import relative_day_words
    from brain_core.timewords import personal_today
    assert out.reply["say"].count("done " + relative_day_words(day, personal_today(NOW))) == 1


def test_no_when_stores_the_4am_day_and_carries_it_after_history_is_archived():
    old = item("send invoice")
    at = datetime(2026, 9, 29, 19, tzinfo=UTC)  # 03:00 SGT on 30 Sep
    out = run([old], "I did send invoice", [{"move": "tick", "thing": old.id,
        "words": "I did send invoice"}], now=at)
    assert out.refusal is None
    done = out.records[0]
    fact = dict(words="I did send invoice", read_by="code", read_on="2026-09-29", day="2026-09-29")
    assert done.data["dates"]["done"] == fact
    edited_out = run([done], "put send invoice under admin", [{"move": "organise", "thing": old.id,
        "label": "admin"}], now=NOW + timedelta(days=1), operation="edit-done")
    assert edited_out.refusal is None
    edited = edited_out.records[0]
    assert edited.data["dates"]["done"] == fact
    # Neither the original nor the completing version remains in the snapshot.
    index = work.index_things([edited])
    assert work.done_day_of(old.id, index) == date(2026, 9, 29)


def test_older_done_records_without_a_date_keep_the_completing_version_fallback():
    old = item("send invoice")
    done = replace(old, id="brn_20260930_030000_0000000000000008", state="done",
        updated_at=datetime(2026, 9, 29, 19, tzinfo=UTC), supersedes=(old.id,),
        data={**old.data, "check": {"locator": "message", "checked_by": "nic"}})
    edited = replace(done, id="brn_20261001_060000_0000000000000009", updated_at=NOW + timedelta(days=1),
                     supersedes=(*done.supersedes, done.id))
    assert work.done_day_of(old.id, work.index_things([old, done, edited])) == date(2026, 9, 29)


def test_no_when_review_tick_uses_his_reply_not_the_question_for_date_words():
    old = item("send invoice")
    line = answer(old, "send invoice: done?", ["done"])
    out = run([old], "yes", [{"move": "tick", "thing": old.id, "words": "not his reply"}], answers=[line])
    assert out.refusal is None
    assert out.records[0].source["words"] == "yes"
    assert out.records[0].data["dates"]["done"] == dict(
        words="yes", read_by="code", read_on="2026-09-30", day="2026-09-30")


@pytest.mark.parametrize("quote", ["I did send invoice", "I did send invoice " + "with details " * 30])
def test_no_when_date_words_come_from_this_ticks_quote_within_the_date_word_limit(quote):
    old = item("send invoice")
    out = run([old], "Other message words. " + quote, [{"move": "tick", "thing": old.id,
        "what": "thing", "words": quote}])
    assert out.refusal is None
    assert out.records[0].data["dates"]["done"]["words"] == quote[:256]
    assert out.records[0].source["words"] == quote


@pytest.mark.parametrize("target", ["routine", "next_action", "dropped", "no_longer_needed"])
def test_no_when_only_whole_thing_ticks_store_a_done_date(target):
    old = item("send invoice", data={"repeat": "daily"} if target == "routine" else
               {"next_action": {"text": "open invoice", "done": False}})
    move = {"move": "tick", "thing": old.id, "words": "I did send invoice"}
    if target == "next_action":
        move["what"] = target
    elif target in {"dropped", "no_longer_needed"}:
        move.update(move="end", ending=target)
    out = run([old], "I did send invoice", [move])
    assert out.refusal is None
    assert "done" not in out.records[0].data.get("dates", {})


@pytest.mark.parametrize("phrase, reading", [("tomorrow", None), ("some special day", {"day": "2026-10-01"})])
def test_future_done_day_refuses_the_whole_change(phrase, reading):
    old = item("send invoice")
    message = f"I did send invoice {phrase}"
    move = {"move": "tick", "thing": old.id, "words": message, "when": phrase}
    if reading:
        move["reading"] = reading
    out = run([old], message, [move])
    assert not out.records and out.refusal["fix"] == "that day hasn't happened yet"


def test_phrase_absent_drops_only_the_date_with_its_question():
    old = item("send invoice")
    out = run([old], "I did send invoice", [{"move": "tick", "thing": old.id,
        "words": "I did send invoice", "when": "yesterday"}])
    assert out.refusal is None and out.records[0].state == "done"
    assert "done" not in out.records[0].data.get("dates", {})
    assert out.flags[0]["dropped"] and "phrase not in his words" in out.flags[0]["message"]
    assert any(m["mark"] == "question" for m in out.records[0].data["review_marks"])


def test_a_dropped_tick_date_question_reaches_the_done_records_review():
    old = item("send invoice")
    out = run([old], "I did send invoice", [{"move": "tick", "thing": old.id,
        "words": "I did send invoice", "when": "yesterday"}])
    assert out.refusal is None
    done = out.records[0]
    question = done.data["review_marks"][0]["text"]
    needs = work.needs_nic(done, work.index_things([done]), NOW, with_answer_kind=True)
    assert len(needs) == 1
    assert needs[0]["text"] == question and needs[0]["answer_kind"] == "question"
    assert needs[0]["allowed_answers"] == work.allowed_answers("question")
    morning = build_morning(review_inputs(done, now=NOW))
    line = next(line for line in morning.parts[2].lines if line.item_ref == old.id)
    assert "tick: date left off: phrase not in your words?" in line.text
    assert line.allowed_answers == needs[0]["allowed_answers"]


def test_a_dropped_tick_date_question_reaches_the_reply_say():
    old = item("send invoice")
    out = run([old], "I did send invoice", [{"move": "tick", "thing": old.id,
        "words": "I did send invoice", "when": "yesterday"}])
    assert out.refusal is None
    assert "send invoice: date change was not saved: date left off: phrase not in your words." in out.reply["say"]


def test_done_questions_and_agent_date_confirmation_are_both_returned():
    old = item("send invoice", state="done", data={
        "check": {"locator": "message", "checked_by": "nic"},
        "dates": {"done": dict(words="the day we met Sam", read_by="hermes-wa",
                               read_on="2026-09-30", day="2026-09-25")},
        "review_marks": [{"mark": "question", "text": "When did you do it?"},
                         {"mark": "ask_stake", "text": "Stale stake question"}]})
    needs = work.needs_nic(old, work.index_things([old]), NOW, with_answer_kind=True)
    assert [(need["text"], need["answer_kind"]) for need in needs] == [
        ("When did you do it?", "question"), ("Keep done: the day we met Sam?", "agents_wording")]


def test_agent_past_reading_is_labelled_and_cannot_replace_code():
    old = item("send invoice")
    message = "I did send invoice on the day we met Sam"
    out = run([old], message, [{"move": "tick", "thing": old.id, "words": message,
        "when": "on the day we met Sam", "reading": {"day": "2026-09-25"}}])
    assert out.refusal is None
    assert out.records[0].data["dates"]["done"]["read_by"] == "hermes-wa"
    assert "I read 'on the day we met Sam' as Fri 25 Sep" in out.reply["say"]
    done = out.records[0]
    index = work.index_things([old, done])
    assert work.needs_nic(done, index, NOW)[0]["text"] == "Keep done: on the day we met Sam?"
    morning = build_morning(review_inputs(old, done, now=NOW))
    line = next(line for line in morning.parts[2].lines if line.item_ref == old.id)
    assert "on the day we met Sam" in line.text and line.rule == "needs_you" and line.allowed_answers
    kept = run([old, done], "Yes, keep the date for send invoice", [{"move": "organise", "thing": old.id,
        "keep": "date", "words": "Yes, keep the date for send invoice"}], operation="keep-done-date")
    assert kept.refusal is None
    kept_index = work.index_things([old, done, *kept.records])
    assert not work.needs_nic(old.id, kept_index, NOW)


@pytest.mark.parametrize("target", ["routine", "next_action", "end"])
def test_when_is_only_for_whole_thing_ticks(target):
    old = item("send invoice", data={"repeat": "daily"} if target == "routine" else
               {"next_action": {"text": "open invoice", "done": False}})
    move = {"move": "end" if target == "end" else "tick", "thing": old.id,
            "words": "I did send invoice yesterday", "when": "yesterday"}
    if target == "next_action":
        move["what"] = "next_action"
    if target == "end":
        move["ending"] = "no_longer_needed"
    out = run([old], "I did send invoice yesterday", [move])
    assert out.refusal and not out.records


def test_late_report_appears_in_the_right_week_and_orientation():
    old = item("send invoice")
    out = run([old], "I did send invoice yesterday", [{"move": "tick", "thing": old.id,
        "words": "I did send invoice yesterday", "when": "yesterday"}], now=NOW + timedelta(days=6))
    assert out.refusal is None
    done = out.records[0]
    before_report = NOW + timedelta(days=5)
    packet = weekly_review(entries(old, done), before_report)
    assert [line.text for line in packet.lines if line.part == "finished"] == ["send invoice — done today"]
    later = NOW + timedelta(days=12)
    assert not [line for line in weekly_review(entries(old, done), later).lines if line.part == "finished"]
    picture = overview(inputs(done, now=before_report), before_report)
    assert any(line.text == "send invoice — done today" for line in picture.lines if line.section == SECTION_FINISHED_THIS_WEEK)


def test_latest_ticked_part_is_chosen_by_done_day_and_shown_in_progress():
    parent = item("prepare invoice", data={"started_at": NOW.isoformat()})
    a = item("open invoice", number=2, state="done", data={"parent": parent.id, "order": 1,
        "check": {"locator": "message", "checked_by": "nic"},
        "dates": {"done": dict(words="yesterday", read_by="code", read_on="2026-09-30", day="2026-09-29")}})
    b = item("find address", number=3, state="done", data={"parent": parent.id, "order": 2,
        "check": {"locator": "message", "checked_by": "nic"},
        "dates": {"done": dict(words="last friday", read_by="code", read_on="2026-09-30", day="2026-09-25")}})
    index = work.index_things([parent, a, b])
    assert work.progress_of(parent, index)["last_ticked"] == a.id
    picture = overview(inputs(parent, a, b, now=NOW), NOW)
    line = next(line for line in picture.lines if line.section == SECTION_IN_PROGRESS)
    assert line.fields_used["progress"]["last_ticked_day"] == "2026-09-29"
