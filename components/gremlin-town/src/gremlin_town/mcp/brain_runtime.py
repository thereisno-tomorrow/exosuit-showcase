"""Typed, host-constructed Brain Core boundary for the shared MCP.

This module contains no filesystem, network, repository, or Brain JSON adapter.
The host injects those capabilities and a trusted owner authorizer.  Callers can
select a semantic scope, but cannot construct owner bindings, roots, grants,
repository settings, or publication evidence.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from typing import Literal, NotRequired, Protocol, TypedDict, TypeVar, runtime_checkable


work_check_only: ContextVar[bool] = ContextVar("brain_check_only", default=False)


def work_caller_allowed(subject: str | None, *, door_participant: str = "hermes-wa",
                        code_callers: frozenset[str] = frozenset()) -> bool:
    """Admit an authenticated participant or an explicitly registered hub relayer.

    Caller identity comes from host authentication, never from public arguments.
    System names never gain access by resembling the participant's name. The
    provider separately requires the host's authenticated owner tap for saves.
    """
    if not subject:
        return False
    return subject in code_callers if subject.startswith("system:") else subject == door_participant


OwnerKind = Literal["personal", "cross-project", "system", "fleet", "project"]
AccessMode = Literal["read", "write"]
Freshness = Literal["current", "stale"]
ContinuityEvent = Literal[
    "source", "effect", "decision", "commitment", "candidate", "blocker", "intent", "next_step"
]
MemoryKind = Literal["item", "fact", "preference"]
MemorySaidBy = Literal["owner", "agent", "other"]
MemoryCameBy = Literal["direct", "agent_guess", "connected_source", "system"]
# Ciel redesign N5 (step C §13), widened by the Ciel and Brain Home PRD §3: what a save or change
# does to an item's stake. proposed/confirmed are "can't undo" (level high); real_cost and minor
# are an agent's labelled guess that counts as saved; normal removes it.
MemoryImportance = Literal["proposed", "confirmed", "real_cost", "minor", "normal"]
# Ciel and Brain Home PRD §3: the kind of time block a task belongs in.
MemoryBlock = Literal["deep_focus", "admin", "errands"]
# Ciel and Brain Home PRD §4.1: the outcome an agent sorted a new item into. "Do it now" is offered,
# never saved, so it isn't one of these.
MemorySort = Literal["next_action", "waiting_on", "raise_with", "someday", "reference", "drop"]


class BrainOperation(StrEnum):
    CONTEXT_RETRIEVE = "context.retrieve"
    CONTINUITY_START = "continuity.start"
    CONTINUITY_APPEND = "continuity.append"
    CONTINUITY_RECOVER = "continuity.recover"
    CONTINUITY_LIST = "continuity.list"
    CONTINUITY_CLOSE = "continuity.close"
    DECISION_RECORD = "decision.record"
    MEMORY_SAVE = "memory.save"
    MEMORY_CHANGE = "memory.change"
    MEMORY_READ = "memory.read"
    MEMORY_SEARCH = "memory.search"
    MEMORY_LIST = "memory.list"
    WORK = "work"
    # Reading project sitreps from Brain Home (docs/product/steps/b-project-sitreps.md), only
    # with `[sitreps]` on. Granted on the Brain Home cross-project owner, so it reaches every
    # agent Nic uses, including agents that aren't enrolled on the project.
    SITREP_READ = "sitrep.read"


class BrainRuntimeError(Exception):
    """Stable failure safe for the MCP wrapper to render."""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 field: str = "arguments", fix: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.field = field
        self.fix = fix or {
            "ACCESS_DENIED": "use an authorized caller and owner scope",
            "brain.policy.excluded": "use an authorized owner scope and operation",
            "brain.capability.unavailable": "retry when the service is available",
            "brain.record.write_failed": "resend the same operation_id to check the result",
            "brain.record.exists": "resend the complete original change or use a new operation_id",
            "brain.conflict": "use a new operation_id for a different change",
            "brain.record.changed": "read the current version and send a new change",
            "brain.not_found": "name an existing record visible in this owner scope",
            "brain.capacity.exceeded": "split the change or ask the operator to make room; nothing was saved",
            "brain.record.corrupt": "ask the operator to check the record before retrying",
            "brain.record.needs_owner_words": "quote Nic's message and set said_by owner, came_by direct",
        }.get(code, "correct the named field and resend the request")


@dataclass(frozen=True, slots=True)
class BrainCaller:
    subject: str
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("subject must be non-empty")


@dataclass(frozen=True, slots=True)
class OwnerSelector:
    """Untrusted semantic selection; never a root or canonical owner binding."""

    kind: OwnerKind
    project_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "project":
            if self.project_id is None or not self.project_id.strip():
                raise ValueError("project scope requires project_id")
        elif self.project_id is not None:
            raise ValueError("project_id is valid only for project scope")


@dataclass(frozen=True, slots=True)
class OwnerAuthorization:
    """Trusted, host-issued capability passed only to injected dependencies."""

    operation: BrainOperation
    access: AccessMode
    selector: OwnerSelector
    owner_id: str
    owner_locator: str
    policy_revision: str
    capability: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.owner_id, self.owner_locator, self.policy_revision)):
            raise ValueError("trusted authorization identity must be non-empty")


@dataclass(frozen=True, slots=True)
class ContextQuery:
    intent: str
    max_sources: int = 12
    max_source_tokens: int = 12_000


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    evidence_id: str
    owner_locator: str
    source_locator: str
    source_version: str
    content_sha256: str
    access_basis: str
    retrieval_method: str
    locator: str
    source_tokens: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class RetrievalExclusion:
    code: str
    reason: str
    count: int


@dataclass(frozen=True, slots=True)
class ContextResult:
    sources: tuple[SourceEvidence, ...]
    exclusions: tuple[RetrievalExclusion, ...]
    result_sha256: str
    observed_at: datetime
    freshness: Freshness
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """Truthful write outcome; validation is not remote acceptance."""

    operation_id: str
    status: Literal["validated", "accepted", "duplicate", "conflict", "refused", "unavailable"]
    owner_locator: str
    record_id: str | None
    version: str | None
    content_sha256: str | None
    receipt_sha256: str | None
    remote_observed: bool
    retryable: bool = False
    refusal_code: str | None = None
    durability_receipt: dict[str, object] | None = None
    transport_evidence: dict[str, object] | None = None
    word: str | None = None
    timing: dict[str, object] | None = None
    links: tuple[dict[str, object], ...] = ()
    calendar_suggestion: dict[str, object] | None = None
    state: str | None = None
    level: str | None = None
    record: dict[str, object] | None = None
    # ROA-11 (Wave 3): a new save's own near-duplicate check only - the record_id
    # of the most recent same-kind, same-owner, owner-worded record within 24h
    # of this save whose words match after normalizing, or None. Never set on a
    # replay (a save/change returning status "duplicate" of its own operation_id).
    duplicate_of: str | None = None
    # Ciel redesign N5: the saved item's {level, reason, proposed_by, confirmed}, or None
    # (normal, and every non-item).
    importance: dict[str, object] | None = None
    ignored: tuple[str, ...] = ()
    fix: str | None = None


@dataclass(frozen=True, slots=True)
class WriteReceiptWithCalendar(WriteReceipt):
    """A save/change receipt that also says what happened on Nic's calendar (N8; step E spec
    §5): ``calendar`` carries ``status`` (added, moved, on_calendar, ask_remove, has_guests or
    failed), ``text`` to show Nic, and the entry's time. ask_remove also carries ``question``.
    Built only while calendar entries are on and there is something to say, so every other
    receipt keeps exactly its old shape."""

    calendar: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WriteReceiptWithLength(WriteReceipt):
    """A save/change receipt that also names the task's length for the agent to say (step I
    section 6; build plan slice 5): ``length`` carries ``minutes``, ``planned_minutes``,
    ``estimated_by``, ``yours`` (Nic gave it) and ``text``, or ``status: needs_length`` with a
    question when a sorted task has none. Built only then, so every other receipt keeps its shape."""

    length: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WriteReceiptWithCalendarAndLength(WriteReceiptWithCalendar):
    """Both of the above: a calendar part and a length part."""

    length: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CalendarRemoveRequest:
    """Remove the entry Exosuit added for a done or dropped task, after Nic's yes (N8, G7).

    ``words`` is his exact reply; ``said_by`` must be owner and ``came_by`` direct: deleting
    is never an agent's own call."""

    operation_id: str
    record_id: str
    said_by: MemorySaidBy
    came_by: MemoryCameBy
    words: str


@dataclass(frozen=True, slots=True)
class CalendarRemoveResult:
    operation_id: str
    record_id: str
    status: Literal["removed", "not_found", "has_guests", "failed"]
    text: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuityStartRequest:
    """``task_id`` (N12b): the record ID of the task this note is for, whatever it is called."""

    operation_id: str
    session_id: str
    intent: str
    source_references: tuple[str, ...]
    next_step: str = "Continue the declared intent."
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuityAppendRequest:
    operation_id: str
    session_id: str
    event: ContinuityEvent
    value: str


@dataclass(frozen=True, slots=True)
class ContinuityRecoverRequest:
    max_tokens: int = 2_500
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuityListRequest:
    limit: int = 20


@dataclass(frozen=True, slots=True)
class ContinuitySummary:
    record_id: str
    session_id: str
    intent: str
    next_step: str
    state: Literal["open", "recovered"]
    updated_at: datetime
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuityListResult:
    items: tuple[ContinuitySummary, ...]


@dataclass(frozen=True, slots=True)
class ContinuityCloseRequest:
    operation_id: str
    session_id: str
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class ContinuityWriteResult:
    record_id: str
    session_id: str
    state: Literal["open", "closed", "recovered"]
    changed: bool
    updated_at: datetime
    receipt: WriteReceipt | None


@dataclass(frozen=True, slots=True)
class ContinuityRecovery:
    record_id: str
    session_id: str
    state: Literal["open", "recovered"]
    intent: str
    next_step: str
    updated_at: datetime
    tokenizer: str
    tokens: int
    sources: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    commitments: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    dropped: dict[str, int] = field(default_factory=dict)
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    operation_id: str
    choice: str
    title: str
    because: str
    forecloses: str
    governing_scope: str
    encoded_in: str
    completeness_check: str
    source_reference: str


class WorkExpectedRecord(TypedDict):
    """Host surface proof keyed by the lasting first ID; sha256 is Record.sha256."""

    record_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkRequest:
    """Public door request (plan 4.2); authenticated context is never an argument.

    ``message`` preserves Nic's words. ``answers`` references today's or yesterday's
    still-open why-log lines; the hub validates their target and offered moves.
    ``check_only`` is host metadata, excluded from the advertised tool schema.
    """

    operation_id: str
    message: str
    said_by: Literal["owner", "other"]
    came_by: Literal["direct", "connected_source"]
    moves: tuple[Mapping[str, object], ...]
    speaker: str | None = None
    answers: tuple[str, ...] = ()
    check_only: bool = False
    # Private surface metadata. Never accepted by the public MCP schema. None
    # stays out of the public operation hash so legacy exact retries survive.
    expected_records: Mapping[str, WorkExpectedRecord] | None = None


@dataclass(frozen=True, slots=True)
class MemorySaveRequest:
    """The v2 language: a word/level item, a sourced fact, or a confirmed preference.

    ``said_by``/``came_by``/``words``/``speaker`` describe whose words this is (language spec
    §2-3); the agent supplies them. ``source.recorded_by`` is never taken from here - the host
    stamps it from the authenticated caller (R10). ``words`` is required unless
    ``came_by: system``, and must be the exact excerpt Brain Core will match against evidence.

    ``importance`` (items only) proposes or, in Nic's own words, confirms high importance with
    ``importance_reason``; the host stamps its proposer from the caller (N5). ``focus_projects``
    (preferences only) names Nic's focus projects; his newest list wins (N4).
    """

    operation_id: str
    kind: MemoryKind
    title: str
    body: str
    source_reference: str
    said_by: MemorySaidBy
    came_by: MemoryCameBy
    words: str | None = None
    speaker: str | None = None
    dedupe_key: str | None = None
    level: str | None = None
    size: str | None = None
    owed_to: str | None = None
    repeat: str | None = None
    time_words: str | None = None
    observed_at: str | None = None
    valid_until: str | None = None
    links: tuple[dict[str, str], ...] = ()
    importance: MemoryImportance | None = None
    importance_reason: str | None = None
    focus_projects: tuple[str, ...] | None = None
    # Ciel and Brain Home PRD §3 (items only): the block type, the agent's rough length in
    # minutes (saved as its labelled guess), and a person to raise the item with.
    block: MemoryBlock | None = None
    rough_minutes: int | None = None
    # Step I §6: the number is Nic's own, in his words, so it is booked unpadded.
    length_is_nics: bool = False
    raise_with: str | None = None
    # PRD §4.1: the outcome the agent sorted it into, checked for its parts and never stored,
    # and a next action's first step.
    sort: MemorySort | None = None
    first_step: str | None = None
    # Set only by a whole-project save (PRD §4.10), never by a tool: a step waiting on another
    # starts blocked, and Nic's one approval covers every step, so a time phrase in it is never
    # read as any one step's date.
    initial_state: str | None = None
    find_time_words: bool = True


@dataclass(frozen=True, slots=True)
class ProjectStepRequest:
    """One step of a whole-project save (PRD §4.10): its own task. ``blocked_by`` names, by number
    from 1, the steps it waits for; a step waiting on none is a next action and needs a first step."""

    title: str
    body: str
    block: MemoryBlock
    rough_minutes: int
    first_step: str | None = None
    blocked_by: tuple[int, ...] = ()
    time_words: str | None = None
    owed_to: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectSaveRequest:
    """A goal and its steps, saved together on Nic's one approval (PRD §4.10): ``said_by`` owner,
    ``came_by`` direct, ``words`` his yes."""

    operation_id: str
    source_reference: str
    said_by: MemorySaidBy
    came_by: MemoryCameBy
    goal_title: str
    goal_body: str
    steps: tuple[ProjectStepRequest, ...]
    words: str | None = None
    speaker: str | None = None
    goal_time_words: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectSaveReceipt:
    """Every record of the project, or none: one commit (Accepted - Nic, 28 Sep 2026: "Nothing
    saved; retry"). ``status`` is ``accepted`` or, for a replay, ``duplicate``."""

    operation_id: str
    status: Literal["accepted", "duplicate"]
    owner_locator: str
    version: str
    goal: WriteReceipt
    steps: tuple[WriteReceipt, ...]
    remote_observed: bool = True


@dataclass(frozen=True, slots=True)
class MemoryChangeRequest:
    """One v2 change: state, item data, or links, checked against the record it read (R8).

    ``importance``: ``proposed`` (with ``importance_reason``) replaces any proposal, ``confirmed``
    confirms the current one (or, with a reason, states a new one) in Nic's words, and
    ``normal`` removes it (N5)."""

    operation_id: str
    record_id: str
    expected_sha256: str
    source_reference: str
    said_by: MemorySaidBy
    came_by: MemoryCameBy
    words: str | None = None
    speaker: str | None = None
    state: str | None = None
    level: str | None = None
    size: str | None = None
    owed_to: str | None = None
    repeat: str | None = None
    time_words: str | None = None
    reason: str | None = None
    check_locator: str | None = None
    checked_by: str | None = None
    parked_until: str | None = None
    links: tuple[dict[str, str], ...] = ()
    importance: MemoryImportance | None = None
    importance_reason: str | None = None
    block: MemoryBlock | None = None
    rough_minutes: int | None = None
    # Step I §6: the number is Nic's own, in his words, so it is booked unpadded.
    length_is_nics: bool = False
    raise_with: str | None = None
    sort: MemorySort | None = None
    first_step: str | None = None
    # PRD §4.7: Nic did a routine's latest occurrence; the routine itself stays open.
    occurrence: Literal["done"] | None = None


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    record_id: str
    kind: Literal["capture", "commitment", "item", "fact", "preference", "decision"]
    title: str
    body: str
    state: str
    owner_locator: str
    version: str
    content_sha256: str
    updated_at: datetime
    freshness: Freshness
    canonical_record: dict[str, object] | None = None
    word: str | None = None
    confirmed: bool = True
    superseded_by: tuple[str, ...] = ()
    importance: dict[str, object] | None = None


class ReaderNext(TypedDict):
    """A selected part/goal answer; omitted context repeats its enclosing now."""

    text: str
    item_ref: NotRequired[str]
    status: NotRequired[str]
    reason: NotRequired[str]
    until: NotRequired[str | None]
    children: NotRequired[tuple[ReaderNext | str, ...]]


class ReaderProgress(TypedDict):
    """Progress beyond a plain not-started count, with exact tick provenance."""

    text: str
    started: bool
    last_ticked: NotRequired[str]
    last_ticked_at: NotRequired[str]
    last_ticked_day: NotRequired[str]


class ReaderPart(TypedDict):
    """A part folded into its container's answer, from the same reader snapshot."""

    record_id: str
    title: str
    details: NotRequired[str]
    status: str
    next: str | ReaderNext


ReaderNow = TypedDict("ReaderNow", {
    "is": str,
    "under": str | None,
    "status": str,
    "next": str | ReaderNext,
    "why": str,
    "progress": str | ReaderProgress,
    "dates": dict[str, object],
    "labels": NotRequired[list[str]],
    "whose": NotRequired[str],
    "needs": NotRequired[dict[str, object]],
    "finish_line": NotRequired[str],
    "parent": NotRequired[str],
    "after": NotRequired[list[str]],
    "expected": NotRequired[dict[str, object]],
    "until": NotRequired[str],
    "stake": NotRequired[dict[str, object]],
    "parts": NotRequired[tuple[ReaderPart, ...]],
    "details": NotRequired[str],
})
ReaderNow.__doc__ = """W13's transient answer from the sole Brain Core work reader.

``is`` names the work kind; ``under`` names its parent, when present.
Status, next, why, progress and dates come from the reader's supplied snapshot.
This mapping is a public reply projection, never a stored record field.
"""


@dataclass(frozen=True, slots=True)
class MemoryRecordWithLinks(MemoryRecord):
    """A record that has links, with each link target shown at its newest version.

    A memory change gives a record a new ID while links on other records keep the old one.
    ``links`` follows the supersedes chain to the newest record, in the same {type, target}
    shape as a stored link. Only built for a record that has links, so every other reply
    keeps exactly its old shape. ``canonical_record`` stays exactly as stored, so the stored
    ID is still there.
    """

    links: tuple[dict[str, object], ...] = ()


# Nic, 30 Sep 2026: a record's body is the note from its first save and is never rewritten,
# while first_step, state and dates are what agents keep current. Read and search replies say
# so right next to body, so an agent does not answer from a stale body.
BODY_IS_NOTE = "original note from the first save; first_step, state and dates are current"


@dataclass(frozen=True, slots=True)
class MemoryRecordWithBodyNote(MemoryRecord):
    """A record whose body has fields beside it, with ``body_is`` saying which one wins.

    Built only when the body is non-empty and the record has a first_step or timing, so every
    other reply keeps exactly its old shape. ``body`` and ``canonical_record`` are
    untouched.
    """

    body_is: str = BODY_IS_NOTE


@dataclass(frozen=True, slots=True)
class MemoryRecordWithLinksAndBodyNote(MemoryRecordWithLinks):
    """Both of the above: resolved links and the ``body_is`` note."""

    body_is: str = BODY_IS_NOTE


@dataclass(frozen=True, slots=True)
class MemoryRecordWithNow(MemoryRecordWithLinksAndBodyNote):
    """W13 read/search interface; providers activate this projection separately.

Render ``now`` before raw fields and the body last, labelled ``first note, old``.
Current work replies name the lasting first ID; version/hash/canonical content
remain pinned to the actual raw version. Historical reads retain their identity.
The optional default leaves existing constructors and provider bodies unchanged.
"""

    now: ReaderNow | None = None
    body_is: str = "first note, old"

    def reader_reply_fields(self) -> dict[str, object]:
        """Wire projection for the W13 formatter; canonical data stays byte-derived."""
        return _reader_reply_fields(self)


@dataclass(frozen=True, slots=True)
class MemorySearchQuery:
    query: str
    limit: int = 10


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    matches: tuple[MemoryRecord, ...]
    version: str
    freshness: Freshness
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class MemoryListQuery:
    """Nic's accepted open items by default (P4); items only, facts have search.

    ``level`` is None when the caller didn't pass one: the provider then infers it from
    ``word`` (idea -> possible, suggestion -> suggested, otherwise accepted) rather than
    defaulting to "accepted" outright - level="accepted" would otherwise silently zero
    out word="idea"/"suggestion", since plain_word() only ever returns those two words
    for level possible/suggested respectively. An explicitly passed level (including
    "accepted") is used exactly as given, with no inference; a tuple of levels lists every
    one of them (speed-03).

    ``kind="preference"`` lists Nic's confirmed preferences instead (P1 of the memory
    follow-ons, Proposed - Nic to confirm): active preferences in his own words
    (``source.said_by`` owner), newest first. ``word``, ``state``, ``level`` and
    ``due_before`` filter items only and are not applied to them.
    """

    word: str | None = None
    state: tuple[str, ...] = ("open", "waiting", "blocked")
    level: str | tuple[str, ...] | None = None
    due_before: str | None = None
    limit: int = 20
    kind: Literal["item", "preference"] = "item"


@dataclass(frozen=True, slots=True)
class MemoryListItem:
    record_id: str
    word: str | None
    title: str
    state: str
    level: str | None
    timing: dict[str, object]
    owed_to: str | None
    links: tuple[dict[str, object], ...]
    updated_at: datetime
    confirmed: bool
    expected_sha256: str
    # Ciel redesign N5: the item's importance as saved, confirmed or not; None is normal.
    importance: dict[str, object] | None = None
    # Ciel and Brain Home PRD §3: the block type (None is unsorted) and who to raise it with;
    # §4.1: a next action's first step.
    block: str | None = None
    raise_with: str | None = None
    first_step: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryListItemWithAnswers(MemoryListItem):
    """A listed item with Nic's answers about it: ``{"record_id", "words"}``, oldest first (step
    E spec §3.1; Proposed - speed-04). Built only while ``[review]`` is on, and only for an item
    that has any, so every other list result keeps exactly its old shape."""

    answers: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryListItemWithNow(MemoryListItemWithAnswers):
    """W13 list interface: reader answer first, raw fields and answers after.

``record_id`` is the current thing's lasting first ID; ``expected_sha256``
still hashes its actual newest raw record. ``now`` is not stored or written.
This optional field is inactive until the provider's W13 behavior lands.
"""

    now: ReaderNow | None = None

    def reader_reply_fields(self) -> dict[str, object]:
        """Wire projection for the W13 formatter, preserving nonempty raw facts."""
        return _reader_reply_fields(self)


def _reader_reply_fields(value: MemoryRecordWithNow | MemoryListItemWithNow) -> dict[str, object]:
    """Answer first and old body last; omit absent/duplicated optional raw fields.

    Retired importance is not an effective work fact (plan 3.4), and remains
    available in a read's exact canonical_record. A raw word equal to the work
    kind, and a first_step equal to the next text, need no second copy. These
    omissions affect replies only, never the underlying dataclass or raw record.
    Listed titles may also be omitted when the full next text equals them
    exactly; read/search titles always remain explicit.
    """
    answer = value.now
    selected = answer.get("next") if answer is not None else None
    next_text = selected if isinstance(selected, str) else selected.get("text") if isinstance(selected, dict) else None
    result: dict[str, object] = {"now": answer}
    optional = {"word", "owed_to", "links", "timing", "block", "raise_with", "first_step",
                "canonical_record", "superseded_by", "answers", "importance"}
    for member in fields(value):
        name = member.name
        if name in {"now", "body", "body_is", "importance"}:
            continue
        current = getattr(value, name)
        if name in optional and current in (None, (), [], {}):
            continue
        if answer is not None:
            if name == "word" and current == answer["is"]:
                continue
            if name == "first_step" and current == next_text:
                continue
            if name == "title" and isinstance(value, MemoryListItemWithNow) and current == next_text:
                continue
        result[name] = current
    if isinstance(value, MemoryRecordWithNow):
        result["body_is"] = value.body_is
        result["body"] = value.body
    return result


@dataclass(frozen=True, slots=True)
class MemoryListResult:
    items: tuple[MemoryListItem, ...]
    version: str
    freshness: Freshness
    truncated: bool = False


@runtime_checkable
class BrainAuthorizer(Protocol):
    async def authorize(
        self,
        caller: BrainCaller,
        operation: BrainOperation,
        selector: OwnerSelector,
        access: AccessMode,
    ) -> OwnerAuthorization: ...


class ContextProvider(Protocol):
    async def retrieve(
        self, authorization: OwnerAuthorization, query: ContextQuery
    ) -> ContextResult: ...


class ContinuityProvider(Protocol):
    async def start(
        self, authorization: OwnerAuthorization, request: ContinuityStartRequest
    ) -> ContinuityWriteResult: ...

    async def append(
        self, authorization: OwnerAuthorization, request: ContinuityAppendRequest
    ) -> ContinuityWriteResult: ...

    async def recover(
        self, authorization: OwnerAuthorization, request: ContinuityRecoverRequest
    ) -> ContinuityRecovery: ...

    async def list_open(
        self, authorization: OwnerAuthorization, request: ContinuityListRequest
    ) -> ContinuityListResult: ...

    async def close(
        self, authorization: OwnerAuthorization, request: ContinuityCloseRequest
    ) -> ContinuityWriteResult: ...


class DecisionProvider(Protocol):
    async def record(
        self, authorization: OwnerAuthorization, request: DecisionRequest
    ) -> WriteReceipt: ...


class MemoryProvider(Protocol):
    async def work(self, authorization: OwnerAuthorization, request: WorkRequest) -> Mapping[str, object]: ...
    async def search(self, authorization: OwnerAuthorization, query: MemorySearchQuery) -> MemorySearchResult: ...

    async def save(
        self, authorization: OwnerAuthorization, request: MemorySaveRequest
    ) -> WriteReceipt: ...

    async def change(
        self, authorization: OwnerAuthorization, request: MemoryChangeRequest
    ) -> WriteReceipt: ...

    async def read(
        self, authorization: OwnerAuthorization, record_id: str
    ) -> MemoryRecord: ...

    async def list(
        self, authorization: OwnerAuthorization, query: MemoryListQuery
    ) -> MemoryListResult: ...


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


ResultT = TypeVar("ResultT")


class BrainRuntime:
    """Authorize an owner scope, then call exactly one typed dependency."""

    def __init__(
        self,
        *,
        authorizer: BrainAuthorizer,
        context: ContextProvider,
        continuity: ContinuityProvider,
        decisions: DecisionProvider,
        memory: MemoryProvider,
        managed_resources: Collection[AsyncCloseable] = (),
    ) -> None:
        self._authorizer = authorizer
        self._context = context
        self._continuity = continuity
        self._decisions = decisions
        self._memory = memory
        self._managed_resources = tuple(dict.fromkeys(map(id, managed_resources)))
        self._resource_by_id = {id(item): item for item in managed_resources}
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise BrainRuntimeError("brain.capability.unavailable", "Brain runtime is closed")

    async def _authorize(
        self,
        caller: BrainCaller,
        operation: BrainOperation,
        selector: OwnerSelector,
        access: AccessMode,
    ) -> OwnerAuthorization:
        self._require_open()
        authorization = await self._authorizer.authorize(caller, operation, selector, access)
        if not isinstance(authorization, OwnerAuthorization):
            raise BrainRuntimeError("brain.contract.invalid", "Trusted authorizer returned an invalid capability")
        if (
            authorization.operation is not operation
            or authorization.selector != selector
            or authorization.access != access
        ):
            raise BrainRuntimeError("brain.policy.excluded", "Trusted authorization does not match the operation")
        return authorization

    @staticmethod
    def _expect(value: object, expected: type[ResultT]) -> ResultT:
        if not isinstance(value, expected):
            raise BrainRuntimeError("brain.contract.invalid", "Brain dependency returned an invalid result")
        return value

    async def retrieve_context(
        self, caller: BrainCaller, selector: OwnerSelector, query: ContextQuery
    ) -> ContextResult:
        authorization = await self._authorize(caller, BrainOperation.CONTEXT_RETRIEVE, selector, "read")
        return self._expect(await self._context.retrieve(authorization, query), ContextResult)

    async def start_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityStartRequest
    ) -> ContinuityWriteResult:
        authorization = await self._authorize(caller, BrainOperation.CONTINUITY_START, selector, "write")
        return self._expect(await self._continuity.start(authorization, request), ContinuityWriteResult)

    async def append_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityAppendRequest
    ) -> ContinuityWriteResult:
        authorization = await self._authorize(caller, BrainOperation.CONTINUITY_APPEND, selector, "write")
        return self._expect(await self._continuity.append(authorization, request), ContinuityWriteResult)

    async def recover_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityRecoverRequest
    ) -> ContinuityRecovery:
        authorization = await self._authorize(caller, BrainOperation.CONTINUITY_RECOVER, selector, "read")
        return self._expect(await self._continuity.recover(authorization, request), ContinuityRecovery)

    async def list_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityListRequest
    ) -> ContinuityListResult:
        authorization = await self._authorize(caller, BrainOperation.CONTINUITY_LIST, selector, "read")
        return self._expect(await self._continuity.list_open(authorization, request), ContinuityListResult)

    async def close_continuity(
        self, caller: BrainCaller, selector: OwnerSelector, request: ContinuityCloseRequest
    ) -> ContinuityWriteResult:
        authorization = await self._authorize(caller, BrainOperation.CONTINUITY_CLOSE, selector, "write")
        return self._expect(await self._continuity.close(authorization, request), ContinuityWriteResult)

    async def record_decision(
        self, caller: BrainCaller, selector: OwnerSelector, request: DecisionRequest
    ) -> WriteReceipt:
        authorization = await self._authorize(caller, BrainOperation.DECISION_RECORD, selector, "write")
        return self._expect(await self._decisions.record(authorization, request), WriteReceipt)

    async def save_memory(
        self, caller: BrainCaller, selector: OwnerSelector, request: MemorySaveRequest
    ) -> WriteReceipt:
        authorization = await self._authorize(caller, BrainOperation.MEMORY_SAVE, selector, "write")
        return self._expect(await self._memory.save(authorization, request), WriteReceipt)

    async def change_memory(
        self, caller: BrainCaller, selector: OwnerSelector, request: MemoryChangeRequest
    ) -> WriteReceipt:
        authorization = await self._authorize(caller, BrainOperation.MEMORY_CHANGE, selector, "write")
        return self._expect(await self._memory.change(authorization, request), WriteReceipt)

    async def change_work(
        self, caller: BrainCaller, selector: OwnerSelector, request: WorkRequest
    ) -> Mapping[str, object]:
        """Authorize WORK and publish one atomic change through the gated provider.

        Check-only returns validated without pinning time or saving any state.
        Refusals name their field and fix; accepted replies stay under 2,000 chars.
        """
        authorization = await self._authorize(caller, BrainOperation.WORK, selector, "write")
        from .host_identity import bind_write_attribution
        with bind_write_attribution(caller.subject):
            return await self._memory.work(authorization, request)

    async def change_work_from_tap(
        self, caller: BrainCaller, selector: OwnerSelector, request: WorkRequest, *, authenticated_owner_id: str,
    ) -> Mapping[str, object]:
        """Host auth adapters only, after verifying Discord owner or dashboard session.

        No public tool exposes this method or accepts authenticated_owner_id.
        The provider repeats admission and binds the tap to the policy's owner.
        """
        from .surface_door import bind_owner_tap
        with bind_owner_tap(caller.subject, authenticated_owner_id, request):
            return await self.change_work(caller, selector, request)

    async def read_memory(
        self, caller: BrainCaller, selector: OwnerSelector, record_id: str
    ) -> MemoryRecord:
        authorization = await self._authorize(caller, BrainOperation.MEMORY_READ, selector, "read")
        return self._expect(await self._memory.read(authorization, record_id), MemoryRecord)

    async def search_memory(self, caller: BrainCaller, selector: OwnerSelector, query: MemorySearchQuery) -> MemorySearchResult:
        authorization = await self._authorize(caller, BrainOperation.MEMORY_SEARCH, selector, "read")
        return self._expect(await self._memory.search(authorization, query), MemorySearchResult)

    async def list_memory(self, caller: BrainCaller, selector: OwnerSelector, query: MemoryListQuery) -> MemoryListResult:
        authorization = await self._authorize(caller, BrainOperation.MEMORY_LIST, selector, "read")
        return self._expect(await self._memory.list(authorization, query), MemoryListResult)

    async def save_project(
        self, caller: BrainCaller, selector: OwnerSelector, request: ProjectSaveRequest
    ) -> ProjectSaveReceipt:
        """Refuse the retired project operation with the current capture/work route."""

        raise BrainRuntimeError("ACCESS_DENIED", "project.save has retired",
            field="operation", fix="use bc_work")

    async def remove_calendar_entry(
        self, caller: BrainCaller, selector: OwnerSelector, request: CalendarRemoveRequest
    ) -> CalendarRemoveResult:
        """Admit the door's participant, then authorize removal as memory.change."""
        self._require_open()
        if not work_caller_allowed(caller.subject,
            door_participant=getattr(self._memory, "door_participant", "hermes-wa")):
            raise BrainRuntimeError("ACCESS_DENIED", "This caller cannot remove Nic's calendar entry",
                field="caller", fix="remove it through Diablo")
        authorization = await self._authorize(caller, BrainOperation.MEMORY_CHANGE, selector, "write")
        remove = getattr(self._memory, "remove_calendar_entry", None)
        if remove is None:
            raise BrainRuntimeError("brain.capability.unavailable", "Calendar entries are not switched on")
        from .host_identity import bind_write_attribution
        with bind_write_attribution(caller.subject):
            return self._expect(await remove(authorization, request), CalendarRemoveResult)

    async def aclose(self) -> None:
        """Close only resources the host explicitly assigned to this runtime."""

        if self._closed:
            return
        self._closed = True
        failures: list[Exception] = []
        for resource_id in reversed(self._managed_resources):
            try:
                await self._resource_by_id[resource_id].aclose()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Brain runtime cleanup failed", failures)

    async def __aenter__(self) -> "BrainRuntime":
        self._require_open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
