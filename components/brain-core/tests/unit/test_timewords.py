from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from brain_core.errors import BrainError, ErrorCode
from brain_core.timewords import find, find_one, from_words, resolve, says_more, resolve_date_and_time, today_in_singapore

SATURDAY = date(2026, 9, 26)  # matches the language spec's worked example
MONDAY = date(2026, 9, 28)
FRIDAY = date(2026, 10, 2)  # a day whose own weekday name rolls to the following week


def test_today_and_tomorrow() -> None:
    assert resolve("today", today=SATURDAY) == "2026-09-26"
    assert resolve("tomorrow", today=SATURDAY) == "2026-09-27"


def test_bare_weekday_is_the_next_such_day_after_today() -> None:
    assert resolve("Friday", today=SATURDAY) == "2026-10-02"
    assert resolve("friday", today=SATURDAY) == "2026-10-02"


def test_this_weekday_matches_bare_weekday() -> None:
    assert resolve("this Friday", today=SATURDAY) == resolve("Friday", today=SATURDAY)


def test_weekday_on_the_same_day_rolls_to_next_week() -> None:
    assert resolve("Saturday", today=SATURDAY) == "2026-10-03"


def test_weekday_on_a_monday() -> None:
    assert resolve("Friday", today=MONDAY) == "2026-10-02"


def test_next_weekday_is_the_following_monday_to_sunday_week() -> None:
    assert resolve("next Friday", today=MONDAY) == "2026-10-09"
    assert resolve("next Sunday", today=MONDAY) == "2026-10-11"


def test_iso_date() -> None:
    assert resolve("2026-10-02", today=SATURDAY) == "2026-10-02"


def test_iso_date_invalid_calendar_date_is_refused() -> None:
    with pytest.raises(BrainError) as raised:
        resolve("2026-02-30", today=SATURDAY)
    assert raised.value.code is ErrorCode.TIME_UNRESOLVED


def test_day_month_and_month_day() -> None:
    assert resolve("2 Oct", today=SATURDAY) == "2026-10-02"
    assert resolve("October 2", today=SATURDAY) == "2026-10-02"


def test_day_month_without_year_rolls_forward_when_in_the_past() -> None:
    assert resolve("1 Jan", today=SATURDAY) == "2027-01-01"


def test_day_month_invalid_calendar_date_is_refused() -> None:
    with pytest.raises(BrainError):
        resolve("31 Feb", today=SATURDAY)


def test_unknown_words_are_refused_not_guessed() -> None:
    with pytest.raises(BrainError) as raised:
        resolve("sometime soon", today=SATURDAY)
    assert raised.value.code is ErrorCode.TIME_UNRESOLVED
    assert raised.value.details["words"] == "sometime soon"


def test_unknown_month_name_is_refused() -> None:
    with pytest.raises(BrainError):
        resolve("2 Frobtember", today=SATURDAY)


def test_today_in_singapore_uses_the_fixed_offset() -> None:
    moment = datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC)  # 01:00 SGT on the 27th
    assert today_in_singapore(moment) == date(2026, 9, 27)


def test_today_in_singapore_requires_a_timezone() -> None:
    with pytest.raises(BrainError) as raised:
        today_in_singapore(datetime(2026, 9, 26, 17, 0, 0))
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_leading_preposition_is_stripped_before_matching() -> None:
    assert resolve("on Friday", today=SATURDAY) == resolve("Friday", today=SATURDAY)
    assert resolve("by Friday", today=SATURDAY) == resolve("Friday", today=SATURDAY)
    assert resolve("for Friday", today=SATURDAY) == resolve("Friday", today=SATURDAY)
    assert resolve("at Friday", today=SATURDAY) == resolve("Friday", today=SATURDAY)


def test_three_letter_weekday_matches_full_name() -> None:
    assert resolve("Fri", today=SATURDAY) == resolve("Friday", today=SATURDAY)
    assert resolve("fri", today=SATURDAY) == resolve("friday", today=SATURDAY)


# --- resolve_date_and_time: finding 6's phrases, each on a Friday and a Saturday -----

_FINDING_6_DATE_ONLY_PHRASES = (
    "Friday", "this Friday", "next Friday", "today", "tomorrow",
    "on Friday", "by Friday", "Fri",
)


@pytest.mark.parametrize("today", [SATURDAY, FRIDAY], ids=["saturday", "friday"])
@pytest.mark.parametrize("phrase", _FINDING_6_DATE_ONLY_PHRASES)
def test_finding_6_date_only_phrases_carry_no_time(phrase: str, today: date) -> None:
    resolved = resolve_date_and_time(phrase, today=today)
    assert resolved.due == resolve(phrase, today=today)
    assert resolved.at is None


@pytest.mark.parametrize("today", [SATURDAY, FRIDAY], ids=["saturday", "friday"])
def test_finding_6_tonight_resolves_to_today_with_no_time(today: date) -> None:
    resolved = resolve_date_and_time("tonight", today=today)
    assert resolved.due == today.isoformat()
    assert resolved.at is None


@pytest.mark.parametrize("today", [SATURDAY, FRIDAY], ids=["saturday", "friday"])
def test_finding_6_time_alone_resolves_to_today_with_that_time(today: date) -> None:
    resolved = resolve_date_and_time("7pm", today=today)
    assert resolved.due == today.isoformat()
    assert resolved.at == "19:00"


@pytest.mark.parametrize("phrase", ["this evening", "this morning", "this afternoon"])
def test_today_equivalent_phrases_resolve_to_today_with_no_time(phrase: str) -> None:
    resolved = resolve_date_and_time(phrase, today=SATURDAY)
    assert resolved.due == SATURDAY.isoformat()
    assert resolved.at is None


def test_tonight_at_7pm_resolves_both_due_today_and_the_time() -> None:
    resolved = resolve_date_and_time("tonight at 7pm", today=SATURDAY)
    assert resolved.due == SATURDAY.isoformat()
    assert resolved.at == "19:00"


@pytest.mark.parametrize("phrase", ["this evening at 7pm", "this morning at 7am"])
def test_today_equivalent_phrase_with_a_time_resolves_both(phrase: str) -> None:
    resolved = resolve_date_and_time(phrase, today=SATURDAY)
    assert resolved.due == SATURDAY.isoformat()
    assert resolved.at in ("19:00", "07:00")


@pytest.mark.parametrize(
    ("phrase", "expected_at"),
    [
        ("7pm", "19:00"),
        ("7 pm", "19:00"),
        ("7:30pm", "19:30"),
        ("19:00", "19:00"),
        ("at 7pm", "19:00"),
        ("7am", "07:00"),
        ("12am", "00:00"),
        ("12pm", "12:00"),
    ],
)
def test_recognized_time_forms(phrase: str, expected_at: str) -> None:
    assert resolve_date_and_time(phrase, today=SATURDAY).at == expected_at


@pytest.mark.parametrize(
    ("phrase", "expected_due", "expected_at"),
    [
        ("Friday 7pm", "2026-10-02", "19:00"),
        ("tomorrow at 19:00", "2026-09-27", "19:00"),
        ("next Friday at 9am", "2026-10-02", "09:00"),
    ],
)
def test_a_date_and_a_time_together_resolve_both(phrase: str, expected_due: str, expected_at: str) -> None:
    resolved = resolve_date_and_time(phrase, today=SATURDAY)
    assert resolved.due == expected_due
    assert resolved.at == expected_at


@pytest.mark.parametrize(
    "phrase",
    [
        "7", "at 7", "25:00", "13pm", "0pm",
        "7:30", "9:00", "12:30",  # a colon hour that could also be a 12-hour reading
    ],
)
def test_a_bare_or_ambiguous_hour_is_refused(phrase: str) -> None:
    with pytest.raises(BrainError) as raised:
        resolve_date_and_time(phrase, today=SATURDAY)
    assert raised.value.code is ErrorCode.TIME_UNRESOLVED


@pytest.mark.parametrize("phrase", ["00:00", "13:00", "23:59"])
def test_an_unambiguous_24_hour_colon_time_needs_no_meridiem(phrase: str) -> None:
    assert resolve_date_and_time(phrase, today=SATURDAY).at == phrase


def test_resolve_date_and_time_refuses_unknown_words_like_resolve_does() -> None:
    with pytest.raises(BrainError) as raised:
        resolve_date_and_time("sometime soon", today=SATURDAY)
    assert raised.value.code is ErrorCode.TIME_UNRESOLVED


def test_today_equivalent_phrase_with_an_unrecognized_trailing_time_is_refused() -> None:
    with pytest.raises(BrainError):
        resolve_date_and_time("tonight at some point", today=SATURDAY)


# --- find(): deterministic scan for time phrases inside free text -------------------


def test_find_returns_exactly_one_combined_phrase_for_the_provider_example() -> None:
    assert find("dinner with Sam tonight at 7pm") == ("tonight at 7pm",)


def test_find_returns_nothing_for_ordinary_text() -> None:
    assert find("just a normal sentence with no dates") == ()
    assert find("") == ()


def test_find_strips_trailing_sentence_punctuation() -> None:
    assert find("Remind me to buy printer ink on Friday.") == ("on Friday",)


def test_find_returns_several_distinct_phrases_when_more_than_one_is_present() -> None:
    found = find("call mum Friday 7pm and also next Tuesday at 9am")
    assert found == ("Friday 7pm", "next Tuesday at 9am")


def test_find_result_is_always_a_verbatim_substring_of_words() -> None:
    words = "let's talk Friday 7pm about it"
    for phrase in find(words):
        assert phrase in words


def test_find_is_deterministic() -> None:
    words = "ping me tonight at 7pm or tomorrow at 19:00"
    assert find(words) == find(words)


# --- find_one(): the single phrase a save may use without being told ---------------


def test_find_one_returns_the_single_sure_phrase() -> None:
    assert find_one("dinner with Sam tonight at 7pm") == "tonight at 7pm"
    assert find_one("Remind me to buy printer ink on Friday.") == "on Friday"


def test_find_one_never_reads_an_english_word_as_a_weekday() -> None:
    # "sun", "sat", "wed" and "mon" are words as well as weekday forms: nothing is guessed.
    assert find_one("Put the basil in the sun") is None
    assert find_one("I sat with Sam about the lease") is None
    assert find_one("Sat at 7pm") is None  # never falls back to "at 7pm" as today
    # An agent that names the phrase still gets it resolved.
    assert resolve("Sat", today=MONDAY) == "2026-10-03"


def test_find_one_returns_nothing_for_none_or_several_phrases() -> None:
    assert find_one("just a normal sentence with no dates") is None
    assert find_one("call mum Friday 7pm and also next Tuesday at 9am") is None


def test_find_one_joins_a_day_and_a_time_said_apart() -> None:
    # Nic, 29 Sep 2026: "tmr need to bring banner to printer at 12pm" is one moment.
    assert find_one("tmr need to bring banner to printer at 12pm") == "tmr at 12pm"
    assert find_one("Friday need to call the bank at 3pm") == "Friday at 3pm"
    # Two days, or two times, never join: nothing is guessed.
    assert find_one("move it from Friday to next Tuesday") is None
    assert find_one("either at 3pm or at 5pm") is None


def test_from_words_takes_time_words_as_one_piece_or_word_by_word() -> None:
    words = "tmr need to bring banner to printer at 12pm"
    assert from_words("at 12pm", words)
    assert from_words("tmr at 12pm", words)
    assert from_words("Tmr at 12PM.", words)
    assert from_words("Friday", "I'll pay the bill Friday.")
    assert not from_words("tomorrow at 12pm", words)
    assert not from_words("next Tuesday", "I'll pay the electricity bill Friday")
    assert not from_words("", words)
    assert not from_words("tmr", "")


def test_says_more_only_for_a_real_day_or_time_not_a_filler() -> None:
    assert says_more("tmr at 12pm", "at 12pm")
    assert not says_more("for next weeks", "next weeks")
    assert not says_more("tmr at 12pm", "tmr at 12pm")


# --- speed-03 (Proposed - Nic to confirm; docs/product/steps/c-record-format.md §5) ---------
# Every phrase here comes from Nic's own Diablo tests on Sat 26 Sep 2026 or is the plain
# neighbour of one: "I need to study for next weeks exam" (refused by speed-02), "Later 12am I
# got a call with Tommy" at 21:50 (refused, then saved as the midnight that had passed).

AT_2150 = time(21, 50)
SUNDAY = date(2026, 9, 27)


@pytest.mark.parametrize(
    ("phrase", "window"),
    [
        ("next week", ("2026-09-28", "2026-10-04")),
        ("next weeks", ("2026-09-28", "2026-10-04")),
        ("next week's", ("2026-09-28", "2026-10-04")),
        ("Next Week’s", ("2026-09-28", "2026-10-04")),  # a phone's curly apostrophe
        ("by next week", ("2026-09-28", "2026-10-04")),
        ("for next weeks", ("2026-09-28", "2026-10-04")),
        ("this week", ("2026-09-26", "2026-09-27")),  # from today, never the days gone
        ("later this week", ("2026-09-26", "2026-09-27")),
        ("this weekend", ("2026-09-26", "2026-09-27")),
        ("next weekend", ("2026-10-03", "2026-10-04")),
    ],
)
def test_week_and_weekend_phrases_are_a_window_with_no_due_date(phrase: str, window: tuple[str, str]) -> None:
    resolved = resolve_date_and_time(phrase, today=SATURDAY)
    assert resolved.window == window
    assert resolved.due is None and resolved.at is None


def test_week_windows_follow_the_monday_to_sunday_week_like_next_weekday_does() -> None:
    assert resolve_date_and_time("this week", today=MONDAY).window == ("2026-09-28", "2026-10-04")
    assert resolve_date_and_time("next week", today=MONDAY).window == ("2026-10-05", "2026-10-11")
    assert resolve_date_and_time("this weekend", today=MONDAY).window == ("2026-10-03", "2026-10-04")
    assert resolve_date_and_time("next weekend", today=MONDAY).window == ("2026-10-10", "2026-10-11")
    assert resolve_date_and_time("this week", today=SUNDAY).window == ("2026-09-27", "2026-09-27")
    assert resolve_date_and_time("this weekend", today=SUNDAY).window == ("2026-09-27", "2026-09-27")
    assert resolve_date_and_time("next week", today=SUNDAY).window == ("2026-09-28", "2026-10-04")


def test_the_date_only_resolver_still_refuses_a_window() -> None:
    with pytest.raises(BrainError) as raised:
        resolve("next week", today=SATURDAY)
    assert raised.value.code is ErrorCode.TIME_UNRESOLVED


def test_later_12am_said_at_2150_is_the_coming_midnight() -> None:
    resolved = resolve_date_and_time("Later 12am", today=SATURDAY, now=AT_2150)
    assert (resolved.due, resolved.at) == ("2026-09-27", "00:00")


@pytest.mark.parametrize(
    ("phrase", "due"),
    [
        ("12am", "2026-09-27"),
        ("7pm", "2026-09-27"),
        ("21:49", "2026-09-27"),
        ("21:50", "2026-09-26"),  # this very minute has not passed yet
        ("11pm", "2026-09-26"),
        ("at 11pm", "2026-09-26"),
        ("later at 11pm", "2026-09-26"),
    ],
)
def test_a_time_alone_that_has_passed_today_means_tomorrow(phrase: str, due: str) -> None:
    assert resolve_date_and_time(phrase, today=SATURDAY, now=AT_2150).due == due


def test_without_now_a_time_alone_is_still_today() -> None:
    assert resolve_date_and_time("12am", today=SATURDAY).due == "2026-09-26"


def test_a_named_day_is_never_moved_by_the_time_now() -> None:
    assert resolve_date_and_time("today at 7pm", today=SATURDAY, now=AT_2150).due == "2026-09-26"
    assert resolve_date_and_time("tonight at 7pm", today=SATURDAY, now=AT_2150).due == "2026-09-26"


@pytest.mark.parametrize(
    ("phrase", "due", "at"),
    [
        ("tonight at 12am", "2026-09-27", "00:00"),
        ("12am tonight", "2026-09-27", "00:00"),
        ("tonight 1am", "2026-09-27", "01:00"),
        ("tonight at 11pm", "2026-09-26", "23:00"),
        ("Friday night at 2am", "2026-10-03", "02:00"),
        ("tomorrow night 12am", "2026-09-28", "00:00"),
        ("tomorrow night at 6am", "2026-09-27", "06:00"),  # 06:00 is morning again
    ],
)
def test_the_small_hours_after_a_night_are_the_next_day(phrase: str, due: str, at: str) -> None:
    resolved = resolve_date_and_time(phrase, today=SATURDAY)
    assert (resolved.due, resolved.at) == (due, at)


@pytest.mark.parametrize(
    ("phrase", "due", "at"),
    [
        ("7pm tomorrow", "2026-09-27", "19:00"),
        ("7 pm tomorrow", "2026-09-27", "19:00"),
        ("at 9am on Friday", "2026-10-02", "09:00"),
        ("3pm next Tuesday", "2026-09-29", "15:00"),
        ("tomorrow morning", "2026-09-27", None),
        ("tomorrow morning at 9am", "2026-09-27", "09:00"),
        ("Friday evening", "2026-10-02", None),
        ("on Sunday afternoon", "2026-09-27", None),
        ("tmr", "2026-09-27", None),
        ("tmrw at 7pm", "2026-09-27", "19:00"),
        ("tonite", "2026-09-26", None),
        ("later today", "2026-09-26", None),
        ("later tonight", "2026-09-26", None),
    ],
)
def test_speed_03_date_and_time_forms(phrase: str, due: str, at: str | None) -> None:
    resolved = resolve_date_and_time(phrase, today=SATURDAY)
    assert (resolved.due, resolved.at, resolved.window) == (due, at, None)


@pytest.mark.parametrize(
    "phrase",
    [
        "later", "later 7", "next", "weekend", "next week at 7pm", "the week after next",
        "sometime next week", "7 tomorrow", "tomorrow 7", "tonight at some point",
    ],
)
def test_speed_03_still_refuses_what_it_cannot_read(phrase: str) -> None:
    with pytest.raises(BrainError) as raised:
        resolve_date_and_time(phrase, today=SATURDAY, now=AT_2150)
    assert raised.value.code is ErrorCode.TIME_UNRESOLVED


def test_find_picks_up_the_phrases_from_nics_26_sep_tests() -> None:
    assert find("I need to study for next weeks exam") == ("for next weeks",)
    assert find("Later 12am I got a call with Tommy") == ("Later 12am",)
    assert find("call mum 7pm tomorrow") == ("7pm tomorrow",)
    assert find_one("I need to study for next week’s exam") == "for next week’s"
    assert find_one("I'll do it later") is None
