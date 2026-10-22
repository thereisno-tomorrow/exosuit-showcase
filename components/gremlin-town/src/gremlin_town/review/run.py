"""Orchestrates one 06:00 review run or one moments check (§3, §4, §8).

Disabled (`[review] enabled = false`, the default) returns `{"outcome":
"disabled"}` immediately: no reads, no why-log, nothing sent. Enabled: collect
plain data, decide with brain_core (no model call), write the why-log *before*
rendering or sending, then deliver through Diablo - a why-log write failure
means nothing is sent (§7). `run_moments` takes its `signal` explicitly; the
polling loop that decides *which* signal fired is the deploy-time timer
(`deploy/gremlin-town-moments.timer.example`), on since 28 Sep 2026 and every 3
minutes since 29 Sep 2026 (`ops/audits/2026-09-28-review-01-install.md`,
`ops/audits/2026-09-29-google-install.md`).

With `[review] telegram_spool_dir` set (§2; Accepted - Nic, 29 Sep 2026), each
message's text for Telegram is also written there for the courier, after its
GT request exists and its run is recorded (`review/spool.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Protocol

from brain_core.moments import RequestReturn, Signal, returnable, returnable_requests
from brain_core.review import RANKING_POINTS, HistoryRow, ItemRecord, Morning, OpenNote, Review, note_for_task, render_morning
from brain_core.presentation import work_words
from brain_core.review import build_morning, with_name_suggestions, MORNING_PARTS, MORNING_TITLES
from gremlin_town.review.names import read_names
from brain_core.timewords import today_in_singapore

from gremlin_town.contracts import Actor
from gremlin_town.review.collect import (
    ReviewSources,
    collect_moments_candidates,
    collect_moments_requests,
    collect_review_inputs,
    collect_morning_inputs,
    read_open_note_list,
)
from gremlin_town.review.deliver import RequestCreator, deliver
from gremlin_town.review.settings import ReviewSettings
from gremlin_town.review.spool import require_folder, write_message, was_sent
from gremlin_town.review.why_log import PreparedMorning, Surfacing, WhyLog, WhyLogError

if TYPE_CHECKING:
    from brain_core.work import WorkIndex
    from gremlin_town.review.handoff_notes import HandoffNoteWriter
    from gremlin_town.review.placement import PlacementContext
    from gremlin_town.review.orient import OrientationContext

logger = logging.getLogger(__name__)

_WAITING_LIKE_STATES = frozenset({"waiting", "blocked", "parked"})
# A daytime return whose picked and waiting items couldn't be read (the storage-limit plan, layer 1):
# the run's outcome in the why-log's runs table ends in this, and the journal says why.
COULDNT_READ = "couldnt_read"


def _failed(status: str | None) -> bool:
    return status is not None and status != "not_configured"


# N12d (Accepted - Nic, 28 Sep 2026): a returned item with no first step says so in its line,
# so Diablo can recognise it and suggest one first step, marked as his. With the courier
# posting the text (step E §2), he gives it in his reply when Nic answers (Accepted - Nic, 29 Sep 2026).
NO_FIRST_STEP = "no first step yet"


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """What `run_review`/`run_moments` need from the host, not the whole `Runtime`.

    Keeps this module testable with fakes; `runtime.py` builds one of these
    from `[review]` and whatever sources are actually configured.
    """

    settings: ReviewSettings
    why_log: WhyLog | None
    sources: ReviewSources
    service: RequestCreator
    # The hub's own note keeper for handed-off work (N12a), built only when
    # `[review] handoff_notes` is on; `poll.poll_once` runs it (review/handoff_notes.py).
    handoff_notes: "HandoffNoteWriter | None" = None
    # `[orientation] ranking`: "Coming up" ranks as both asks do (PRD §5).
    ranking: str = RANKING_POINTS
    # `[placement]` (step I, build plan slice 4): the day's blocks and slots, kept by the same poll.
    # None, or off, and the poll does exactly what it did before.
    placement: "PlacementContext | None" = None
    morning_sender: "MorningSender | None" = None
    schedule_enabled: bool = False
    flowsavvy_enabled: bool = False
    schedule: Any | None = None
    auto_tidy_enabled: bool = False


class MorningSender(Protocol):
    """Receives built parts and refs for item-backed lines, or None with a frozen retry packet."""

    def send(self, context: ReviewContext, morning: Morning | None, references: tuple[str, ...],
             *, inputs, now: datetime, spool: str | None): ...


class ReviewPrerequisiteError(RuntimeError):
    """The host lacks the actual pinned input inventory required for its first morning."""

    field = "morning_bootstrap.input_sha/first_ids"
    fix = "Stage the actual pinned pre-move INPUT SHA and all first IDs via WhyLog.stage_morning_bootstrap."

    def __init__(self) -> None:
        super().__init__(f"Unmet morning prerequisite: {self.field}. {self.fix}")


class TelegramMorningSender:
    """The sole sender: full request to Diablo, short message to the Telegram courier."""

    def send(self, context: ReviewContext, morning: Morning | None, references: tuple[str, ...],
             *, inputs, now: datetime, spool: str | None):
        packet = context.why_log.morning_packet(today_in_singapore(now))
        if packet is None:
            if morning is None:
                raise RuntimeError("a morning retry requires its durable prepared packet")
            packet = self._prepare(context, morning, references, inputs=inputs, now=now, spool=spool)
        receipt = deliver(context.service, **packet.request)
        context.why_log.record_run(kind="review", started_at=packet.prepared_at, last_event_cursor=None, outcome="sent",
                                  bootstrap_input_sha=packet.bootstrap_input_sha)
        if spool is not None:
            write_message(spool, packet.request["client_operation_id"], packet.short)
        return receipt

    def _prepare(self, context: ReviewContext, morning: Morning, references: tuple[str, ...],
                 *, inputs, now: datetime, spool: str | None) -> PreparedMorning:
        from brain_core import work

        settings = context.settings
        lines = tuple(line for part in morning.parts for line in part.lines)
        if len(references) != sum(line.item_ref is not None for line in lines):
            raise WhyLogError("morning references do not match its item-backed lines")
        line_references = iter(references)
        constraints = {
            "short": morning.short,
            "lines": [dict(reference=next(line_references) if line.item_ref is not None else None,
                           kind=line.rule, part=line.section, number=line.number,
                           item_ref=line.item_ref, question=line.text,
                           allowed_answers=list(line.allowed_answers), offered=list(line.offered),
                           suggested_name=line.suggested_name,
                           suggested_title=line.suggested_title, suggested_label=line.suggested_label)
                      for line in lines],
        }
        # Only raw own stakes are examples; never serialize inherited reader facts.
        index = work.index_things(entry.record for entry in inputs.items)
        raw = {entry.record.id: entry.record for entry in inputs.items}
        examples = [raw[record.id] for record in index.values()
                    if (raw[record.id].data.get("stake") or {}).get("confirmed")]
        examples.sort(key=lambda record: (record.updated_at, record.id), reverse=True)
        constraints["stake_examples"] = [dict(item_ref=work.first_id(record), title=record.title,
                                              stake=record.data["stake"]) for record in examples[:10]]
        start = ('The courier posts only the short message. Acknowledge; post no parts on wake. '
                 if spool is not None else 'Post constraints.short once to Nic’s Telegram. ')
        request = dict(
            sender_participant=settings.sender_participant,
            delivery_participant=settings.delivery_participant, project=settings.project or "",
            client_operation_id=f"review-{today_in_singapore(now).isoformat()}",
            objective="Today's 06:00 morning review: short message, then four parts on start",
            text=render_morning(morning), constraints=constraints,
            next_action=start + 'On Nic’s "start", load gremlin-town-review, then post Look, Catch up, New and Your day '
                'one part at a time, exactly as given. Answers go through bc_work with the line references in answers; '
                'picks go through gremlin_town_review_respond with item_refs. Ask "Is that your day?" only after he picks; '
                'send day_settled only on his yes.',
            expected_reply="Acknowledge the morning request; keep its parts for Nic's start",
        )
        return context.why_log.prepare_morning(PreparedMorning(
            now, request, references, morning.short,
            inputs.bootstrap.input_sha if inputs.bootstrap is not None and not inputs.bootstrap.completed else None,
            not lines,
        ))


def _log_morning_surfacings(why_log: WhyLog, morning: Morning, now: datetime) -> tuple[str, ...]:
    lines = tuple(line for part in morning.parts for line in part.lines if line.item_ref is not None)
    why_log.record_surfacings(Surfacing(
        surfaced_at=now, channel="review", item_ref=line.item_ref,
        record_sha256=line.record_sha256 or "", record_path=line.record_path,
        record_state=line.record_state, rule=line.rule, fields_used=line.fields_used,
        assumptions=line.assumptions, unknowns=line.unknowns,
        question=line.text, offered=line.offered,
    ) for line in lines)
    # A retry at an identical timestamp may have earlier aborted rows. Use this batch's IDs.
    references = why_log.review_references(now)
    return references[-len(lines):] if lines else ()


def _build_morning(context, inputs, **options):
    if context.settings.opening_deadlines:
        options["opening_deadlines"] = True
    if context.settings.new_lengths:
        options["new_lengths"] = True
    morning = build_morning(inputs, **options)
    if context.settings.tidy:
        from brain_core.review import with_tidy_suggestions
        from gremlin_town.review.tidy import read_tidy
        morning = with_tidy_suggestions(morning, read_tidy(context, inputs.now))
    if context.auto_tidy_enabled:
        from brain_core.review import with_tidy_suggestions
        from gremlin_town.review.tidy import read_added_tidy
        suggestions = read_added_tidy(context, inputs.now, current_items=inputs.items,
            open_item_refs={line.item_ref for part in morning.parts for line in part.lines if line.item_ref}, enabled=True)
        morning = with_tidy_suggestions(morning, suggestions)
    return morning


def current_review_part(context: ReviewContext, now: datetime, *, caller: str,
                        door_participant: str = "hermes-wa", part: int = 4,
                        relayer: str | None = None, code_callers: frozenset[str] = frozenset(),
                        review_day: date | None = None, section_cap: int = 10) -> dict[str, object]:
    """Current lines for any part, preserving the questions and answers already shown."""
    return current_review_parts(context, now, caller=caller, door_participant=door_participant,
        parts=(part,), relayer=relayer, code_callers=code_callers,
        review_day=review_day, section_cap=section_cap)[part]


def current_review_parts(context: ReviewContext, now: datetime, *, caller: str,
                        door_participant: str = "hermes-wa", parts: tuple[int, ...] = (1, 2, 3, 4),
                        relayer: str | None = None, code_callers: frozenset[str] = frozenset(),
                        review_day: date | None = None, section_cap: int = 10, card_local_day: bool = False,
                        prepared_index=None) -> dict[int, dict[str, object]]:
    """Build one fresh snapshot, then preserve shown questions for each requested part."""
    from gremlin_town.mcp.brain_runtime import BrainRuntimeError, work_caller_allowed
    from brain_core.timewords import personal_today

    identity = relayer if relayer is not None else caller
    if not work_caller_allowed(identity, door_participant=door_participant, code_callers=code_callers):
        raise BrainRuntimeError("ACCESS_DENIED", "Only Diablo reads the current review part", field="caller",
                                fix="Nic goes through his review with Diablo")
    if not parts or any(not isinstance(part, int) or isinstance(part, bool) or part not in (1, 2, 3, 4) for part in parts):
        raise ValueError("part must be 1, 2, 3 or 4")
    log = context.why_log
    if not context.settings.enabled or log is None:
        raise RuntimeError("review is disabled")
    day = review_day if review_day is not None else personal_today(now)
    packet = log.morning_packet(day)
    if packet is None or not packet.completed:
        if not card_local_day or identity != "system:review-card":
            raise WhyLogError("no sent morning review for today")
        # Interactive local-date questions do not impersonate courier delivery.
        # Their refs are still surfaced by the same checked WhyLog below.
        from types import SimpleNamespace
        packet = SimpleNamespace(prepared_at=None, request={"constraints": {"lines": []}})
    inputs = collect_morning_inputs(context.sources, log.morning_history(), now, ranking=context.ranking,
        previous_review_at=packet.prepared_at, bootstrap=log.morning_bootstrap(), shown_item_refs=log.shown_item_refs())
    if inputs.source_status.get("brain") is not None:
        raise WhyLogError(f"current Brain records are unavailable: {inputs.source_status['brain']}")
    # The personal day keeps a 03:00 continuation on the same review. Source timestamps remain current.
    morning = with_name_suggestions(_build_morning(context, inputs, today=day, section_cap=section_cap, prepared_index=prepared_index), read_names(context, now))
    from brain_core import work
    index = work.index_things(entry.record for entry in inputs.items) if prepared_index is None else prepared_index
    blocked = frozenset(ref for ref, record in index.items() if work.has_unfinished_prerequisites(record, index))
    # Keep the supplied order: New establishes the side-question numbers used by Your day.
    return {part: _current_part(context, log, packet, morning, now, day, part, blocked=blocked) for part in parts}


def _current_part(context, log, packet, morning, now, day, part, *, blocked=frozenset()):
    original = packet.request["constraints"]["lines"]
    name = MORNING_PARTS[part - 1]
    saved = log.review_part_state(name, original=original, now=now, review_day=day) if part != 4 else ()
    refresh_pending = part == 3 and (context.settings.tidy or context.settings.new_lengths or context.auto_tidy_enabled)
    refreshed_keys = {(line.item_ref, line.rule) for line in morning.parts[part - 1].lines
                      if refresh_pending and line.rule in {"needs_you", "needs_length", "new_schedule"}}
    old_numbers = {(line["item_ref"], line["rule"]): line["number"] for line in saved}
    saved_keys = {(line["item_ref"], line["rule"]) for line in saved}
    last_number = max((line["number"] for line in original if line["part"] in MORNING_PARTS[:part - 1]), default=0)
    last_number = max(last_number, max((line["number"] for line in saved), default=0))
    lines = tuple(replace(line, number=old_numbers.get((line.item_ref, line.rule), last_number + i + 1) if line.number else 0)
                  for i, line in enumerate(line for line in morning.parts[part - 1].lines
                                           if (line.item_ref, line.rule) not in saved_keys - refreshed_keys))
    saved = tuple(line for line in saved if (line["item_ref"], line["rule"]) not in refreshed_keys)
    if part == 4:
        # Earlier answers shift rebuilt New numbers; point at the questions Nic was shown.
        side_numbers = {line["item_ref"]: line["number"]
                        for line in log.review_part_state("new", original=original, now=now, review_day=day)
                        if line["kind"] == "needs_you" and line["number"]}
        lines = tuple(replace(line, text=line.text.rsplit(" — side question", 1)[0] + (
            f" — side question, see {side_numbers[line.fields_used['side_question_ref']]}"
            if line.fields_used["side_question_ref"] in side_numbers
            else " — side question in New; ask to see more"))
            if "side_question_ref" in line.fields_used else line for line in lines)
    data = (*({key: value for key, value in line.items() if key != "rule"} for line in saved),
            *log.current_part_lines(name, lines, now=now, original=original, refresh_pending=refresh_pending, review_day=day))
    if refresh_pending:
        data = tuple(sorted(data, key=lambda line: line["number"] or float("inf")))
    if part != 1:
        # Retain the audit and answer identities, but never offer an unfinished
        # successor merely because an earlier version was already surfaced.
        data = tuple(line for line in data if line.get("item_ref") not in blocked)
    text = "\n".join([f"## {MORNING_TITLES[part - 1]}", *(
        f"{line['number']}. {line['text']}" if line['number'] else line['text'] for line in data)])
    if not data:
        text += "\nNothing here today."
    return dict(part=part, text=text, lines=list(data))


def _telegram_spool(settings: ReviewSettings) -> str | None:
    """The courier's folder when `telegram_spool_dir` is set (§2), else `None`.

    Checked before anything is logged or sent, so a missing or read-only folder fails the run
    the way a why-log failure does, with nothing sent (`spool.SpoolError`)."""

    if settings.telegram_spool_dir is None:
        return None
    require_folder(settings.telegram_spool_dir)
    return settings.telegram_spool_dir


def _moment_operation_id(signal: Signal, recipient: str, occurrence: str) -> str:
    return f"moment-{signal}-{recipient}-{occurrence}"


def close_hub_deliveries(context: ReviewContext, now: datetime) -> int:
    """Check courier files and settled-day rows each poll, even with no new signal."""
    if not context.settings.enabled or context.why_log is None:
        return 0
    reader = getattr(context.service, "hub_delivery_requests", None)
    if reader is None:
        return 0  # a thin request-creator fake has no delivery reader
    hub = context.settings.sender_participant
    actor = Actor(actor_id=hub)
    closed = 0
    for delivery in reader(actor, hub_participant=hub):
        key = delivery["client_operation_id"]
        proof = None
        if key.startswith("moment-") and context.settings.telegram_spool_dir is not None:
            if was_sent(context.settings.telegram_spool_dir, key):
                proof = f"sent/{key}.txt"
        elif key.startswith("review-"):
            day = date.fromisoformat(key.removeprefix("review-"))
            rows = context.why_log.signals_on("day_settled", day)
            if rows:
                proof = f"why-log:day_settled:{rows[0].id}"
        if proof is not None:
            closed += context.service.close_hub_delivery(  # type: ignore[attr-defined]
                actor, delivery["request_id"], hub_participant=hub, proof=proof,
            )
    return closed


# With the courier on (§2), Diablo posts nothing when a request wakes him: he acknowledges it
# and returns this result, and reads the request again when Nic answers by number.
_COURIER_NEXT_ACTION = (
    'Nothing to post: the courier posts the text. Acknowledge, then return the result "posted by the courier"'
)
_COURIER_CLARIFY = "When Nic next answers the review, ask him about each task in constraints.clarify, in your own words"
# PRD §4.1: the morning's sorting, beside the review like the questions.
_COURIER_TO_SORT = (
    "When Nic next answers the review, ask him to approve a sort for each item in constraints.to_sort, "
    "as your skill says"
)
_COURIER_EXPECTED_REPLY = "posted by the courier"


def _courier_wording(objective: str, *, clarify: bool = False, to_sort: bool = False) -> dict[str, str]:
    """The request's wording when the courier posts its text (§2; Accepted - Nic, 29 Sep 2026)."""

    parts = [_COURIER_NEXT_ACTION, *([_COURIER_CLARIFY] if clarify else []), *([_COURIER_TO_SORT] if to_sort else [])]
    return {"objective": objective, "next_action": ". ".join(parts), "expected_reply": _COURIER_EXPECTED_REPLY}


def _log_review_surfacings(why_log: WhyLog, review: Review, now: datetime) -> None:
    lines = [
        Surfacing(
            surfaced_at=now,
            channel="review",
            item_ref=line.item_ref,
            record_sha256=line.record_sha256 or "",
            rule=line.rule,
            fields_used=line.fields_used,
            record_path=line.record_path,
            record_state=line.record_state,
            assumptions=line.assumptions,
            unknowns=line.unknowns,
        )
        for line in review.lines
    ]
    # Each clarifying question is its own row under its own `clarify:<record_id>` reference
    # (§3.1), so a response to the task's review line still lands on that line's row. Its rule
    # says which question it was: a thin task's, or a started task's first step (N12c).
    questions = [
        Surfacing(
            surfaced_at=now,
            channel="review",
            item_ref=task.item_ref,
            record_sha256=task.record_sha256,
            rule=task.rule,
            fields_used=task.fields_used,
            record_path=task.record_path,
            record_state=task.fields_used.get("state"),
        )
        for task in review.clarify
    ]
    why_log.record_surfacings([*lines, *questions])


# §3.1: the picks travel as data beside the verbatim review, never inside it.
_CLARIFY_NEXT_ACTION = (
    "Post the text to Nic's Telegram, verbatim. Then, in one separate message, ask Nic about "
    "each task in constraints.clarify, in your own words"
)
_CLARIFY_EXPECTED_REPLY = "The Telegram message IDs of the review and of the questions once posted"


def _clarify_entry(task) -> dict[str, object]:
    entry: dict[str, object] = {"record_id": task.record_id, "title": task.title, "number": task.number}
    if task.question is not None:
        # N12c: a started task's first step - asked in these words; the answer becomes the
        # next step of the note named here.
        entry["question"] = task.question
        entry["session_id"] = task.session_id
    return entry


# PRD §4.1 (build plan slice 4): each item to sort this morning, with what it already has, so
# Diablo can show an agent's pre-sort, or pre-sort an unsorted task himself, for Nic's yes.
_SORT_FIELDS = ("level", "speaker", "block", "first_step", "timing", "raise_with", "owed_to", "repeat", "links")
_TO_SORT_NEXT_ACTION = "propose a sort for each item in constraints.to_sort, for his yes"


def _sort_entry(entry) -> dict[str, object]:
    fields = entry.fields_used
    result: dict[str, object] = {"record_id": entry.record_id, "title": entry.title, "number": entry.number}
    result.update({name: fields[name] for name in _SORT_FIELDS if fields.get(name) not in (None, "", [], {})})
    if fields.get("two_minutes"):
        result["two_minutes"] = True  # offer to do it now
    return result


def _clarify_delivery(review: Review) -> dict[str, object]:
    if not review.clarify and not review.to_sort:
        return {}
    constraints: dict[str, object] = {}
    if review.clarify:
        constraints["clarify"] = [_clarify_entry(task) for task in review.clarify]
    if review.to_sort:
        constraints["to_sort"] = [_sort_entry(entry) for entry in review.to_sort]
    if review.clarify and review.to_sort:
        next_action = f"{_CLARIFY_NEXT_ACTION}, and {_TO_SORT_NEXT_ACTION}"
    elif review.clarify:
        next_action = _CLARIFY_NEXT_ACTION
    else:
        next_action = f"Post the text to Nic's Telegram, verbatim. Then, in one separate message, {_TO_SORT_NEXT_ACTION}"
    return {"constraints": constraints, "next_action": next_action, "expected_reply": _CLARIFY_EXPECTED_REPLY}


def run_review(context: ReviewContext, now: datetime) -> dict[str, object]:
    settings = context.settings
    if not settings.enabled:
        return {"outcome": "disabled"}
    why_log = context.why_log
    if why_log is None:
        raise RuntimeError("[review] is enabled but no why-log was built")
    spool = _telegram_spool(settings)

    why_log.prune(now)
    today = today_in_singapore(now)
    if why_log.sent_today("review", today):
        # Replay-safe (§3): a same-day rerun must not re-derive content, since the
        # "raised once" rules would make a second render differ from the first.
        return {"outcome": "already_sent"}

    packet = why_log.morning_packet(today)
    morning = inputs = None
    if packet is not None:
        references = packet.references
    else:
        bootstrap = why_log.morning_bootstrap()
        if bootstrap is None:
            raise ReviewPrerequisiteError()
        history = why_log.morning_history()
        inputs = collect_morning_inputs(context.sources, history, now, ranking=context.ranking,
                                        previous_review_at=why_log.last_sent_review_at(), bootstrap=bootstrap,
                                        shown_item_refs=why_log.shown_item_refs())
        morning = with_name_suggestions(_build_morning(context, inputs), read_names(context, now))
        references = _log_morning_surfacings(why_log, morning, now)
    receipt = (context.morning_sender or TelegramMorningSender()).send(
        context, morning, references, inputs=inputs, now=now, spool=spool,
    )
    packet = why_log.morning_packet(today)
    result: dict[str, object] = {
        "outcome": "sent",
        "request_id": receipt.request_id,
        "is_empty": packet.is_empty if packet else all(part.is_empty for part in morning.parts),
        "line_count": len(packet.references) if packet else len(references),
        "clarify_count": 0,
        "to_sort_count": 0,
    }
    if settings.sweep_participant and today.weekday() == SWEEP_WEEKDAY:
        result["sweep"] = _send_sweep(context, settings.sweep_participant, today)
    return result


# The weekly sweep goes out with Friday's review, the weekly review's day (PRD section 4.8).
SWEEP_WEEKDAY = 4
SWEEP_OBJECTIVE = "Weekly sweep: bring what Nic would be hurt to lose from your workers into Brain Home"
SWEEP_TEXT = (
    "Ask each worker you run for anything it holds that passes the test \"Would Nic be hurt if this were "
    "forgotten or done late?\" (a task, promise, date, someone he's waiting on, a decision) and is new or "
    "changed since the last sweep. bc_memory_search each one for a duplicate first. Save only the new ones as "
    "suggestions: level suggested, said_by other, speaker the worker's exact name, words the worker's exact "
    f"wording. They reach Nic in the 06:00 morning's New part, under {work_words('needs_you')}."
)


def _send_sweep(context: ReviewContext, participant: str, today: date) -> dict[str, object]:
    """Friday's sweep request, once per day by its operation key. Best effort, like D24's second
    delivery: the review has already been sent, so a failure here is reported, never raised."""

    settings = context.settings
    try:
        receipt = deliver(
            context.service,
            sender_participant=settings.sender_participant,
            delivery_participant=participant,
            project=settings.project or "",
            client_operation_id=f"sweep-{participant}-{today.isoformat()}",
            objective=SWEEP_OBJECTIVE,
            text=SWEEP_TEXT,
            next_action="Sweep your workers now, save the new items as suggestions, then report",
            expected_reply="Per worker: what you saved (record IDs) and what you skipped as a duplicate",
        )
    except Exception as exc:  # noqa: BLE001 - reported in the run's result and the unit's log
        return {"outcome": "failed", "recipient": participant, "error": type(exc).__name__}
    return {"outcome": "sent", "recipient": participant, "request_id": receipt.request_id}


def _last_surfacing(history: tuple[HistoryRow, ...], item_ref: str) -> HistoryRow | None:
    latest: HistoryRow | None = None
    for row in history:
        if row.item_ref == item_ref and (latest is None or row.surfaced_at > latest.surfaced_at):
            latest = row
    return latest


def _moment_line_text(
    item: ItemRecord, history: tuple[HistoryRow, ...], *, index: "WorkIndex", now: datetime, today: date,
) -> str:
    """A newly up part, its parent/progress and its owner's current next action."""
    from brain_core import work

    record = index[work.first_id(item.record)]
    base = f"{record.title} — no longer waiting"
    parent = index.get(record.data.get("parent"))
    if parent is not None:
        base += f" ({parent.title}; {work.progress_of(parent, index)['text']})"
    return _add_first_step(base, item, history, index=index, now=now, today=today)


def _first_step(item: ItemRecord, notes: tuple[OpenNote, ...]) -> str:
    """The returned item's first step: its open note's next step, found through the note's
    link to its task whatever the note is called, or the older `item-<record_id>` key (N12b)."""

    note = note_for_task(item.record, notes)
    return "" if note is None else note.next_step.strip()


def _add_first_step(
    base: str, item: ItemRecord, history: tuple[HistoryRow, ...], *,
    index: "WorkIndex", now: datetime, today: date,
) -> str:
    """Add only an unfinished owner action from the reader, and the existing stall box."""
    from dataclasses import replace
    from brain_core import work
    from brain_core.orientation import tends_to_stall, time_box

    record = index[work.first_id(item.record)]
    next_item = work.next_of(record, index, now)
    next_record = index[next_item["item_ref"]]
    action = next_record.data.get("next_action") or {}
    marked = any(mark.get("mark") == "agents_wording" and mark.get("part") == "next_action"
                 for mark in next_record.data.get("review_marks", ()))
    extra = [f"next: {next_item['text']}"] if action and not action.get("done") and not marked else []
    # Adapt only historical references for the stall reader's raw Record ID;
    # exact version hashes stay pinned, never replaced by a mapped hash.
    stall_history = tuple(replace(row, item_ref=item.record.id)
                          for row in history
                          if row.item_ref in {work.first_id(item.record), item.record.id}
                          and today_in_singapore(row.surfaced_at) < today)
    box = time_box(item.record, tends_to_stall(item.record, stall_history))
    if box is not None:
        extra.append(f"{box} minutes, then stop")
    return f"{base} ({'; '.join(extra)})" if extra else base


def _orientation_notes(orientation: "OrientationContext") -> tuple[OpenNote, ...]:
    """Orientation's own `session_id -> next_step` notes, keyed `item-<record_id>` (step F §6)."""

    from gremlin_town.review.orient import read_open_notes

    mapping, _status = read_open_notes(orientation.sources.open_notes)
    earliest = datetime.min.replace(tzinfo=UTC)
    return tuple(OpenNote(session_id=key, next_step=value, updated_at=earliest) for key, value in mapping.items())


def _request_surfacings(requests: tuple[RequestReturn, ...], signal: Signal, now: datetime) -> list[Surfacing]:
    """One why-log row per request that comes back (§12): `record_state` is the state it came
    back in, which is what keeps it to once per state. A GT request has no Brain Home record,
    so, as for the review's "Waiting on" line, its SHA-256 is empty and it has no path."""

    return [
        Surfacing(
            surfaced_at=now,
            channel="moment",
            signal=signal,
            item_ref=entry.request.id,
            record_sha256="",
            record_state=entry.state,
            rule=entry.rule,
            fields_used={
                "signal": signal,
                "work_state": entry.request.work_state,
                "check": entry.request.check,
                "handed_to": entry.request.handed_to,
                "objective": entry.request.objective,
            },
        )
        for entry in requests
    ]


def run_moments(context, now, signal, *, also_notify=None, signal_id=None, orientation=None):
    """Serialize schedule returns across processes before reading daily consumption."""
    if context.settings.enabled and context.schedule_enabled and context.why_log is not None:
        with context.why_log.schedule_return_lock() as acquired:
            if not acquired:
                return {"outcome": "busy", "signal": signal}
            return _run_moments(context, now, signal, also_notify=also_notify,
                                signal_id=signal_id, orientation=orientation)
    return _run_moments(context, now, signal, also_notify=also_notify,
                        signal_id=signal_id, orientation=orientation)


def _run_moments(
    context: ReviewContext,
    now: datetime,
    signal: Signal,
    *,
    also_notify: str | None = None,
    signal_id: str | None = None,
    orientation: "OrientationContext | None" = None,
) -> dict[str, object]:
    """`also_notify` is the agent Nic is using at the time (D24): daytime returns go to
    both Diablo on Telegram and that agent. Only the caller knows who that is for a
    given signal occurrence (e.g. `local-codex` for `pc_on`); a `calendar_gap` with no
    such agent passes `None` and gets the Diablo-only delivery.

    Each returned line names its current part, parent/progress and owner's next action
    from the full work index. Retired notes and agent first steps are not read. The existing
    stall time box remains, whether or not `orientation` is given.

    The same signal also brings back, after the items, each GT request Nic was shown in
    "Waiting on" whose result has since come back or been checked (§12, ROA-21).

    A read that fails is never dropped silently (the storage-limit plan, layer 1): it is logged,
    and when the picked and waiting items couldn't be read, the run's outcome in the why-log's runs
    table is `<signal>:couldnt_read` (`<signal>:sent:couldnt_read` when something else still went
    out), and the result carries `couldnt_read` with the reason, in plain words for a Brain Home
    size limit."""
    settings = context.settings
    if not settings.enabled:
        return {"outcome": "disabled"}
    why_log = context.why_log
    if why_log is None:
        raise RuntimeError("[review] is enabled but no why-log was built")
    spool = _telegram_spool(settings)
    telegram_wording = (
        _courier_wording("This update; the courier posts it to Nic's Telegram")
        if spool is not None
        else {"objective": "Post this update to Nic's Telegram, verbatim"}
    )

    close_hub_deliveries(context, now)

    why_log.prune(now)
    history = why_log.history()
    candidates, candidates_status = collect_moments_candidates(context.sources, history, now)
    couldnt_read = candidates_status if _failed(candidates_status) else None
    if couldnt_read is not None:
        logger.warning("Daytime return (%s) couldn't read today's picked and waiting items: %s", signal, couldnt_read)
    from brain_core import work

    index = work.index_things(item.record for item in candidates)
    items = returnable(candidates, history, signal, now)
    statuses, requests_status = collect_moments_requests(context.sources, history)
    if _failed(requests_status):
        logger.warning("Daytime return (%s) couldn't read the requests Nic is waiting on: %s", signal, requests_status)
    requests = returnable_requests(statuses, history, signal)
    # Step I §5a: a pick or something due today that no longer fits today's plan comes back once,
    # here, with the rest of the daytime return (build plan slice 6).
    today = today_in_singapore(now)
    notices = () if context.flowsavvy_enabled or context.schedule_enabled else why_log.pending_placement_notices(today)
    schedule_notices = ()
    if context.schedule_enabled and context.schedule is not None:
        from gremlin_town.review.schedule import late_notices

        try:
            snapshot = context.schedule(now)
            if snapshot.available:
                fresh = tuple(context.sources.brain_items()) if context.sources.brain_items else ()
                schedule_notices = late_notices(snapshot, fresh, history, now)
        except Exception:
            logger.warning("Daytime return could not read the schedule")
    if not items and not requests and not notices and not schedule_notices:
        if couldnt_read is not None:
            why_log.record_run(
                kind="moments", started_at=now, last_event_cursor=None, outcome=f"{signal}:{COULDNT_READ}",
            )
            return {"outcome": "nothing", "signal": signal, COULDNT_READ: couldnt_read}
        why_log.record_run(kind="moments", started_at=now, last_event_cursor=None, outcome=f"{signal}:nothing")
        return {"outcome": "nothing", "signal": signal}

    surfacings = [
        Surfacing(
            surfaced_at=now,
            channel="moment",
            signal=signal,
            item_ref=work.first_id(item.record),
            record_sha256=item.sha256,
            record_state=work.status_of(work.first_id(item.record), index, now),
            record_path=item.path,
            rule="right_moment_return",
            fields_used={"signal": signal, "state": work.status_of(work.first_id(item.record), index, now)},
        )
        for item in items
    ]
    surfacings += _request_surfacings(requests, signal, now)
    why_log.record_surfacings(surfacings)  # written before sending (§7); raises on failure

    lines = [
        _moment_line_text(item, history, index=index, now=now, today=today) for item in items
    ]
    lines += [entry.text for entry in requests]
    lines += [
        f'"{notice["title"]}" no longer fits today\'s plan. Drop it, move it to tomorrow, or swap it for something?'
        for notice in notices
    ]
    lines += [f'"{notice["title"]}" is now scheduled after its deadline.' for notice in schedule_notices]
    text = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    occurrence = signal_id or now.isoformat()

    telegram_receipt = deliver(
        context.service,
        sender_participant=settings.sender_participant,
        delivery_participant=settings.delivery_participant,
        project=settings.project or "",
        client_operation_id=_moment_operation_id(signal, settings.delivery_participant, occurrence),
        text=text,
        **telegram_wording,
    )
    deliveries = [{"recipient": settings.delivery_participant, "request_id": telegram_receipt.request_id}]

    if also_notify and also_notify != settings.delivery_participant:
        try:
            agent_receipt = deliver(
                context.service,
                sender_participant=settings.sender_participant,
                delivery_participant=also_notify,
                project=settings.project or "",
                client_operation_id=_moment_operation_id(signal, also_notify, occurrence),
                objective="Show Nic this update now, since he is using you (D24)",
                text=text,
                next_action="Surface this to Nic in your current conversation",
                expected_reply="Acknowledged",
            )
        except Exception:
            # D24's second delivery is best-effort: an agent with no configured
            # route yet (or otherwise unreachable) never blocks the primary
            # Diablo delivery above, which has already succeeded.
            pass
        else:
            deliveries.append({"recipient": also_notify, "request_id": agent_receipt.request_id})

    why_log.record_run(
        kind="moments", started_at=now, last_event_cursor=None,
        outcome=f"{signal}:sent" + (f":{COULDNT_READ}" if couldnt_read is not None else ""),
    )
    if notices:
        why_log.mark_placement_notices_sent(today, (notice["key"] for notice in notices), now)
    if spool is not None:  # Diablo's copy only: the agent in also_notify is not on Telegram
        write_message(spool, _moment_operation_id(signal, settings.delivery_participant, occurrence), text)
    if schedule_notices:
        # Only successful delivery consumes a notice, so a failed send remains retryable.
        why_log.record_surfacings(tuple(Surfacing(
            surfaced_at=now, channel="moment", signal=signal, item_ref=notice["key"],
            record_sha256=notice["sha256"], record_path=notice["path"], record_state=notice["state"],
            rule="scheduled_after_deadline", fields_used={"deadline": notice["deadline"], "end": notice["end"]},
        ) for notice in schedule_notices))
    return {
        "outcome": "sent",
        "signal": signal,
        "request_id": telegram_receipt.request_id,
        "deliveries": tuple(deliveries),
        "item_ids": tuple(item.record.id for item in items),
        "request_ids": tuple(entry.request.id for entry in requests),
        **({"placement_notices": tuple(notice["key"] for notice in notices)} if notices else {}),
        **({"schedule_notices": tuple(notice["key"] for notice in schedule_notices)} if schedule_notices else {}),
        **({COULDNT_READ: couldnt_read} if couldnt_read is not None else {}),
    }
