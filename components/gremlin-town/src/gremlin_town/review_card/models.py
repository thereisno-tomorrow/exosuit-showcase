"""Plain card state and transport results; no record ownership lives here."""
from dataclasses import dataclass
from datetime import datetime, timedelta, time

from brain_core.timewords import SINGAPORE


class CardError(ValueError):
    """A safe, owner-facing failure, without provider exception details."""


class CardRenderError(CardError):
    """Presentation failed; this does not establish a writer failure."""


def review_day(now, zone=SINGAPORE):
    """Local calendar date; the six-am courier is a separate delivery event."""
    return now.astimezone(zone).date()


def expires(day):
    return datetime.combine(day + timedelta(days=1), time(0), SINGAPORE)


@dataclass(frozen=True)
class CardResult:
    message: dict
    day: str
    revision: int
    modal: dict | None = None
    changed: bool = False


def initial_state():
    return dict(screen="open", item=None, skipped=[], picks={}, history=[], win="", error="", page=0,
                mode="planned", not_yet=False, added=[], settling=False, revision=0)


def location(state):
    return {key: state.get(key) for key in ("screen", "item", "mode", "not_yet", "page", "stage", "length_ref", "manual_ref", "review_ref", "review_title")}


def screen_key(state):
    picker = state.get('selector', {}) if state.get('screen') == 'selector' else {}
    return "|".join(str(state.get(key, "")) for key in ("screen", "item", "mode", "not_yet", "page", "stage")) + '|' + str(picker.get('field_index', '')) + '|' + str(picker.get('offset', ''))
