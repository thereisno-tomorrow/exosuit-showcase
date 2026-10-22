"""Brain Home's read side for Diablo, as the Hermes memory provider ``brain``.

Before each of Nic's turns this reads his open tasks and suggestions and his newest confirmed
preferences (``memory.list``) and the open "where I stopped" notes (``continuity.list``)
through the Gremlin Town hub, and hands Hermes one short block that starts with the Singapore
date and time. Recall (29 Sep 2026) then searches Brain Home (``memory.search``) with the real words
of Nic's message and adds up to three records that truly match, so what he said before reaches
Diablo without Diablo deciding to look; a message with no such words, or no match, adds nothing.
It never saves a turn. Its only writes are one-line markers on the Brain Home
note this chat was already keeping: ``compressed`` when Hermes shortens the chat, and ``idle``
after 30 minutes with no turn. A marker the hub could not take is kept in
``$HERMES_HOME/brain-outbox.jsonl`` and sent again after the next read that works.

A read that fails or is skipped never passes unlogged: the model gets one line saying
Brain Home was not reachable, and ``$HERMES_HOME/brain-prefetch.log`` gets one JSON line.

No model is called here, and nothing here decides whether to read: that is the
``prefetch`` setting. Spec: docs/product/steps/g-damien-hooks.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .hub_client import HubClientConfig, HubClientError, HubHTTPSClient


PROVIDER_NAME = "brain"
SETTINGS_FILE = Path(__file__).with_name("settings.json")  # hub URL and token-file path; no secret
OPTIONS_FILE_NAME = "brain.json"  # in $HERMES_HOME, written by save_config
PREFETCH_LOG_NAME = "brain-prefetch.log"  # in $HERMES_HOME: one JSON line per failed or skipped read
OUTBOX_NAME = "brain-outbox.jsonl"  # in $HERMES_HOME: markers a transport error kept from the hub (follow-on P5)

CACHE_SECONDS = 60.0
TIMEOUT_SECONDS = 5.0
FAILURES_BEFORE_PAUSE = 3
PAUSE_SECONDS = 300.0
BLOCK_TOKEN_BUDGET = 250
ITEM_LIMIT = 10  # accepted items shown
SUGGESTION_LIMIT = 5  # suggestions shown after them (speed-03)
NOTE_LIMIT = 3
PREFERENCE_LIMIT = 3  # Nic's newest confirmed preferences shown (follow-on P1)
# Recall (29 Sep 2026, Nic: "do it"): records matching the words of Nic's message, after the block.
RECALL_LIMIT = 3  # records shown
RECALL_SEARCH_LIMIT = 8  # asked of the hub; the filter below keeps the few that really match
RECALL_TERMS = 8  # most words searched for
RECALL_TOKEN_BUDGET = 90  # its own room, so a match never pushes an open item out of the block
RECALL_MIN_SECONDS = 0.5  # what must be left of the turn's 5 s to start the search
RECALL_BODY_CHARACTERS = 60
RECALL_STRONG_CHARACTERS = 5  # one title word this long (or a number) is a match on its own
IDLE_SECONDS = 1800.0  # follow-on P2: a quiet spell is 30 minutes with no turn
IDLE_CHECK_SECONDS = 60.0  # how often the idle watcher looks

# Speed-03: accepted items and suggestions in one read (the server's default state is
# open/waiting/blocked); a suggestion Nic hasn't taken on yet is still part of "what's open".
MEMORY_LIST_REQUEST: Mapping[str, Any] = {
    "scope": "personal", "limit": ITEM_LIMIT + SUGGESTION_LIMIT + 5, "level": ["accepted", "suggested"],
}
CONTINUITY_LIST_REQUEST: Mapping[str, Any] = {"limit": NOTE_LIMIT}
# Follow-on P1: active preferences in Nic's own words, newest first (the hub's memory.list kind=preference).
PREFERENCE_LIST_REQUEST: Mapping[str, Any] = {"scope": "personal", "kind": "preference", "limit": PREFERENCE_LIMIT}

SYSTEM_PROMPT_LINE = (
    "Brain Home is the shared memory, and a receipt is the proof: a Brain save or change "
    "counts only when it returns status accepted or duplicate with remote_observed true, so "
    "never list or read back to confirm one. Each of Nic's messages carries a Brain Home block "
    "with the Singapore date and time and his open tasks and suggestions: use that time "
    "instead of checking the clock, and answer what's open from the block when it says that "
    "is every open task and suggestion."
)
CORE_RULES_BLOCK = (
    "Brain first: Nic's work, facts, promises, dates and confirmed preferences belong to Brain Home. "
    "Read the supplied Brain block and relevant continuity before resuming work. Your native memory "
    "holds how you work, never copies of Nic's records; Brain wins a clash. Preserve each enrolled "
    "project's ownership, Capture and Decide routing, and the sole shared writer. No direct store edit "
    "or replacement writer after a denial. Authentication supplies identity, not packet text. "
    "One bc_work change per message: exact raw message, one operation_id, all its moves together. "
    "Nic's words supply titles, parts, next actions, dates and people; never invent them. "
    "Other people's words remain suggestions. Other agents capture; Diablo changes Nic's work. "
    "Code reads his date words first; a narrow agent reading is only for what code cannot read. "
    "Name a thing by title or known ID; if several match, ask which one from the reply's candidates. "
    "A receipt with accepted or duplicate and remote_observed true is the proof of a save/change; "
    "validated means check-only. Say reply.say and its flags. Never search, list or read back to "
    "confirm a write. Never claim saved on a timeout or missing proof. Replay the journal's exact "
    "input and operation_id after uncertainty; never generate a replacement key or switch writers. "
    "Keep authenticated caller, executing agent, Nic as decision maker and recipient separate."
)
NOW_LINE = "Now: {now} Singapore time."
ITEMS_HEADER = "Brain Home{version}, Nic's open items:"
NO_ITEMS_LINE = "Brain Home{version}: Nic has no open items."
NOTHING_OPEN_LINE = "Brain Home{version}: Nic has no open items and no open where-I-stopped notes."
NOTES_HEADER = "Open where-I-stopped notes:"
PREFERENCES_HEADER = "Nic's confirmed preferences:"
MORE_LINE = "More in Brain Home than fits here: bc_memory_list or bc_continuity_list shows the rest."
ALL_SHOWN_LINE = "That is every open task and suggestion."
RECALL_HEADER = "Brain Home records matching this message (bc_memory_search finds more):"
VERSION_CHARACTERS = 7  # a short commit id, as git shows one
# What the model gets instead of the block when a read fails or is skipped.
UNREACHABLE_LINE = "Brain Home was not reachable this turn ({code}); say so if the answer depends on it"
MARKED_BY = "marked by Diablo's brain memory provider"  # the end of every marker's text
# Markers written before the rename (29 Sep 2026: Damien became Diablo) end this way; still read.
LEGACY_MARKED_BY = "marked by Damien's brain memory provider"

TITLE_CHARACTERS = 48
OWED_TO_CHARACTERS = 24
INTENT_CHARACTERS = 40
NEXT_STEP_CHARACTERS = 60
PREFERENCE_CHARACTERS = 60

# P2: dates and times in records are Singapore time, which has no daylight saving.
SINGAPORE = timezone(timedelta(hours=8), "Asia/Singapore")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Brain Core's lexical rule (brain.lexical/v1, brain_core.retrieval.tokenize): each run of
# word characters is one token, and so is every other character that isn't a space.
_LEXICAL_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)

_NOTE_TOOLS = frozenset({"bc_continuity_start", "bc_continuity_append", "bc_continuity_recover"})
_CLOSE_TOOL = "bc_continuity_close"
_NO_OPEN_NOTE = frozenset({"brain.continuity.empty", "brain.record.transition"})
# Follow-on P5: the failures after which the same marker may still land later; any other is a refusal.
_TRANSPORT_CODES = frozenset({"UNAVAILABLE", "TIMEOUT"})
_MARKER_ID = re.compile(r"hermes-(compressed|idle)-[0-9a-f]{24}")

# A code shown to the model or logged must be a short code; anything else is UNKNOWN,
# so no text from a reply reaches the model through the "not reachable" line.
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# Created 0600 (the umask can only narrow it); every line is one O_APPEND write.
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
_REWRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
# One Hermes process writes $HERMES_HOME; these keep its providers (one per chat) in step.
_OUTBOX_LOCK = threading.Lock()  # every read and change of the outbox file
_REPLAYING = threading.Lock()  # one replay at a time

# Words that say nothing about which record Nic means: English function words, question and
# chat words, Singlish particles, and task words every record could share. Only what is left is
# searched, so a message made only of these searches nothing.
_RECALL_STOPWORDS = frozenset("""
a about above after again all also am an and any anything are as at be because been before being
below between both but by can cannot could did do does doing done down during each else ever every
few for from further get gets getting go goes going gone got had has have having he her here hers
him his how i if in into is it its itself just know let like me mine more most much must my myself
need needs no nor not now of off on once only or other our ours out over own please pls same she
should so some something still such tell than thank thanks that the their theirs them then there
these they thing things this those through to too under until up us very want wants was we were
what whats when where which while who whom why will with would yes yet you your yours
ok okay ya yah yeah nah hey hi hello lah leh lor meh sia hor ah eh oh wah abit
today tomorrow tonight yesterday later soon again next step steps open item items task tasks
remind reminder show list check help make makes see say said think thought one two three four five
ask asked asking due mark marked left new old last first mean means meaning explain
monday tuesday wednesday thursday friday saturday sunday mon tue wed thu fri sat sun
january february march april may june july august september october november december
""".split())
_RECALL_WORD = re.compile(r"\w+", re.UNICODE)

SETTING_KEYS = ("prefetch", "preferences", "idle_marker", "recall", "core_rules")
CORE_RULES_FIELD = {"key": "core_rules", "description": "Trial: fixed Brain ownership and receipt rules",
                    "default": "off", "choices": ["on", "off"]}
PREFETCH_FIELD: Mapping[str, Any] = {
    "key": "prefetch",
    "description": "Read Nic's open items and open where-I-stopped notes before each turn",
    "default": "on",
    "choices": ["on", "off"],
}
PREFERENCES_FIELD: Mapping[str, Any] = {
    "key": "preferences",
    "description": "Show Nic's three newest confirmed preferences in the block before each turn",
    "default": "on",
    "choices": ["on", "off"],
}
IDLE_MARKER_FIELD: Mapping[str, Any] = {
    "key": "idle_marker",
    "description": "Mark this chat's open where-I-stopped note after 30 minutes with no turn",
    "default": "on",
    "choices": ["on", "off"],
}
RECALL_FIELD: Mapping[str, Any] = {
    "key": "recall",
    "description": "Also show up to three Brain Home records matching the words of Nic's message",
    "default": "on",
    "choices": ["on", "off"],
}

log = logging.getLogger(__name__)

# A hub read or write: (operation, request, seconds allowed) -> the hub's JSON reply.
Hub = Callable[[str, Mapping[str, Any], float], Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _spawn_daemon(target: Callable[[], None], *, name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def lexical_tokens(text: str) -> int:
    """Tokens by Brain Core's lexical rule; the block's size is measured with this."""

    return len(_LEXICAL_TOKEN.findall(text))


def code_of(exc: BaseException) -> str:
    """The short code a failure is shown and logged with: the hub's code, else the error's type."""

    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = type(exc).__name__
    return code if _SAFE_CODE.fullmatch(code) else "UNKNOWN"


def _transport_error(exc: BaseException) -> bool:
    """The hub client's word that the same write may still land: the hub or GitHub was
    unreachable or too slow, or said to try again (429, 5xx). Only these are kept (follow-on P5)."""

    return isinstance(exc, HubClientError) and (exc.retryable is True or exc.code in _TRANSPORT_CODES)


def _append_line(path: Path, entry: Mapping[str, Any]) -> None:
    """One JSON line in one O_APPEND write, to a file created 0600."""

    descriptor = os.open(path, _LOG_FLAGS, 0o600)
    try:
        os.write(descriptor, (json.dumps(entry) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


# -- Settings -----------------------------------------------------------------------------


class SettingsError(ValueError):
    """A bad brain.json, or a bad value given to save_config."""

    code = "CONFIGURATION_INVALID"


@dataclass(frozen=True, slots=True)
class Options:
    prefetch: bool = True
    preferences: bool = True  # follow-on P1: the block's confirmed preferences
    idle_marker: bool = True  # follow-on P2: the quiet-spell marker
    recall: bool = True  # the records matching Nic's message
    core_rules: bool = False


def checked_options(values: Any, *, source: str) -> dict[str, str]:
    """The settings given, each checked, or a loud error naming what is wrong."""

    if not isinstance(values, Mapping):
        raise SettingsError(f"{source} must be a JSON object")
    unknown = sorted(str(key) for key in set(values) - set(SETTING_KEYS))
    if unknown:
        raise SettingsError(f"{source} has unsupported settings {unknown}; only {list(SETTING_KEYS)} are supported")
    checked: dict[str, str] = {}
    for key in SETTING_KEYS:
        if key in values:
            if values[key] not in ("on", "off"):
                raise SettingsError(f"{source}: {key} must be 'on' or 'off'")
            checked[key] = values[key]
    return checked


def load_options(hermes_home: Any) -> Options:
    """``$HERMES_HOME/brain.json``; existing settings default on; core_rules defaults off."""

    if not hermes_home:
        return Options()
    path = Path(hermes_home) / OPTIONS_FILE_NAME
    if not path.is_file():
        return Options()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"{path} is unreadable or not JSON") from exc
    checked = checked_options(raw, source=str(path))
    return Options(**{key: checked.get(key, "off" if key == "core_rules" else "on") == "on" for key in SETTING_KEYS})


def protected_configuration(settings_file: Path = SETTINGS_FILE) -> HubClientConfig:
    """The hub endpoint and Diablo's existing bearer, from local files and environment only.

    Mirrors the gremlin-town plugin's rule: a protected token source must be named, in the
    environment or in this folder's settings.json, so the shared Grok token path is never used.
    """

    named = bool(
        os.environ.get("GREMLIN_TOWN_HUB_TOKEN", "").strip()
        or os.environ.get("GREMLIN_TOWN_HUB_TOKEN_FILE", "").strip()
    )
    if not named and settings_file.is_file():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HubClientError("CONFIGURATION_INVALID", "brain provider settings unreadable or invalid") from exc
        named = isinstance(settings, dict) and bool(settings.get("token_file"))
    if not named:
        raise HubClientError("CONFIGURATION_MISSING", "a protected hub token source is required")
    return HubClientConfig.from_environment(settings_path=settings_file)


def https_hub(config: HubClientConfig, *, requester: Any = None) -> Hub:
    """Every call through the existing hub client, with the time the caller has left.

    ``requester`` is the hub client's own transport hook, for tests; production uses its default.
    """

    def call(operation: str, request: Mapping[str, Any], seconds: float) -> Any:
        client = HubHTTPSClient(HubClientConfig(config.base_url, config.token, timeout=seconds), client=requester)
        return client.call(operation, dict(request))

    return call


# -- The block ------------------------------------------------------------------------------


class _BadReply(ValueError):
    """A hub reply not in the agreed shape."""

    code = "MALFORMED_RESPONSE"


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _short(text: str, limit: int) -> str:
    text = _collapse(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:") + "…"


def _text(entry: Mapping[str, Any], key: str, *, required: bool = False) -> str:
    value = entry.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise _BadReply(f"{key} must be text")
    return _collapse(value)


def _entries(reply: Mapping[str, Any], operation: str) -> list[Mapping[str, Any]]:
    items = reply.get("items")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise _BadReply(f"{operation} reply has no items list")
    return items


def _day(value: date, today: date) -> str:
    shown = f"{_WEEKDAYS[value.weekday()]} {value.day} {_MONTHS[value.month - 1]}"
    return shown if value.year == today.year else f"{shown} {value.year}"


def _shown_day(value: Any, today: date) -> str:
    text = value.strip() if isinstance(value, str) else ""
    try:
        return _day(date.fromisoformat(text), today)
    except ValueError:
        return _short(text, 16)


def _when(timing: Any, today: date) -> str:
    if timing is None:
        return ""
    if not isinstance(timing, Mapping):
        raise _BadReply("timing must be an object")
    due, at = timing.get("due"), timing.get("at")
    due = due.strip() if isinstance(due, str) else ""
    at = at.strip()[:5] if isinstance(at, str) else ""  # the agreed "HH:MM"
    if due:
        shown = _shown_day(due, today)
        return f"due {shown} {at}" if at else f"due {shown}"
    window = timing.get("window")
    if isinstance(window, Mapping) and window.get("start") and window.get("end"):
        # "this/next week(end)" (speed-03): a window, no due date.
        return f"{_shown_day(window['start'], today)} to {_shown_day(window['end'], today)}"
    return f"at {at}" if at else ""


def _item_line(item: Mapping[str, Any], today: date) -> str:
    """Render W13's supplied ReaderNow; never recompute work from raw legacy fields."""
    current = item.get("now")
    if not isinstance(current, Mapping):
        raise _BadReply("memory.list item lacks the current reader's now projection")
    next_item = current.get("next")
    next_text = next_item.get("text") if isinstance(next_item, Mapping) else next_item
    if not isinstance(next_text, str) or not next_text.strip():
        raise _BadReply("reader now.next must be non-blank text or a ReaderNext with non-blank text")
    # W13's compact default list omits title only when the full exact title is
    # already supplied verbatim by next. No body/parent/ID reconstruction.
    display_name = item["title"] if "title" in item else next_text
    if not isinstance(display_name, str) or not display_name.strip():
        raise _BadReply("title must be text")
    title = _short(display_name, TITLE_CHARACTERS)
    label = _text(current, "is", required=True)
    owed_to = _text(item, "owed_to")
    if owed_to:
        label = f"promise to {_short(owed_to, OWED_TO_CHARACTERS)}"
    if item.get("level") == "suggested":
        label = f"suggestion ({label})"
    details = []
    progress = current.get("progress")
    progress_text = _text(progress, "text", required=True) if isinstance(progress, Mapping) else progress
    if not isinstance(progress_text, str) or not progress_text.strip():
        raise _BadReply("reader now.progress must be non-blank text or a ReaderProgress with text")
    details.append(progress_text)
    if next_text != display_name:
        details.append("next: " + _short(next_text, NEXT_STEP_CHARACTERS))
    dates = current.get("dates")
    if not isinstance(dates, Mapping):
        raise _BadReply("reader now.dates must be an object")
    for kind, value in dates.items():
        if not isinstance(value, Mapping):
            raise _BadReply("reader date must be an object")
        if kind == "planned_period":
            shown = f"{_shown_day(value.get('start'), today)} to {_shown_day(value.get('end'), today)}"
        elif value.get("day"):
            shown = _shown_day(value["day"], today)
        else:
            shown = _text(value, "event")
        if not shown:
            continue
        name = {"deadline": "due", "appointment": "at", "planned_day": "planned",
                "planned_period": "planned", "not_before": "not before", "expected": "expected",
                "start_by": "start by", "suggested": "suggested"}.get(kind, kind)
        at = _text(value, "at")
        event = _text(value, "event")
        details.append(f"{name} {shown}" + (f" {at}" if at else "") +
                       (f" ({event})" if event and event != shown else ""))
    status = _text(current, "status", required=True)
    if status not in {"up", "open"}:
        details.append(status)
    return f"- {label}: {title} ({', '.join(details)})" if details else f"- {label}: {title}"


def _note_line(note: Mapping[str, Any]) -> str:
    session = _text(note, "session_id", required=True)
    intent = _short(_text(note, "intent"), INTENT_CHARACTERS)
    step = _short(_text(note, "next_step"), NEXT_STEP_CHARACTERS)
    body = "; ".join(part for part in (intent, f"next: {step}" if step else "") if part)
    return f"- {session}: {body}" if body else f"- {session}"


def _preference_line(preference: Mapping[str, Any]) -> str:
    return f"- {_short(_text(preference, 'title', required=True), PREFERENCE_CHARACTERS)}"


def _fit(lines: Iterable[str], room: int) -> tuple[list[str], int]:
    kept: list[str] = []
    for line in lines:
        cost = lexical_tokens(line)
        if cost > room:
            break
        kept.append(line)
        room -= cost
    return kept, room


def _section(header: str, lines: list[str]) -> list[str]:
    return [header, *lines] if lines else []


def _version(listing: Mapping[str, Any]) -> str:
    """The header's version part, " at <short commit>", naming the Brain Home it read; "" if none."""

    version = listing.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{7,64}", version):
        return ""
    return f" at {version[:VERSION_CHARACTERS]}"


def _now_line(now: datetime) -> str:
    moment = now.astimezone(SINGAPORE)
    day = f"{_WEEKDAYS[moment.weekday()]} {moment.day} {_MONTHS[moment.month - 1]} {moment.year}"
    return NOW_LINE.format(now=f"{day}, {moment:%H:%M}")


def render_block(
    listing: Mapping[str, Any],
    notes: Mapping[str, Any],
    *,
    today: date,
    now: datetime | None = None,
    preferences: Mapping[str, Any] | None = None,
) -> str:
    """One block of at most BLOCK_TOKEN_BUDGET lexical tokens: notes get room first, then
    preferences, then items.

    Speed-03: it opens with the Singapore date and time when ``now`` is given, lists
    accepted items before suggestions, and ends the items with a line saying either that
    Brain Home has more or that this is everything. Follow-on P1: when ``preferences`` (the hub's
    ``memory.list`` reply for kind preference) is given, up to three of Nic's confirmed
    preferences follow that line, before the notes. They don't change it: it speaks of
    tasks and suggestions only.
    """

    all_entries = _entries(listing, "memory.list")
    for entry in all_entries:
        if not isinstance(entry.get("now"), Mapping):
            raise _BadReply("memory.list item lacks the current reader's now projection")
    # W13 returns lasting first IDs. Contained things are folded into their parent's
    # reader progress/next; no raw-parent lookup or shadow reader here.
    entries = [entry for entry in all_entries if entry["now"].get("under") is None]
    accepted = [item for item in entries if item.get("level") != "suggested"][:ITEM_LIMIT]
    suggested = [item for item in entries if item.get("level") == "suggested"][:SUGGESTION_LIMIT]
    item_lines = [_item_line(item, today) for item in (*accepted, *suggested)]
    note_lines = [_note_line(note) for note in _entries(notes, "continuity.list")[:NOTE_LIMIT]]
    preference_lines = ([] if preferences is None else
                        [_preference_line(entry) for entry in _entries(preferences, "memory.list")[:PREFERENCE_LIMIT]])
    version = _version(listing)
    top = [_now_line(now)] if now is not None else []
    if not item_lines and not note_lines:
        head = NOTHING_OPEN_LINE.format(version=version)
        room = BLOCK_TOKEN_BUDGET - sum(map(lexical_tokens, top)) - lexical_tokens(head)
        shown_preferences, _ = _fit(preference_lines, room - lexical_tokens(PREFERENCES_HEADER))
        return "\n".join([*top, head, *_section(PREFERENCES_HEADER, shown_preferences)])
    head = (ITEMS_HEADER if item_lines else NO_ITEMS_LINE).format(version=version)
    ending = max(lexical_tokens(MORE_LINE), lexical_tokens(ALL_SHOWN_LINE))
    room = BLOCK_TOKEN_BUDGET - sum(map(lexical_tokens, top)) - lexical_tokens(head) - ending
    shown_notes: list[str] = []
    if note_lines:
        shown_notes, room = _fit(note_lines, room - lexical_tokens(NOTES_HEADER))
    shown_preferences = []
    if preference_lines:
        shown_preferences, room = _fit(preference_lines, room - lexical_tokens(PREFERENCES_HEADER))
    shown_items, _ = _fit(item_lines, room)
    hidden = len(entries) - len(shown_items) + len(note_lines) - len(shown_notes)
    lines = [*top, head, *shown_items]
    if hidden or listing.get("truncated") is True:
        lines.append(MORE_LINE)
    elif item_lines:
        lines.append(ALL_SHOWN_LINE)
    lines += _section(PREFERENCES_HEADER, shown_preferences)
    lines += _section(NOTES_HEADER, shown_notes)
    return "\n".join(lines)


# -- Recall: records matching Nic's message ---------------------------------------------------


def recall_terms(message: str) -> tuple[str, ...]:
    """The words of ``message`` worth searching for, in order, at most RECALL_TERMS: no stopwords,
    nothing shorter than 3 characters unless it is a number. Empty means search nothing."""

    terms: list[str] = []
    for word in _RECALL_WORD.findall(message.casefold()):
        if word in _RECALL_STOPWORDS or word in terms or word.isdigit() and len(word) < 3:
            continue
        if len(word) < 3 and not word.isdigit():
            continue
        terms.append(word)
    return tuple(terms[:RECALL_TERMS])


def _words(text: str) -> set[str]:
    return set(_RECALL_WORD.findall(text.casefold()))


def _strong(term: str) -> bool:
    return term.isdigit() or len(term) >= RECALL_STRONG_CHARACTERS


def recall_lines(reply: Mapping[str, Any], terms: tuple[str, ...], *, skip: frozenset[str] = frozenset()) -> list[str]:
    """Lines for the records in a ``memory.search`` reply that really match ``terms``, best first.

    The hub matches any shared letters, so this keeps a record only when two of the terms are
    whole words in its title or body, or one long word or number is a whole word in its title.
    Records already in the block (``skip``, by record ID) are left out.
    """

    matches = reply.get("matches")
    if not isinstance(matches, list):
        raise _BadReply("memory.search reply has no matches list")
    ranked: list[tuple[int, int, int, str]] = []
    for position, match in enumerate(matches):
        if (not isinstance(match, Mapping) or _text(match, "record_id") in skip
                or match.get("level") == "possible" or match.get("word") == "idea"):
            continue
        title = _text(match, "title", required=True)
        body = _text(match, "body")
        in_title = [term for term in terms if term in _words(title)]
        anywhere = [term for term in terms if term in _words(title) or term in _words(body)]
        if len(anywhere) < 2 and not any(_strong(term) for term in in_title):
            continue
        label = _text(match, "word") or _text(match, "kind") or "record"
        state = _text(match, "state")
        line = f"- {label}: {_short(title, TITLE_CHARACTERS)}"
        if state and state not in ("open", "active", "current"):
            line += f" ({state})"
        snippet = _short(body, RECALL_BODY_CHARACTERS)
        if snippet and _collapse(body).casefold() != _collapse(title).casefold():
            line += f": {snippet}"
        ranked.append((-len(in_title), -len(anywhere), position, line))
    ranked.sort()
    return [line for *_, line in ranked[:RECALL_LIMIT]]


def render_recall(lines: list[str]) -> str:
    """The recall section within RECALL_TOKEN_BUDGET lexical tokens; "" when nothing matched."""

    shown, _ = _fit(lines, RECALL_TOKEN_BUDGET - lexical_tokens(RECALL_HEADER))
    return "\n".join([RECALL_HEADER, *shown]) if shown else ""


# -- The note this chat is keeping ------------------------------------------------------------


def _function(call: Any) -> Mapping[str, Any]:
    if not isinstance(call, Mapping):
        return {}
    function = call.get("function")
    return function if isinstance(function, Mapping) else call


def _arguments(function: Mapping[str, Any]) -> Mapping[str, Any]:
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = None
    return arguments if isinstance(arguments, Mapping) else {}


def note_session_in(messages: Any, current: str | None = None) -> str | None:
    """The Brain Home note Diablo's own continuity calls in these messages last pointed to.

    ``current`` is the note this chat was keeping before these messages; the calls carry on
    from it. A later ``bc_continuity_close`` of that note clears it. Notes with a
    ``project_id`` or a scope other than personal belong to their project, not Brain Home,
    and are skipped. Only continuity tool calls are read: no words from the chat.
    """

    for message in messages if isinstance(messages, (list, tuple)) else ():
        if not isinstance(message, Mapping):
            continue
        calls = message.get("tool_calls")
        for call in calls if isinstance(calls, (list, tuple)) else ():
            function = _function(call)
            name = function.get("name")
            arguments = _arguments(function)
            if name == "gremlin_town_more_tools":
                if arguments.get("action") != "call" or not isinstance(arguments.get("arguments"), Mapping):
                    continue
                name, arguments = arguments.get("name"), arguments["arguments"]
            if not isinstance(name, str) or (name not in _NOTE_TOOLS and name != _CLOSE_TOOL):
                continue
            session = arguments.get("session_id")
            if not isinstance(session, str) or not session:
                continue
            if arguments.get("project_id") or arguments.get("scope") not in (None, "personal"):
                continue
            if name == _CLOSE_TOOL:
                if session == current:
                    current = None
            else:
                current = session
    return current


def _outbox_request(line: bytes) -> dict[str, str] | None:
    """The ``continuity.append`` an outbox line holds, or None if it isn't a marker (follow-on P5)."""

    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, Mapping):
        return None
    operation_id, note, value = entry.get("operation_id"), entry.get("note"), entry.get("value")
    if not (isinstance(operation_id, str) and _MARKER_ID.fullmatch(operation_id)
            and isinstance(note, str) and note and entry.get("event") == "effect"
            and isinstance(value, str) and value.endswith((MARKED_BY, LEGACY_MARKED_BY))):
        return None
    return {"operation_id": operation_id, "session_id": note, "event": "effect", "value": value}


def _marker_kind(request: Mapping[str, str]) -> str:
    matched = _MARKER_ID.fullmatch(request["operation_id"])
    return matched.group(1) if matched else "marker"


class _TooSlow(Exception):
    code = "TIMEOUT"


# -- The provider ------------------------------------------------------------------------------


class BrainProvider:
    """Everything the ``brain`` memory provider does; ``__init__.py`` adds Hermes's base class."""

    # Version 1, best effort: never a checkpoint of the transcript (S8), never in compression's way.
    pre_compress_checkpoint_api_version = 1

    def __init__(
        self,
        *,
        hub: Hub | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
        spawn: Callable[..., None] = _spawn_daemon,
        settings_file: Path = SETTINGS_FILE,
    ) -> None:
        super().__init__()
        self._injected_hub = hub
        self._hub: Hub | None = None
        self._clock = clock
        self._now = now
        self._spawn = spawn
        self._settings_file = settings_file
        self._options = Options()
        self._session_id = ""
        self._note_session: str | None = None
        self._unavailable = ""
        self._setup_code = "NOT_INITIALIZED"  # why there is no hub, until initialize succeeds
        self._log_file: Path | None = None
        self._outbox_file: Path | None = None
        self._state = threading.Lock()
        self._one_read = threading.Lock()
        self._cached: tuple[str, float] | None = None
        self._listed: frozenset[str] = frozenset()  # record IDs of the items in the last block read
        self._failures = 0
        self._last_code = ""
        self._paused_until = 0.0
        self._outage_logged = False
        self._log_broken = False
        # Follow-on P2, the quiet spell: when the last turn was (on ``clock``, and in UTC for the marker's
        # text), whether this spell's marker has had its one try, and whether a watcher is on it.
        self._last_turn = 0.0
        self._last_turn_at: datetime | None = None
        self._idle_marked = False
        self._watching = False
        self._stop = threading.Event()

    # Identity and set-up ---------------------------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        """Configured and credentialed, from local files and environment only; no network."""

        if self._injected_hub is not None:
            return True
        try:
            protected_configuration(self._settings_file)
        except HubClientError as exc:
            self._unavailable = f"{exc.code}: {exc}"
            return False
        self._unavailable = ""
        return True

    def unavailable_reason(self) -> str:
        return self._unavailable

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Once at agent start: read the settings and resolve the hub. No network.

        A bad setting or configuration raises (Hermes logs it). Every turn after that gets the
        "not reachable" line with the set-up code, and a line in the prefetch log.
        """

        self._session_id = session_id or ""
        self._note_session = None
        home = kwargs.get("hermes_home")
        self._log_file = Path(home) / PREFETCH_LOG_NAME if home else None
        self._outbox_file = Path(home) / OUTBOX_NAME if home else None
        self._hub = None
        with self._state:
            self._last_turn_at = None
            self._idle_marked = False
        try:
            self._options = load_options(home)
            self._hub = self._injected_hub or https_hub(protected_configuration(self._settings_file))
        except Exception as exc:
            self._setup_code = code_of(exc)
            raise
        if not self._options.prefetch:
            log.info("Brain prefetch is off by setting (%s); Diablo gets no Brain Home block before his turns",
                     Path(home) / OPTIONS_FILE_NAME)

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        del kwargs
        self._session_id = new_session_id or ""
        self._note_session = None
        with self._state:
            self._last_turn_at = None  # a new chat: no quiet spell until its first turn
            self._idle_marked = False

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [dict(PREFETCH_FIELD), dict(PREFERENCES_FIELD), dict(IDLE_MARKER_FIELD), dict(RECALL_FIELD), dict(CORE_RULES_FIELD)]

    def save_config(self, values: Mapping[str, Any], hermes_home: str) -> None:
        checked = checked_options(values, source="brain settings")
        path = Path(hermes_home) / OPTIONS_FILE_NAME
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(checked, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    # Tools: none. Diablo's Brain tools stay in the gremlin-town plugin. ----------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def handle_tool_call(self, tool_name: str, args: Mapping[str, Any], **kwargs: Any) -> str:
        del args, kwargs
        return json.dumps(
            {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": f"the brain provider has no tool {tool_name}"}}
        )

    # Read side -------------------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._hub is None:
            return ""
        return SYSTEM_PROMPT_LINE + ("\n" + CORE_RULES_BLOCK if self._options.core_rules else "")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Nic's open items, preferences and notes as one block, cached 60 s; on failure, one line saying so.

        Then, if ``recall`` is on and the block was read, up to three records matching the words of
        Nic's message (``query``), all within the same 5 s. No match, or a search that fails, adds nothing.
        """

        del session_id
        deadline = self._clock() + TIMEOUT_SECONDS
        self._turn()
        block = self._block(use_cache=True, when="before_turn")
        if not block or block.startswith(UNREACHABLE_LINE.split("(")[0]) or not self._options.recall:
            return block
        recall = self._recall(query, deadline)
        return f"{block}\n{recall}" if recall else block

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """After a turn, on Hermes's background worker: read again for the next turn."""

        del query, session_id
        self._turn()
        self._block(use_cache=False, when="after_turn")

    def _block(self, *, use_cache: bool, when: str) -> str:
        if not self._options.prefetch:
            return ""  # off by setting: no read is meant to happen (said once, at start-up)
        if self._hub is None:
            return self._skipped(self._setup_code, when)
        deadline = self._clock() + TIMEOUT_SECONDS
        if use_cache and (cached := self._fresh()) is not None:
            return cached
        if (last := self._pause_code()) is not None:
            return self._skipped(last, when, paused=True)
        if not self._one_read.acquire(timeout=max(0.0, deadline - self._clock())):
            # An earlier read is still running past this turn's 5 s; it logs its own outcome.
            return self._skipped("TIMEOUT", when)
        try:
            if use_cache and (cached := self._fresh()) is not None:
                return cached  # the read we waited for has just filled the cache
            if (last := self._pause_code()) is not None:
                return self._skipped(last, when, paused=True)
            try:
                block = self._read_block(deadline)
            except Exception as exc:  # the model is told, never shown an error; the breaker counts it
                return self._failed(code_of(exc), when)
            self._succeeded(block)
        finally:
            self._one_read.release()
        self._replay_outbox_soon()  # follow-on P5: the hub answered, so what the outbox holds can go now
        return block

    def _read_block(self, deadline: float) -> str:
        """The three reads within one 5 s deadline, then the block (follow-on P1 adds the third)."""

        listing = self._read("memory.list", MEMORY_LIST_REQUEST, deadline)
        listed = frozenset(_text(entry, "record_id") for entry in _entries(listing, "memory.list"))
        with self._state:
            self._listed = listed - {""}  # recall leaves these out: the block shows them already
        notes = self._read("continuity.list", CONTINUITY_LIST_REQUEST, deadline)
        preferences = (self._read("memory.list", PREFERENCE_LIST_REQUEST, deadline)
                       if self._options.preferences else None)
        moment = self._now().astimezone(SINGAPORE)
        return render_block(listing, notes, today=moment.date(), now=moment, preferences=preferences)

    def _read(self, operation: str, request: Mapping[str, Any], deadline: float) -> Mapping[str, Any]:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _TooSlow(operation)
        reply = self._hub(operation, dict(request), remaining)  # type: ignore[misc]
        if self._clock() > deadline:
            raise _TooSlow(operation)
        if not isinstance(reply, Mapping):
            raise _BadReply(f"{operation} reply is not an object")
        return reply

    def _recall(self, query: str, deadline: float) -> str:
        """The recall section for one message, or "". Searches only when the message has words worth
        searching and at least RECALL_MIN_SECONDS of the turn's 5 s are left. A failure is logged
        ("recall" in the prefetch log) and adds nothing; it never counts toward the breaker."""

        terms = recall_terms(query or "")
        if not terms:
            return ""
        remaining = deadline - self._clock()
        if remaining < RECALL_MIN_SECONDS:
            self._record("skipped", "TIMEOUT", "recall")
            return ""
        request = {"scope": "personal", "query": " ".join(terms), "limit": RECALL_SEARCH_LIMIT}
        try:
            reply = self._read("memory.search", request, deadline)
            with self._state:
                skip = self._listed
            return render_recall(recall_lines(reply, terms, skip=skip))
        except Exception as exc:  # the model gets the block without recall, never an error
            self._record("failed", code_of(exc), "recall")
            return ""

    def _fresh(self) -> str | None:
        with self._state:
            if self._cached is None:
                return None
            block, read_at = self._cached
            return block if self._clock() - read_at < CACHE_SECONDS else None

    def _pause_code(self) -> str | None:
        """While the breaker is open, the code of the failure that opened it; otherwise None."""

        with self._state:
            return (self._last_code or "UNKNOWN") if self._clock() < self._paused_until else None

    def _skipped(self, code: str, when: str, *, paused: bool = False) -> str:
        self._record("skipped", code, when)
        return UNREACHABLE_LINE.format(code=f"paused after {code}" if paused else code)

    def _failed(self, code: str, when: str) -> str:
        with self._state:
            self._failures += 1
            self._last_code = code
            failures = self._failures
            if failures >= FAILURES_BEFORE_PAUSE:
                self._paused_until = self._clock() + PAUSE_SECONDS
            first_warning = failures >= FAILURES_BEFORE_PAUSE and not self._outage_logged
            if first_warning:
                self._outage_logged = True
        log.debug("Brain prefetch failed (%s), %d in a row", code, failures)
        if first_warning:
            log.warning(
                "Brain prefetch paused for 5 minutes after %d failures in a row (last: %s); "
                "Diablo replies without the Brain Home block until a read works again",
                failures, code,
            )
        self._record("failed", code, when)
        return UNREACHABLE_LINE.format(code=code)

    def _succeeded(self, block: str) -> None:
        with self._state:
            recovered = self._outage_logged
            self._failures = 0
            self._last_code = ""
            self._paused_until = 0.0
            self._outage_logged = False
            self._cached = (block, self._clock())
        if recovered:
            log.info("Brain prefetch resumed")

    def _record(self, reason: str, code: str, when: str) -> None:
        """One JSON line in $HERMES_HOME/brain-prefetch.log; a write failure never breaks the turn."""

        entry = {
            "time": self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": reason,
            "code": code,
            "breaker": "closed" if self._pause_code() is None else "open",  # once this read is counted
            "when": when,
        }
        try:
            if self._log_file is None:
                raise FileNotFoundError("Hermes gave no hermes_home to hold " + PREFETCH_LOG_NAME)
            _append_line(self._log_file, entry)
        except Exception as exc:
            with self._state:
                first = not self._log_broken
                self._log_broken = True
            if first:
                log.warning("Brain prefetch log %s could not be written (%s); failed and skipped reads are "
                            "not recorded there until a write works again",
                            self._log_file or PREFETCH_LOG_NAME, type(exc).__name__)
            return
        with self._state:
            self._log_broken = False

    # The two markers ---------------------------------------------------------------------------

    def on_pre_compress(self, messages: Any, **kwargs: Any) -> str:
        """Mark this chat's open Brain Home note ``compressed``, in the background.

        Never adds to the summary, never blocks or fails compression.
        """

        del kwargs
        if self._hub is None:
            return ""
        self._note_session = session = note_session_in(messages, self._note_session)
        if session is None:
            return ""
        if self._pause_code() is not None:
            log.debug("Brain note %s not marked compressed: prefetch is paused", session)
            return ""
        request = self._marker("compressed", session, self._now())
        self._spawn(lambda: self._send_marker(request), name="brain-mark-compressed")
        return ""

    def _turn(self) -> None:
        """A turn began or ended: the quiet spell starts again, and a watcher looks after it (follow-on P2)."""

        with self._state:
            self._last_turn = self._clock()
            self._last_turn_at = self._now()
            self._idle_marked = False
            start = (self._options.idle_marker and self._hub is not None
                     and not self._watching and not self._stop.is_set())
            if start:
                self._watching = True
        if not start:
            return
        try:
            self._spawn(self._watch_idle, name="brain-idle-watch")
        except Exception as exc:  # the turn goes on; this spell just gets no marker
            with self._state:
                self._watching = False
            log.warning("Brain idle watcher could not start (%s); no idle marker this time", type(exc).__name__)

    def _watch_idle(self) -> None:
        """The idle watcher, on its own background thread: a look a minute until the spell ends."""

        try:
            while not self._stop.wait(IDLE_CHECK_SECONDS) and self.check_idle():
                pass
        except Exception as exc:  # a background thread: log it, never raise into Hermes
            with self._state:
                self._watching = False
            log.warning("Brain idle watcher stopped (%s); the next turn starts another", type(exc).__name__)

    def check_idle(self) -> bool:
        """One look at this chat's quiet spell; True while the watcher should keep looking (follow-on P2).

        After IDLE_SECONDS with no turn, one ``idle:`` marker goes to this chat's open Brain
        Home note, once per quiet spell: never without a note, and not while the breaker is
        open. The watcher calls this once a minute; the tests call it with a fake clock.
        """

        with self._state:
            if (not self._options.idle_marker or self._hub is None
                    or self._last_turn_at is None or self._idle_marked):
                self._watching = False
                return False
            if self._clock() - self._last_turn < IDLE_SECONDS:
                return True
            self._idle_marked = True  # this spell's one try, whatever happens next
            self._watching = False
            since = self._last_turn_at
        session = self._note_session
        if session is None:
            return False  # no note, no write: the provider never starts one
        if self._pause_code() is not None:
            log.debug("Brain note %s not marked idle: prefetch is paused", session)
            return False
        self._send_marker(self._marker("idle", session, since))
        return False

    def _marker(self, kind: str, session: str, moment: datetime) -> dict[str, str]:
        """A marker's ``continuity.append``, replay-safe: its id comes from the Hermes session,
        the note and the minute its text names, so a replay is the same request (§4)."""

        stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        digest = hashlib.sha256(f"{self._session_id}\n{session}\n{stamp}".encode("utf-8")).hexdigest()[:24]
        what = (f"compressed: Hermes shortened this chat at {stamp}" if kind == "compressed"
                else f"idle: no turn since {stamp}")
        return {
            "operation_id": f"hermes-{kind}-{digest}",
            "session_id": session,
            "event": "effect",
            "value": f"{what}; {MARKED_BY}",
        }

    def _send_marker(self, request: Mapping[str, str]) -> None:
        """One marker on a background thread; never raises into Hermes.

        A closed or missing note is a quiet refusal. A transport error keeps the marker in the
        outbox for after the next read that works (follow-on P5); any other failure is one warning.
        """

        session, kind = request["session_id"], _marker_kind(request)
        try:
            self._hub("continuity.append", dict(request), TIMEOUT_SECONDS)  # type: ignore[misc]
        except Exception as exc:  # a background thread: log it, never raise into Hermes
            code = code_of(exc)
            if isinstance(exc, HubClientError) and exc.code in _NO_OPEN_NOTE:
                log.debug("No open Brain Home note %s to mark %s (%s)", session, kind, code)
            elif _transport_error(exc) and self._queue(request):
                log.warning("Brain note %s not marked %s (%s); kept in %s to send after the next read that works",
                            session, kind, code, OUTBOX_NAME)
            else:
                log.warning("Brain note %s not marked %s (%s)", session, kind, code)
            return
        log.info("Brain note %s marked %s", session, kind)
        self._replay_outbox_soon()  # the hub took a write, so it can take the queued ones too

    # The outbox (follow-on P5) ---------------------------------------------------------------------

    def _queue(self, request: Mapping[str, str]) -> bool:
        """Keep one marker in $HERMES_HOME/brain-outbox.jsonl; False if it could not be kept."""

        entry = {
            "time": self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "operation_id": request["operation_id"],
            "session": self._session_id,
            "note": request["session_id"],
            "event": request["event"],
            "value": request["value"],
        }
        try:
            if self._outbox_file is None:
                raise FileNotFoundError("Hermes gave no hermes_home to hold " + OUTBOX_NAME)
            with _OUTBOX_LOCK:
                _append_line(self._outbox_file, entry)
        except Exception as exc:
            log.warning("Brain outbox %s could not be written (%s)", self._outbox_file or OUTBOX_NAME,
                        type(exc).__name__)
            return False
        return True

    def _replay_outbox_soon(self) -> None:
        """If the outbox holds anything, send it on a background thread, one replay at a time."""

        path = self._outbox_file
        if path is None or self._hub is None or not path.exists():
            return
        if not _REPLAYING.acquire(blocking=False):
            return  # a replay is running; it sends what is there
        try:
            self._spawn(self._replay_then_release, name="brain-outbox-replay")
        except Exception as exc:  # the read that worked still counts; the outbox waits
            _REPLAYING.release()
            log.warning("Brain outbox replay could not start (%s)", type(exc).__name__)

    def _replay_then_release(self) -> None:
        try:
            self._replay_outbox()
        except Exception as exc:  # a background thread: log it, never raise into Hermes
            log.warning("Brain outbox replay stopped (%s); what is left goes after the next read that works",
                        type(exc).__name__)
        finally:
            _REPLAYING.release()

    def _replay_outbox(self) -> None:
        """Send the outbox's markers in order.

        A reply (accepted, or duplicate for one that had landed after all) removes its line; a
        refusal removes it with a log line; a transport error, or anything unexpected, stops
        the replay and keeps that line and the rest for the next read that works.
        """

        path = self._outbox_file
        if path is None:
            return
        try:
            with _OUTBOX_LOCK:
                lines = path.read_bytes().splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Brain outbox %s could not be read (%s)", path, type(exc).__name__)
            return
        handled = 0
        for line in lines:
            request = _outbox_request(line)
            if request is None:
                log.warning("Brain outbox: dropped a line that is not one of this provider's markers")
            else:
                note, kind = request["session_id"], _marker_kind(request)
                try:
                    self._hub("continuity.append", dict(request), TIMEOUT_SECONDS)  # type: ignore[misc]
                except Exception as exc:
                    code = code_of(exc)
                    if _transport_error(exc) or not isinstance(exc, HubClientError):
                        log.warning("Brain outbox: note %s not marked %s yet (%s); %d kept for the next read "
                                    "that works", note, kind, code, len(lines) - handled)
                        break
                    if exc.code in _NO_OPEN_NOTE:
                        log.info("Brain outbox: dropped the %s marker for note %s, which is closed or gone (%s)",
                                 kind, note, code)
                    else:
                        log.warning("Brain outbox: dropped the %s marker for note %s; the hub refused it (%s)",
                                    kind, note, code)
                else:
                    log.info("Brain note %s marked %s, from the outbox", note, kind)
            handled += 1
        if handled:
            self._drop_from_outbox(lines[:handled])

    def _drop_from_outbox(self, done: list[bytes]) -> None:
        """Remove the lines a replay handled; a file with nothing left goes away."""

        path = self._outbox_file
        if path is None:
            return
        with _OUTBOX_LOCK:
            try:
                current = path.read_bytes().splitlines(keepends=True)
                if [line.rstrip(b"\r\n") for line in current[: len(done)]] != done:
                    log.warning("Brain outbox %s changed while it was being sent; left as it is", path)
                    return
                rest = current[len(done):]
                if not rest:
                    path.unlink()
                    return
                temporary = path.with_name(path.name + ".tmp")
                with os.fdopen(os.open(temporary, _REWRITE_FLAGS, 0o600), "wb") as handle:
                    handle.writelines(rest)
                os.replace(temporary, path)
            except OSError as exc:
                log.warning("Brain outbox %s could not be rewritten (%s); its sent markers go again later, "
                            "which their ids make safe", path, type(exc).__name__)

    # Write side: deliberately nothing (S8: no automatic capture; one writer) --------------------

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs: Any) -> None:
        """After each turn. Saves nothing (S8): it follows which Brain Home note Diablo's own
        continuity calls point to, as on_pre_compress does, and the quiet spell starts again."""

        del user_content, assistant_content
        self._note_session = note_session_in(kwargs.get("messages"), self._note_session)
        self._turn()

    def on_session_end(self, messages: Any) -> None:
        del messages

    def on_memory_write(self, action: str, target: str, content: str, metadata: Any = None) -> None:
        del action, target, content, metadata

    def shutdown(self) -> None:
        """Process exit: the idle watcher stops. Nothing else to close."""

        self._stop.set()
