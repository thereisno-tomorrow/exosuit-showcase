"""Right-moment return: what comes back during the day, and only at a moment Nic can act.

`returnable` decides, for one signal, which already-collected items should come back. GT
collects only the records named in today's picks and waiting items before calling this - not
the whole store. No model call, no IO. See docs/product/steps/e-review-and-return.md §4.

`returnable_requests` does the same for the GT requests Nic was shown as open and is waiting
on (§12, ROA-21): one comes back once when its result is checked and accepted, worded as done,
and once when its result comes back while he last saw it still being worked on, worded as
returned but not yet checked, never as done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Literal

from brain_core.errors import BrainError, ErrorCode
from brain_core.review import (
    REQUEST_DONE,
    REQUEST_OPEN,
    REQUEST_RETURNED_UNCHECKED,
    REQUEST_RETURNED_PROOF,
    REQUEST_PROOF_UNVERIFIED,
    quoted_proof,
    CalendarEvent,
    HistoryRow,
    ItemRecord,
    request_title,
)
from brain_core.timewords import SINGAPORE, today_in_singapore
from brain_core.work import WorkIndex, first_id, index_things, is_up

Signal = Literal["pc_on", "task_finished", "calendar_gap"]
SIGNALS: frozenset[str] = frozenset({"pc_on", "task_finished", "calendar_gap"})

DEFAULT_GAP_MINUTES = 30
DEFAULT_GAP_START_HOUR = 8
DEFAULT_GAP_END_HOUR = 21

_WAITING_LIKE_STATES = frozenset({"waiting", "blocked", "parked"})

# §12 (ROA-21). The rule `review.build_review` logs each handed-off request's "Waiting on" line
# under, with the state it showed (`review.REQUEST_OPEN` or `REQUEST_RETURNED_UNCHECKED`) as the
# row's `record_state`: the proof Nic was shown the request as open, and how.
WAITING_ON_REQUEST_RULE = "waiting_on_request"
# The two states a request can come back in, also kept as the why-log row's `record_state`.
REQUEST_RULES: dict[str, str] = {
    REQUEST_DONE: "request_done",
    REQUEST_RETURNED_UNCHECKED: "request_returned_unchecked",
    REQUEST_RETURNED_PROOF: "request_returned_proof",
    REQUEST_PROOF_UNVERIFIED: "request_proof_unverified",
}
# Fixed words, like the review's Missed line, naming the request as "Waiting on" did. Step B: a
# returned result is not done until someone other than the doer checks it, so only an accepted
# check reads "done".
_REQUEST_WORDS: dict[str, str] = {
    REQUEST_DONE: "{title} — done (was with {handed_to})",
    REQUEST_RETURNED_UNCHECKED: "{title} — returned, not yet checked (with {handed_to})",
    REQUEST_RETURNED_PROOF: "{title} — back, with proof: {proof} (with {handed_to})",
    REQUEST_PROOF_UNVERIFIED: "{title} — back, proof unverified: {proof} (with {handed_to})",
}
# A return that said "returned, not yet checked" also showed Nic the request as still open.
_SHOWN_OPEN_RULES = frozenset({WAITING_ON_REQUEST_RULE, REQUEST_RULES[REQUEST_RETURNED_UNCHECKED]})


@dataclass(frozen=True, slots=True)
class RequestStatus:
    """A handed-off GT request as GT reads it at a signal: step B's `work_state` and `check`
    (`accepted`, `rejected` or `none`, from the latest check of a returned result), and the
    objective the review names it by."""

    id: str
    handed_to: str
    work_state: str
    check: str = "none"
    objective: str = ""
    proof: str | None = None
    proof_verified: bool = True
    last_activity: datetime | None = None


@dataclass(frozen=True, slots=True)
class RequestReturn:
    """One request that comes back at a signal: the state it comes back in, and its words."""

    request: RequestStatus
    state: str
    text: str

    @property
    def rule(self) -> str:
        return REQUEST_RULES[self.state]


def _validate_signal(signal: str) -> None:
    if signal not in SIGNALS:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "Unknown signal; only pc_on, task_finished and calendar_gap exist",
            details={"signal": signal},
        )


def _last_surfacing(history: tuple[HistoryRow, ...], item_ref: str) -> HistoryRow | None:
    latest: HistoryRow | None = None
    for row in history:
        if row.item_ref != item_ref:
            continue
        if latest is None or row.surfaced_at > latest.surfaced_at:
            latest = row
    return latest


def _already_returned_today(history: tuple[HistoryRow, ...], item: ItemRecord, today: date) -> bool:
    for row in history:
        if row.item_ref != item.record.id or row.channel != "moment":
            continue
        if row.record_sha256 != item.sha256:
            continue
        if today_in_singapore(row.surfaced_at) == today:
            return True
    return False


def _event_happened(
    history: tuple[HistoryRow, ...], item: ItemRecord, index: WorkIndex, now: datetime,
) -> bool:
    """Last shown waiting by lasting identity, and actionable through the one reader now."""
    if not is_up(first_id(item.record), index, now):
        return False
    # Work-01 wrote physical version IDs; work-02 writes the lasting first ID.
    # Both name this same current thing through its complete raw lineage.
    aliases = {item.record.id, *item.record.supersedes}
    last: HistoryRow | None = None
    for row in history:
        if row.item_ref in aliases and (last is None or row.surfaced_at >= last.surfaced_at):
            last = row  # later written row wins a timestamp tie
    return last is not None and last.record_state in _WAITING_LIKE_STATES


def returnable(
    items: tuple[ItemRecord, ...],
    history: tuple[HistoryRow, ...],
    signal: str,
    now: datetime,
) -> tuple[ItemRecord, ...]:
    """Return changed items at an actionable signal, using the current full reader index.

    ``items`` includes current parts and their parents, with each ``ItemRecord`` retaining
    its raw record path and hash. History matches a thing by its lasting first ID, even
    after successor versions. A last-shown waiting thing returns when the work reader
    says it is up now; an unfinished pick alone never warrants a return. Empty means
    nothing is sent (step E §4, work-model plan W10).
    """

    _validate_signal(signal)
    today = today_in_singapore(now)
    index = index_things(item.record for item in items)
    result: list[ItemRecord] = []
    for item in items:
        key = first_id(item.record)
        if key not in index or index[key].id != item.record.id:
            continue
        if any(
            row.item_ref in {item.record.id, *item.record.supersedes} and row.channel == "moment"
            and row.record_sha256 == item.sha256
            and today_in_singapore(row.surfaced_at) == today
            for row in history
        ):
            continue
        if _event_happened(history, item, index, now):
            result.append(item)
    return tuple(result)


def request_state(request: RequestStatus) -> str | None:
    """`done` once someone other than the doer accepted the returned result; `returned_unchecked`
    while nobody has checked it. Anything else - still being worked on, declined, cancelled, or a
    result whose check was rejected - is nothing to report (§12)."""

    if request.work_state != "result_returned":
        return None
    if request.check == "accepted":
        return REQUEST_DONE
    if request.proof:
        return REQUEST_RETURNED_PROOF if request.proof_verified else REQUEST_PROOF_UNVERIFIED
    if request.check == "none":
        return REQUEST_RETURNED_UNCHECKED
    return None


def _came_back(history: tuple[HistoryRow, ...], request_id: str, state: str) -> bool:
    return any(
        row.item_ref == request_id and row.channel == "moment" and row.rule == REQUEST_RULES[state]
        for row in history
    )


_REQUEST_ROW_RULES = frozenset({WAITING_ON_REQUEST_RULE, *REQUEST_RULES.values()})


def _last_seen(history: tuple[HistoryRow, ...], request_id: str) -> str | None:
    """The state Nic was last shown a request in: its latest "Waiting on" line or return (the
    later row wins a tie, as the why-log lists rows in the order written). `None` for a line
    written before the review recorded the state it showed."""

    latest: HistoryRow | None = None
    for row in history:
        if row.item_ref != request_id or row.rule not in _REQUEST_ROW_RULES:
            continue
        if latest is None or row.surfaced_at >= latest.surfaced_at:
            latest = row
    return latest.record_state if latest is not None else None


def requests_to_check(history: tuple[HistoryRow, ...]) -> tuple[str, ...]:
    """The GT requests a signal may report on (§12): shown to Nic as open - in a 06:00 review's
    "Waiting on", or a return that said "returned, not yet checked" - and not yet told done.

    GT reads the state of these requests only; one Nic was never shown is never read or reported.
    """

    shown = {row.item_ref for row in history if row.rule in _SHOWN_OPEN_RULES}
    return tuple(sorted(ref for ref in shown if not _came_back(history, ref, REQUEST_DONE)
                        and not any(row.item_ref == ref and row.rule == REQUEST_RULES[REQUEST_RETURNED_PROOF]
                                    for row in history)))


def returnable_requests(
    requests: Iterable[RequestStatus],
    history: tuple[HistoryRow, ...],
    signal: str,
) -> tuple[RequestReturn, ...]:
    """The requests Nic was waiting on that come back at this signal, ordered by request ID.

    Each comes back at most once in each state, read from the why-log (§7). "Done" comes back
    once. "Returned, not yet checked" comes back only if Nic last saw the request still being
    worked on: not when the morning's "Waiting on" already said so. A request already done by the
    time a signal arrives comes back only as done. Empty means nothing is added (capability 10)."""

    _validate_signal(signal)
    shown = set(requests_to_check(history))
    by_id = {request.id: request for request in requests}
    result: list[RequestReturn] = []
    for request_id in sorted(by_id):
        request = by_id[request_id]
        state = request_state(request)
        if state is None or request_id not in shown or _came_back(history, request_id, state):
            continue
        if state in (REQUEST_RETURNED_UNCHECKED, REQUEST_PROOF_UNVERIFIED) and _last_seen(history, request_id) != REQUEST_OPEN:
            continue
        title = request_title(request_id, request.objective)
        text = _REQUEST_WORDS[state].format(
            title=title, handed_to=request.handed_to, proof=quoted_proof(request.proof or ""),
        )
        result.append(RequestReturn(request=request, state=state, text=text))
    return tuple(result)


def find_gap(
    events: tuple[CalendarEvent, ...],
    now: datetime,
    min_minutes: int = DEFAULT_GAP_MINUTES,
    start_hour: int = DEFAULT_GAP_START_HOUR,
    end_hour: int = DEFAULT_GAP_END_HOUR,
) -> bool:
    """True when there is no event now and the next one is at least `min_minutes` away,
    between `start_hour` and `end_hour` Singapore time (half-open: [start, end))."""

    local_now = now.astimezone(SINGAPORE)
    local_time = local_now.time()
    if not (time(start_hour, 0) <= local_time < time(end_hour, 0)):
        return False
    for event in events:
        if event.start.astimezone(SINGAPORE) <= local_now < event.end.astimezone(SINGAPORE):
            return False
    upcoming = [event for event in events if event.start.astimezone(SINGAPORE) > local_now]
    if not upcoming:
        return True
    next_event = min(upcoming, key=lambda event: event.start)
    gap = next_event.start.astimezone(SINGAPORE) - local_now
    return gap >= timedelta(minutes=min_minutes)
