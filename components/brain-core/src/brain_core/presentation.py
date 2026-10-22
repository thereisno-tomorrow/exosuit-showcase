"""Shared words for user-facing work text; stored values keep their wire format."""

from datetime import date, timedelta


WORK_WORDS = {
    "up": "can do now",
    "needs_you": "For your review",
    "orient": "All my work",
}


def work_words(word: str, *, label: bool = False) -> str:
    """Display a work label; callers retain internal names in records and logs."""
    text = WORK_WORDS.get(word, word)
    return text[:1].upper() + text[1:] if label else text


STAKE_WORDS = {
    "nothing_much": "nothing much",
    "nuisance": "a nuisance",
    "hurts_weeks": "hurts for weeks",
    "changes_life": "changes my life",
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FULL_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def stake_words(level: str) -> str:
    """Render a stake level without changing the code used by records and answers."""
    return STAKE_WORDS.get(level, level)


def day_words(day: date | str) -> str:
    """The review's fixed, locale-independent day format, e.g. Thu 8 Oct."""
    parsed = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{_WEEKDAYS[parsed.weekday()]} {parsed.day} {_MONTHS[parsed.month - 1]}"


def relative_day_words(day: date | str, today: date | None = None) -> str:
    """Words relative to the supplied personal day; no clock or stored-text changes."""
    parsed = date.fromisoformat(day) if isinstance(day, str) else day
    if today is not None:
        offset = (parsed - today).days
        if offset in (-1, 0, 1):
            return {-1: "yesterday", 0: "today", 1: "tomorrow"}[offset]
        sunday = today + timedelta(days=6 - today.weekday())
        if today < parsed <= sunday:
            return f"this {_FULL_WEEKDAYS[parsed.weekday()]}, {parsed.day} {_MONTHS[parsed.month - 1]}"
    return day_words(parsed)
