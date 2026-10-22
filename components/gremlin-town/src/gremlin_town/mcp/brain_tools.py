"""Narrow ``bc_*`` MCP tools over a host-constructed :class:`BrainRuntime`, plus step F's
``bc_orient``/``bc_next``/``bc_next_respond`` over an :class:`OrientationContext` (step F spec
§8) - a separate dependency shape, so ``build_orientation_tools`` is a second, parallel factory
reusing this module's error mapping and strict-argument helpers rather than folding orientation
into ``BrainRuntime`` itself."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

import anyio
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from brain_core.presentation import work_words
from brain_core.github_storage import GitHubStorageError, GitHubTransportError

from gremlin_town.api.serialization import to_jsonable
from gremlin_town.contracts import Actor
from gremlin_town.mcp.brain_usage import BrainUsageEvent, BrainUsageLog
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation,
    BrainRuntime,
    BrainRuntimeError,
    CalendarRemoveRequest,
    ContextQuery,
    ContinuityAppendRequest,
    ContinuityCloseRequest,
    ContinuityListRequest,
    ContinuityRecoverRequest,
    ContinuityStartRequest,
    DecisionRequest,
    MemoryChangeRequest,
    MemoryListQuery,
    MemorySaveRequest,
    MemorySearchQuery,
    OwnerSelector,
    WorkRequest,
    WriteReceipt,
)

if TYPE_CHECKING:
    from gremlin_town.review.orient import OrientationContext

logger = logging.getLogger(__name__)

ProjectId = Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
VaultProjectId = Annotated[
    StrictStr,
    Field(pattern=r"^vault_[a-z0-9][a-z0-9_]*$", max_length=128),
]
OperationId = Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")]
OpaqueReference = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
RecordId = Annotated[StrictStr, Field(pattern=r"^brn_\d{8}_\d{6}_[0-9a-f]{4,16}$")]
# N12b: a note names the task it belongs to, so it is found whatever the note is called.
NoteTaskId = Annotated[
    StrictStr,
    Field(
        pattern=r"^brn_\d{8}_\d{6}_[0-9a-f]{4,16}$",
        description="The task's record ID; pass it when the note is for a task",
    ),
]
Body = Annotated[StrictStr, Field(min_length=1, max_length=65_536)]
ShortText = Annotated[StrictStr, Field(min_length=1, max_length=8_192)]
SourceReferences = Annotated[list[OpaqueReference], Field(max_length=50)]
OwnerScope = Literal["personal", "cross-project", "system", "fleet", "project"]
ContinuityEventInput = Literal[
    "source", "effect", "decision", "commitment", "candidate", "blocker", "intent", "next_step"
]
MemoryKindInput = Literal["item", "fact", "preference"]
ListKindInput = Literal["item", "preference"]
SaidByInput = Literal["owner", "agent", "other"]
CameByInput = Literal["direct", "agent_guess", "connected_source", "system"]
SaidBy = Annotated[
    SaidByInput,
    Field(description="Who said the words: owner, agent, or other."),
]
CameBy = Annotated[
    CameByInput,
    Field(
        description=(
            "How the words reached the server: direct, agent_guess, connected_source, or "
            "system. The server refuses a value other than direct when said_by is owner."
        )
    ),
]
SourceWords = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=8_192,
        description=(
            "The exact words, quoted verbatim. The server refuses a missing value unless "
            "came_by is system."
        ),
    ),
]
SourceSpeaker = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=8_192,
        description=(
            "Who spoke the words. The server refuses a missing value when said_by is agent "
            "or other."
        ),
    ),
]
ItemLevelInput = Literal["possible", "suggested", "accepted"]
ItemSizeInput = Literal["task", "goal"]
ItemStateInput = Literal[
    "open", "waiting", "blocked", "parked", "done", "dropped", "no_longer_needed", "current", "outdated"
]
MemoryWordInput = Literal["idea", "suggestion", "task", "promise", "routine", "goal"]
DateOnly = Annotated[StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
ListLevelInput = Annotated[
    ItemLevelInput | Annotated[list[ItemLevelInput], Field(min_length=1, max_length=3)],
    Field(
        description=(
            "Filter to this exact level, or to several: [\"accepted\", \"suggested\"] lists tasks "
            "and suggestions in one call. Omit to infer it from word (idea -> possible, "
            "suggestion -> suggested, otherwise accepted); omitting both word and level "
            "lists accepted items, per the default."
        )
    ),
]
LinkTypeInput = Literal["part_of", "waiting_on", "blocked_by", "handed_to", "about", "replaces", "came_from"]
# Ciel redesign N5 and N4 (docs/product/steps/c-record-format.md §13).
ImportanceInput = Literal["proposed", "confirmed", "real_cost", "minor", "normal"]
# Ciel and Brain Home PRD §3: the time block a task belongs in, and an agent's rough length.
BlockInput = Literal["deep_focus", "admin", "errands"]
RoughMinutes = Annotated[StrictInt, Field(ge=1, le=1440)]
# PRD §4.1: the outcome an agent sorts a new item into, and a next action's first step.
SortInput = Literal["next_action", "waiting_on", "raise_with", "someday", "reference", "drop"]
FirstStep = Annotated[StrictStr, Field(min_length=1, max_length=256, pattern=r"^[^\r\n]+$")]
FocusProjects = Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=128)]], Field(max_length=10)]


class MemoryLinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: LinkTypeInput
    target: Annotated[StrictStr, Field(min_length=1, max_length=512)]


MemoryLinks = Annotated[list[MemoryLinkInput], Field(max_length=20)]


class WorkReadingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: DateOnly
    at: ShortText | None = None
    end: ShortText | None = None


class WorkStakeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Literal["changes_life", "hurts_weeks", "nuisance", "nothing_much"] | None = None
    at_stake: ShortText | None = None
    odds: ShortText | None = None
    cost_shape: Literal["grows", "lands"] | None = None


class WorkPartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: ShortText
    details: ShortText | None = None
    then: StrictBool | None = None
    done: StrictBool | None = None
    when: ShortText | None = None
    reading: WorkReadingInput | None = None
    event: ShortText | None = None
    whose: ShortText | None = None
    block: BlockInput | None = None
    rough_minutes: RoughMinutes | None = None
    length_is_nics: StrictBool | None = None


class WorkMoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    move: Literal["new", "start", "tick", "parts", "next_action", "wait", "date", "end", "park", "organise", "undo", "stake"]
    thing: ShortText | None = None
    words: SourceWords | None = None
    title: ShortText | None = None
    details: ShortText | None = None
    level: ShortText | None = None
    owed_to: ShortText | None = None
    repeat: ShortText | None = None
    when: ShortText | None = None
    reading: WorkReadingInput | None = None
    label: ShortText | None = None
    block: BlockInput | None = None
    rough_minutes: RoughMinutes | None = None
    length_is_nics: StrictBool | None = None
    unclear: StrictBool | None = None
    looks_like: Literal["goal", "project"] | None = None
    stake: WorkStakeInput | None = None
    ask_stake: StrictBool | None = None
    what: ShortText | None = None
    parts: Annotated[list[WorkPartInput], Field(min_length=1, max_length=20)] | None = None
    text: ShortText | None = None
    kind: Literal["after", "theirs", "needs", "not_before"] | None = None
    after: Annotated[list[ShortText], Field(max_length=20)] | None = None
    who: ShortText | None = None
    expected: ShortText | None = None
    event: ShortText | None = None
    until: ShortText | None = None
    replaces: ShortText | None = None
    proposed: StrictBool | None = None
    ending: Literal["dropped", "no_longer_needed"] | None = None
    reason: ShortText | None = None
    called: Literal["goal", "project", "task"] | None = None
    under: ShortText | None = None
    order: StrictInt | None = None
    finish_line: ShortText | None = None
    merge_into: ShortText | None = None
    accept: StrictBool | None = None
    keep: ShortText | None = None
    change: OperationId | None = None
    at_stake: ShortText | None = None
    odds: ShortText | None = None
    cost_shape: Literal["grows", "lands"] | None = None
    confirm: StrictBool | None = None


YesWords = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=8_192,
        description="Nic's exact reply to \"Remove it from your calendar?\", quoted verbatim.",
    ),
]

# bc_calendar_remove (N8, rebuild plan G7): registered only while calendar entries are on.
_CALENDAR_REMOVE_TEXT = (
    "Remove the calendar entry Exosuit added for a task that is now done or dropped, only after "
    "Nic has just said yes to \"Remove it from your calendar?\" (the question that change's "
    "receipt asks). Deleting needs his fresh yes every time, never your own judgement. record_id "
    "is the done or dropped version from that receipt; said_by owner, came_by direct, words his "
    "exact reply. It removes only the entry carrying that task's exosuit marker, never one with "
    "guests. Keep operation_id; after an uncertain response, replay the same input. "
)


class _StrictArguments(FuncMetadata):
    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        return data


# Keyword values that are NAME MAPS: every key is a property/def NAME, never a schema
# keyword, so it must never be filtered by name (a parameter can legitimately be called
# "title" - bc_memory_save and bc_decision_record both have one). Only each map's VALUES
# are schemas to slim.
_SCHEMA_NAME_MAP_KEYWORDS = frozenset({"properties", "$defs", "definitions", "patternProperties"})
# Keyword values that are LISTS OF SCHEMAS.
_SCHEMA_LIST_KEYWORDS = frozenset({"anyOf", "allOf", "oneOf"})
# Keyword values that are DATA, not schemas - never recurse into them (a default value,
# an enum member, or an example could themselves be dicts with a key that collides with
# a schema keyword, e.g. a default value shaped like {"title": "..."}; that "title" is
# data, not a schema's own metadata, and must survive untouched).
_SCHEMA_DATA_KEYWORDS = frozenset({"default", "enum", "const", "examples"})


def _slim_schema(schema: Any) -> Any:
    """Strip presentational JSON-schema bloat pydantic v2 always adds, that no client
    acts on and no test inspects: every auto-generated "title" (redundant with the
    property/tool name already being the JSON key), a "default": null next to a field
    that already being absent from "required" marks optional, and the "anyOf" wrapper a
    two-branch Optional[...] union adds around its real type (the null branch says
    nothing "required" doesn't already say). Leaves type, enum, pattern,
    minLength/maxLength, minimum/maximum, items, $ref/$defs, additionalProperties and
    required exactly alone. This only changes the schema advertised to a client -
    ``tool.fn_metadata.arg_model`` still validates every call exactly as before.

    Bug fixed here: an earlier version dropped every dict key literally named "title" at
    every depth, including inside "properties" - where keys are parameter NAMES, not
    schema keywords. That silently deleted the `title` parameter's own entry from
    bc_memory_save/bc_decision_record's advertised properties (while `required` and
    `additionalProperties: false` still demanded it), so a strict client could no longer
    construct a valid call. "title" is stripped only where it is actually a schema
    keyword - never as a key of a name map, and never inside a data-valued keyword like
    "default" or "enum"."""
    if isinstance(schema, dict):
        cleaned: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "title":
                continue
            if key == "default" and value is None:
                continue
            if key in _SCHEMA_NAME_MAP_KEYWORDS and isinstance(value, dict):
                cleaned[key] = {name: _slim_schema(subschema) for name, subschema in value.items()}
            elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
                cleaned[key] = [_slim_schema(item) for item in value]
            elif key == "items":
                cleaned[key] = _slim_schema(value)
            elif key in _SCHEMA_DATA_KEYWORDS:
                cleaned[key] = value
            else:
                cleaned[key] = value
        any_of = cleaned.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2:
            null_branches = [b for b in any_of if isinstance(b, dict) and b.get("type") == "null"]
            other = [b for b in any_of if b not in null_branches]
            if len(null_branches) == 1 and len(other) == 1:
                merged = dict(cleaned)
                del merged["anyOf"]
                merged.update(other[0])
                return merged
        return cleaned
    if isinstance(schema, list):
        return [_slim_schema(item) for item in schema]
    return schema


def _result(value: object, *, error: bool = False) -> CallToolResult:
    data = to_jsonable(value)
    if isinstance(value, WriteReceipt):
        # The single-release Python bridge retains its argument/receipt type;
        # the retired calendar suggestion is absent from public replies.
        data.pop("calendar_suggestion", None)
    return CallToolResult(
        structuredContent=data,
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, allow_nan=False))],
        isError=error,
    )


def _error(code: str, message: str, *, retryable: bool = False,
           field: str = "request", fix: str = "correct the request and send it again") -> CallToolResult:
    field = field or "request"
    fix = fix or "correct the request and send it again"
    return _result({"error": {"code": code, "message": message, "retryable": retryable,
                              "field": field, "fix": fix}}, error=True)


def _safe_code(value: object) -> str | None:
    raw = getattr(value, "value", value)
    if isinstance(raw, str) and re.fullmatch(r"(?:brain\.[a-z0-9_.-]+|ACCESS_DENIED)", raw):
        return raw
    return None


def _mapped_error(exc: Exception, *, nothing_saved: bool = False) -> CallToolResult:
    """``nothing_saved``: the tool writes (a yes to a pick starts a note), so a size limit or the used-up
    allowance says nothing was saved, never only that nothing is lost."""

    code = _safe_code(getattr(exc, "code", None))
    denied = isinstance(exc, PermissionError) or code == "ACCESS_DENIED" or (
        code is not None and (code.startswith("brain.grant.") or code == "brain.policy.excluded")
    )
    if denied:
        return _error("ACCESS_DENIED", exc.message if isinstance(exc, BrainRuntimeError) and code == "ACCESS_DENIED"
                      else "resource unavailable or access denied",
                      field=getattr(exc, "field", "caller"),
                      fix=getattr(exc, "fix", "use an authorized participant and owner"))
    if isinstance(exc, GitHubStorageError):
        # A raw storage failure (the note a yes starts reaches Brain Home's storage directly). Logged, as
        # every unexpected failure here is. A size limit, GitHub's used-up allowance or GitHub being
        # unreachable is mapped the way the memory tools map it (brain.capacity.exceeded or retryable
        # brain.capability.unavailable); anything else stays INTERNAL_ERROR (the storage-limit plan,
        # layer 1).
        from gremlin_town.mcp.github_brain_provider import map_storage_error
        from gremlin_town.review.brain_source import brain_problem

        logger.error("Brain MCP operation failed on Brain Home storage (%s %s)", type(exc).__name__, exc.code)
        if brain_problem(exc) is None and not isinstance(exc, GitHubTransportError):
            return _error("INTERNAL_ERROR", "Brain operation failed")
        exc = map_storage_error(exc, reading=True, nothing_saved=nothing_saved)
    if isinstance(exc, BrainRuntimeError):
        return _error(exc.code, exc.message, retryable=exc.retryable,
                      field=exc.field, fix=exc.fix)
    if code is not None:
        message = getattr(exc, "message", None)
        return _error(code, message if isinstance(message, str) else "Brain operation failed",
                      field=getattr(exc, "field", None) or getattr(exc, "details", {}).get("field", "request"),
                      fix=getattr(exc, "fix", None) or getattr(exc, "hint", None)
                      or "correct the request and send it again")
    if isinstance(exc, ValueError):
        return _error("brain.invalid_argument", str(exc))
    logger.error("Brain MCP operation failed (%s)", type(exc).__name__)
    return _error("INTERNAL_ERROR", "Brain operation failed")


_READ_SCOPE = "bc:read"
_WRITE_SCOPE = "bc:write"
_RETRY = (
    "Choose and retain operation_id before calling. After an uncertain response, replay with "
    "exactly the same input, never a replacement key. Returns a receipt: saved means status "
    "accepted or duplicate with remote_observed true, naming the record in record_id. "
)
# _RUNNING_NOTE: kept in full on bc_continuity_start only (and in Diablo's SKILL.md) - the
# other four continuity tools drop it, since a client reads every tool's description
# together and the protocol only needs stating once.
_RUNNING_NOTE = (
    "Start this task's note (session_id gt-<request_id> for work handed through Gremlin Town) "
    "when work begins, and append next_step, and blocker or effect as they happen, after every "
    "meaningful step so it is current however the chat ends. Close it only when the task is done "
    "and checked. When asked what is still open, read the matching note for each open request. "
)


def build_brain_tools(
    runtime: BrainRuntime,
    actor_provider: Callable[[], Actor],
    *,
    scopes_provider: Callable[[], Collection[str]],
    authorize: Callable[[], Awaitable[None]],
    concurrency_limit: int = 4,
    usage_log: BrainUsageLog | None = None,
    transport: str = "mcp",
    door: bool = False,
    calendar_removal: bool = False,
    project_saves: bool = False,
) -> list[Tool]:
    """Build the separate Brain Core tool family for lead-owned composition.

    Production composition must supply per-call authentication revalidation and
    verified OAuth scopes.  The runtime separately resolves and authorizes the
    selected owner before it calls any injected body provider.

    ``door`` includes the HTTP-only work door. Calendar removal also requires
    ``calendar_removal``. ``project_saves`` is a retired compatibility keyword:
    project.save is never registered.
    """

    if isinstance(concurrency_limit, bool) or not isinstance(concurrency_limit, int) or concurrency_limit < 1:
        raise ValueError("concurrency_limit must be a positive integer")
    limiter = anyio.CapacityLimiter(concurrency_limit)

    async def invoke(
        operation: BrainOperation,
        *,
        write: bool,
        call: Callable[[BrainCaller], Awaitable[object]],
        operation_id: str | None = None,
        logged_as: str | None = None,
    ) -> CallToolResult:
        started = time.monotonic()
        result = await _invoke(write=write, call=call)
        if usage_log is not None:
            data = result.structuredContent if isinstance(result.structuredContent, dict) else {}
            error = data.get("error") if isinstance(data.get("error"), dict) else None
            try:
                actor_id = actor_provider().actor_id
            except Exception:
                actor_id = "unauthenticated"
            usage_log.record(BrainUsageEvent(
                actor=actor_id,
                transport=transport,
                operation=logged_as or operation.value,
                outcome="error" if error else "ok",
                error_code=error.get("code") if error else None,
                duration_ms=round((time.monotonic() - started) * 1000),
                operation_id=operation_id,
                record_id=data.get("record_id") if isinstance(data.get("record_id"), str) else None,
            ))
        return result

    async def _invoke(
        *,
        write: bool,
        call: Callable[[BrainCaller], Awaitable[object]],
    ) -> CallToolResult:
        try:
            await authorize()
            actor = actor_provider()
            if not isinstance(actor, Actor) or not actor.actor_id or actor.is_operator:
                return _error("ACCESS_DENIED", "participant authentication required")
            scopes = frozenset(scopes_provider())
            if _READ_SCOPE not in scopes or (write and _WRITE_SCOPE not in scopes):
                return _error("ACCESS_DENIED", "required Brain Core scope not granted")
            caller = BrainCaller(actor.actor_id, scopes)
            from gremlin_town.mcp.host_identity import bind_write_attribution
            async with limiter:
                await authorize()
                with bind_write_attribution(actor.actor_id):
                    value = await call(caller)
            return _result(value)
        except Exception as exc:
            return _mapped_error(exc)

    async def bc_context_retrieve(
        project_id: VaultProjectId,
        intent: ShortText,
        max_sources: Annotated[StrictInt, Field(ge=1, le=100)] = 12,
        max_source_tokens: Annotated[StrictInt, Field(ge=1, le=12_000)] = 12_000,
    ) -> CallToolResult:
        selector = OwnerSelector("project", project_id)
        return await invoke(
            BrainOperation.CONTEXT_RETRIEVE,
            write=False,
            call=lambda caller: runtime.retrieve_context(
                caller, selector, ContextQuery(intent, max_sources, max_source_tokens)
            ),
        )

    async def bc_continuity_start(
        operation_id: OperationId,
        session_id: OpaqueReference,
        intent: ShortText,
        source_references: SourceReferences,
        project_id: VaultProjectId | None = None,
        scope: OwnerScope | None = None,
        next_step: ShortText = "Continue the declared intent.",
        task_id: NoteTaskId | None = None,
    ) -> CallToolResult:
        selector = OwnerSelector(scope or ("project" if project_id else "personal"), project_id)
        return await invoke(
            BrainOperation.CONTINUITY_START,
            write=True,
            operation_id=operation_id,
            call=lambda caller: runtime.start_continuity(
                caller,
                selector,
                ContinuityStartRequest(
                    operation_id, session_id, intent, tuple(source_references), next_step, task_id
                ),
            ),
        )

    async def bc_continuity_append(
        operation_id: OperationId,
        session_id: OpaqueReference,
        event: ContinuityEventInput,
        value: ShortText,
        project_id: VaultProjectId | None = None,
        scope: OwnerScope | None = None,
    ) -> CallToolResult:
        selector = OwnerSelector(scope or ("project" if project_id else "personal"), project_id)
        return await invoke(
            BrainOperation.CONTINUITY_APPEND,
            write=True,
            operation_id=operation_id,
            call=lambda caller: runtime.append_continuity(
                caller, selector, ContinuityAppendRequest(operation_id, session_id, event, value)
            ),
        )

    async def bc_continuity_recover(
        project_id: VaultProjectId | None = None,
        scope: OwnerScope | None = None,
        session_id: OpaqueReference | None = None,
        max_tokens: Annotated[StrictInt, Field(ge=80, le=12_000)] = 2_500,
    ) -> CallToolResult:
        selector = OwnerSelector(scope or ("project" if project_id else "personal"), project_id)
        return await invoke(
            BrainOperation.CONTINUITY_RECOVER,
            write=False,
            call=lambda caller: runtime.recover_continuity(
                caller, selector, ContinuityRecoverRequest(max_tokens, session_id)
            ),
        )

    async def bc_continuity_list(
        project_id: VaultProjectId | None = None,
        scope: OwnerScope | None = None,
        limit: Annotated[StrictInt, Field(ge=1, le=100)] = 20,
    ) -> CallToolResult:
        selector = OwnerSelector(scope or ("project" if project_id else "personal"), project_id)
        return await invoke(
            BrainOperation.CONTINUITY_LIST,
            write=False,
            call=lambda caller: runtime.list_continuity(
                caller, selector, ContinuityListRequest(limit)
            ),
        )

    async def bc_continuity_close(
        operation_id: OperationId,
        session_id: OpaqueReference,
        project_id: VaultProjectId | None = None,
        scope: OwnerScope | None = None,
        recovered: StrictBool = False,
    ) -> CallToolResult:
        selector = OwnerSelector(scope or ("project" if project_id else "personal"), project_id)
        return await invoke(
            BrainOperation.CONTINUITY_CLOSE,
            write=True,
            operation_id=operation_id,
            call=lambda caller: runtime.close_continuity(
                caller, selector, ContinuityCloseRequest(operation_id, session_id, recovered)
            ),
        )

    async def bc_decision_record(
        operation_id: OperationId,
        scope: OwnerScope,
        title: ShortText,
        choice: ShortText,
        because: ShortText,
        forecloses: ShortText,
        governing_scope: ShortText,
        encoded_in: ShortText,
        completeness_check: ShortText,
        source_reference: OpaqueReference | None = None,
        project_id: ProjectId | None = None,
    ) -> CallToolResult:
        return await invoke(
            BrainOperation.DECISION_RECORD,
            write=True,
            operation_id=operation_id,
            call=lambda caller: runtime.record_decision(
                caller,
                OwnerSelector(scope, project_id),
                DecisionRequest(
                    operation_id,
                    choice,
                    title,
                    because,
                    forecloses,
                    governing_scope,
                    encoded_in,
                    completeness_check,
                    source_reference or "mcp-request:" + hashlib.sha256(operation_id.encode()).hexdigest(),
                ),
            ),
        )

    async def bc_memory_save(
        operation_id: OperationId,
        scope: OwnerScope,
        kind: MemoryKindInput,
        title: ShortText,
        body: Body,
        said_by: SaidBy,
        came_by: CameBy,
        words: SourceWords | None = None,
        speaker: SourceSpeaker | None = None,
        source_reference: OpaqueReference | None = None,
        project_id: ProjectId | None = None,
        dedupe_key: OpaqueReference | None = None,
        level: ItemLevelInput | None = None,
        size: ItemSizeInput | None = None,
        owed_to: ShortText | None = None,
        repeat: ShortText | None = None,
        time_words: ShortText | None = None,
        observed_at: ShortText | None = None,
        valid_until: ShortText | None = None,
        links: MemoryLinks = [],
        importance: ImportanceInput | None = None,
        importance_reason: ShortText | None = None,
        focus_projects: FocusProjects | None = None,
        block: BlockInput | None = None,
        rough_minutes: RoughMinutes | None = None,
        length_is_nics: StrictBool = False,
        raise_with: ShortText | None = None,
        sort: SortInput | None = None,
        first_step: FirstStep | None = None,
    ) -> CallToolResult:
        return await invoke(
            BrainOperation.MEMORY_SAVE,
            write=True,
            operation_id=operation_id,
            call=lambda caller: runtime.save_memory(
                caller,
                OwnerSelector(scope, project_id),
                MemorySaveRequest(
                    operation_id, kind, title, body,
                    source_reference or "mcp-request:" + hashlib.sha256(operation_id.encode()).hexdigest(),
                    said_by=said_by, came_by=came_by, words=words, speaker=speaker, dedupe_key=dedupe_key,
                    level=level, size=size, owed_to=owed_to, repeat=repeat, time_words=time_words,
                    observed_at=observed_at, valid_until=valid_until,
                    links=tuple({"type": item.type, "target": item.target} for item in links),
                    importance=importance, importance_reason=importance_reason,
                    focus_projects=None if focus_projects is None else tuple(focus_projects),
                    block=block, rough_minutes=rough_minutes, length_is_nics=length_is_nics,
                    raise_with=raise_with, sort=sort, first_step=first_step,
                ),
            ),
        )

    async def bc_memory_change(
        operation_id: OperationId,
        scope: OwnerScope,
        record_id: RecordId,
        expected_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")],
        said_by: SaidBy,
        came_by: CameBy,
        words: SourceWords | None = None,
        speaker: SourceSpeaker | None = None,
        source_reference: OpaqueReference | None = None,
        project_id: ProjectId | None = None,
        state: Literal["current", "outdated", "proposed", "active", "retired"] | None = None,
        reason: ShortText | None = None,
    ) -> CallToolResult:
        return await invoke(
            BrainOperation.MEMORY_CHANGE,
            write=True,
            operation_id=operation_id,
            call=lambda caller: runtime.change_memory(
                caller,
                OwnerSelector(scope, project_id),
                MemoryChangeRequest(
                    operation_id, record_id, expected_sha256,
                    source_reference or "mcp-request:" + hashlib.sha256(operation_id.encode()).hexdigest(),
                    said_by=said_by, came_by=came_by, words=words, speaker=speaker,
                    state=state, reason=reason,
                ),
            ),
        )

    async def bc_work(
        operation_id: OperationId,
        message: Body,
        said_by: Literal["owner", "other"],
        came_by: Literal["direct", "connected_source"],
        moves: Annotated[list[WorkMoveInput], Field(min_length=1, max_length=20)],
        speaker: SourceSpeaker | None = None,
        answers: Annotated[list[OpaqueReference], Field(max_length=20)] = [],
    ) -> CallToolResult:
        from gremlin_town.mcp.brain_runtime import work_check_only
        return await invoke(
            BrainOperation.WORK, write=True, operation_id=operation_id,
            call=lambda caller: runtime.change_work(
                caller, OwnerSelector("personal"), WorkRequest(
                    operation_id=operation_id, message=message, said_by=said_by, came_by=came_by,
                    moves=tuple(move.model_dump(exclude_none=True) for move in moves),
                    speaker=speaker, answers=tuple(answers), check_only=work_check_only.get(),
                ),
            ),
        )

    async def bc_memory_read(
        scope: OwnerScope,
        record_id: RecordId,
        project_id: ProjectId | None = None,
    ) -> CallToolResult:
        return await invoke(
            BrainOperation.MEMORY_READ,
            write=False,
            call=lambda caller: runtime.read_memory(
                caller, OwnerSelector(scope, project_id), record_id
            ),
        )

    async def bc_memory_search(
        query: ShortText,
        scope: OwnerScope = "personal",
        limit: Annotated[StrictInt, Field(ge=1, le=20)] = 10,
        project_id: ProjectId | None = None,
    ) -> CallToolResult:
        return await invoke(
            BrainOperation.MEMORY_SEARCH, write=False,
            call=lambda caller: runtime.search_memory(caller, OwnerSelector(scope, project_id), MemorySearchQuery(query, limit)),
        )

    async def bc_memory_list(
        scope: OwnerScope = "personal",
        project_id: ProjectId | None = None,
        word: MemoryWordInput | None = None,
        state: Annotated[list[ItemStateInput], Field(min_length=1, max_length=9)] = ["open", "waiting", "blocked"],
        level: ListLevelInput | None = None,
        due_before: DateOnly | None = None,
        limit: Annotated[StrictInt, Field(ge=1, le=50)] = 20,
        kind: ListKindInput = "item",
    ) -> CallToolResult:
        return await invoke(
            BrainOperation.MEMORY_LIST, write=False,
            call=lambda caller: runtime.list_memory(
                caller,
                OwnerSelector(scope, project_id),
                MemoryListQuery(
                    word=word, state=tuple(state), level=tuple(level) if isinstance(level, list) else level,
                    due_before=due_before, limit=limit, kind=kind,
                ),
            ),
        )

    async def bc_calendar_remove(
        operation_id: OperationId,
        record_id: RecordId,
        said_by: SaidBy,
        came_by: CameBy,
        words: YesWords,
        scope: Literal["personal", "cross-project"] = "personal",
    ) -> CallToolResult:
        return await invoke(
            BrainOperation.MEMORY_CHANGE,
            write=True,
            operation_id=operation_id,
            logged_as="calendar.remove",
            call=lambda caller: runtime.remove_calendar_entry(
                caller,
                OwnerSelector(scope),
                CalendarRemoveRequest(operation_id, record_id, said_by, came_by, words),
            ),
        )

    _LANGUAGE = (
        "Save new things as said; changes to Nic's work go through Diablo or the morning review. "
        "time_words is only the time part in his words; never invent a date. words is what Nic "
        "said about the thing, never your instruction to save it. His own direct capture is accepted; "
        "other people's words stay suggestions. Brain Home holds anything Nic would be "
        "hurt to lose or have done late (tasks, promises, dates, people he waits on, decisions); "
        "your own memory holds only how you work, and Brain wins a clash. "
    )
    _WORK = (
        "Change Nic's work through Diablo only, in one commit or none. message is his work message "
        "word for word; never invent words. moves is one to twenty of new, start, tick, parts, "
        "next_action, wait, date, end, park, organise, undo or stake. thing is any version's record "
        "ID, a title, or #n for the nth new thing in this change. A title must match one open thing; "
        "if several match, ask which one. New captures are saved as said; other people's words stay "
        "suggestions and their other moves are dropped. Titles and next actions not in his words "
        "get review marks; do not silently rewrite them. A guessed block and length stay labelled "
        "guesses; length_is_nics only for his own number. Dates need his actual when phrase; code "
        "reads the kind and day, reading only helps when code cannot. never invent a date or person. "
        "Owner-only moves need words quoting message: end, park, undo, tick of a thing, confirming "
        "a stake, or organise with called, accept, keep, title, merge_into or his own length/block. "
        "A bare yes, number, save it or drop it carries no decision alone. answers names today's "
        "or yesterday's still-open review lines whose question offered that move on that thing; "
        "then his reply carries that decision, including a plain yes. Other moves still need quotes. "
        "parts adds his ordered parts, then waits on the previous part; a subtask holds no parts. "
        "new/parts details quote message; show indented under the part, never in morning lists. "
        "tick next_action finishes just that action; tick thing ends the thing, while a routine "
        "records an occurrence and stays open. end needs ending and a reason when dropped. organise "
        "places or names it, accept raises a suggestion, keep clears a mark or confirms a date. "
        "Only Nic's yes confirms stake; propose level and at_stake first. undo names this caller's "
        "change; changed-since records refuse, so send a correction as a new change. No more than "
        "25 output records: split it if refused. Dropped moves leave their question for the review. "
        "Keep operation_id and replay exactly the same input after an uncertain reply. accepted "
        "or duplicate means the entire change landed; validated means nothing was saved. "
    )
    definitions = [
        # Its last sentence is Ciel redesign N11 (rebuild slice 7): older ideas come back once, still ideas.
        (bc_memory_search, "Search Brain Home personal or cross-project memories by topic or phrase, e.g. Clanker. Before answering from your own memory about Nic's tasks, promises, dates, people, decisions or what he said before, search here: your memory is not the record. Use this for recall when no record ID is known. Returns bounded matching records, each with its plain word when applicable, whether it is confirmed, and the observed repository version. Every v1 capture or commitment comes back confirmed: false. ", False, False),
        (bc_memory_list, "List Nic's items from Brain Home: accepted items in open, waiting or blocked by default (word, state and level narrow or widen this), sorted by due date first (a window by its start; undated last) then most recently updated. Omit level to have it follow word (idea is level=possible, suggestion is level=suggested, everything else is level=accepted); pass level explicitly to filter to it exactly, including accepted alongside word=idea or word=suggestion. Use this for 'what's open', 'what did I ask', 'anything due'. Search is for recall by topic. kind=preference lists Nic's confirmed preferences instead, newest first; facts have search. Each result carries expected_sha256, so bc_memory_change can follow with no read. ", False, False),
        # ROA-72 answer 5 ("Rule + helpful error"; ROA-85): said once, here, since a client reads every
        # tool's description together; it covers the note tools' project_id too.
        (bc_context_retrieve, "Retrieve bounded current project context with version, hash, freshness and exclusion receipts. A project_id is vault_ plus the vault's name, e.g. vault_exosuit, here and in the note tools; a wrong one is answered with the valid IDs. ", False, False),
        (bc_continuity_start, "Start general continuity in Brain Home by omitting project_id; supply project_id only for an explicitly named project. " + _RETRY + _RUNNING_NOTE, True, False),
        (bc_continuity_append, "Append one supported Brain Core continuity event. " + _RETRY, True, False),
        (bc_continuity_recover, "Recover general Brain Home continuity by omitting project_id; supply project_id only for an explicitly named project. Supply session_id to recover one task's note instead of the latest. ", False, False),
        (bc_continuity_list, "List open Brain Home continuity notes, newest first: session_id, task_id, intent, next_step and when each was last updated. ", False, False),
        (bc_continuity_close, "Close or mark recovered an authorized continuity session. " + _RETRY, True, True),
        (bc_decision_record, "Record an accepted decision through its host-resolved canonical owner. Omit source_reference for the current user message; never invent a document reference. Return a truthful receipt. " + _RETRY, True, False),
        (bc_memory_save, "Save executive memory to Brain Home with scope personal: items (idea/task/promise/routine/goal), sourced facts, and preferences. A preference is saved only from Nic's own words (said_by owner, came_by direct); a guessed preference is an item at level=suggested instead. A preference's focus_projects (project IDs) names Nic's focus projects; his newest list wins. Tags cannot perfectly classify arbitrary prose. Omit source_reference for the current user message: the server derives authenticated request provenance. Supply source_reference only for a previously enrolled document; never invent it. Put the full content in body: other agents cannot open private links such as claude.ai pages or local file paths. The confirmation carries the plain word, any resolved date, and links. Return validation versus acceptance explicitly. " + _LANGUAGE + _RETRY, True, False),
        (bc_memory_change, "Change one fact or preference in Brain Home; items use bc_work through Diablo or the morning review. Read the record first for record_id and expected_sha256; only the newest version can change. " + _LANGUAGE + _RETRY, True, False),
        (bc_memory_read, "Read one authorized memory record with version, hash and freshness, its plain word when applicable, and whether it is confirmed (every v1 capture or commitment reads back confirmed: false). A record already replaced by a change reads back freshness: stale with superseded_by naming its successor(s); re-read the successor before changing it again. ", False, False),
    ]
    if door:
        definitions.append((bc_work, _WORK, True, False))
    if door and calendar_removal:
        definitions.append((bc_calendar_remove, _CALENDAR_REMOVE_TEXT, True, True))
    tools: list[Tool] = []
    for function, description, write, destructive in definitions:
        required_scopes = ["gt:read", _READ_SCOPE, *([_WRITE_SCOPE] if write else [])]
        tool = Tool.from_function(
            function,
            description=description,
            structured_output=False,
            annotations=ToolAnnotations(
                readOnlyHint=not write,
                destructiveHint=destructive,
                idempotentHint=True,
                openWorldHint=True,
            ),
            meta={"securitySchemes": [{"type": "oauth2", "scopes": required_scopes}]},
        )
        model = tool.fn_metadata.arg_model
        model.model_config = ConfigDict(**{**model.model_config, "extra": "forbid"})
        model.model_rebuild(force=True)
        tool.fn_metadata = _StrictArguments(arg_model=model)
        tool.parameters = _slim_schema(model.model_json_schema(by_alias=True))
        if function.__name__ == "bc_memory_save":
            # One-release bridge accepts stale fields but does not advertise them.
            for retired in ("size", "importance", "importance_reason", "sort", "first_step", "links"):
                tool.parameters["properties"].pop(retired, None)
        tools.append(tool)
    return tools


# Step F's two asks (Ciel slice 4; rebuild plan G9). Registered only when [orientation] is on,
# so none of this counts in the default tool text (tests/mcp/test_tool_text_measurement.py).
_OVERVIEW_TEXT = (
    f'"{work_words("orient")}" (he may ask "where am I?"): every open thing once, with parts folded into their task. Shows progress, next and why it waits; highest right now ranks only things he {work_words("up")}. Show the returned sections and reasons, including what could not be read. Recommends nothing. Changes go through Diablo or the morning review, in Nic\'s words.'
)
_NEXT_TEXT = (
    f'"What next?", only when Nic asks: the pick he {work_words("up")}, what it belongs to, progress and Nic\'s next action if supplied, else the task itself. Never invent a first step. Show reasons biggest first, alternatives, what can wait and any time box. Name in_progress first. Ask missed questions as returned. If not_enough, give its reason and questions. Wait for his answer; changes go through Diablo or the morning review.'
)
# PRD §4.9 (build plan slice 6): an agenda for one person, and closing its topics in Nic's words.
_AGENDA_TEXT = (
    "\"What do I need to raise with <person>?\": their parts (whose), needs from them (needs.from), "
    "Nic's promises to them, things to raise, and legacy waiting links, through the sole work reader. "
    f"Things he {work_words('up')} are most important first; waits keep their stable order. Answers go through Diablo "
    "or the morning review, in Nic's words; promises and waits change only as he says. A first name "
    "finds a full one; if outcome is "
    "ask_which, ask Nic which of its names he means, then call again with that name and exact true. "
)
# PRD §4.3 (build plan slice 10), only while time blocks are on: the block filters the pick, and
# Nic's own line overrides it.
_BLOCKS_TEXT = (
    "Inside one of Nic's time blocks it offers only that kind of work (moment says which); "
    "outside any, everything. If he says he's out, wiped or has N "
    "minutes, pass override (out, wiped or minutes, with minutes) and override_words, his line. "
)
# PRD §4.8 (build plan slice 9): the weekly review, walked through one decision at a time.
_WEEKLY_TEXT = (
    "The frozen weekly review, ready every Friday afternoon: finished work including parts; missed "
    f"deadlines; unanswered {work_words('needs_you')}; waiting by person; every goal and project once with progress, "
    "next and still want it?; slipping and strong routines; next week's deadlines and appointments; "
    "agent requests idle 14 days once, marked stale. Ask whether Nic wants to go through his ideas; "
    "only on his yes call with ideas=true. Show his saved words; label existing agent paraphrases "
    "for his rewording. Walk him through one decision at a time. Answers go through Diablo or the "
    "morning review, in his words. "
)
_NEXT_RESPOND_TEXT = (
    "Record Nic's yes, no, not_now, or other with target to the bc_next surfacing_id. Tier B records only the why-log answer; it writes no work or note. Diablo's yes/other goes through the work door as Start: supply explicit said_by and the original raw message, with words quoting it. A bare yes cannot authorise Start; ask for his words naming what he started. Replaying the same answer is safe. Project tasks keep their own project note and next step. Name any in_progress or caution returned. first_step is compatibility only."
)


def build_orientation_tools(
    context: OrientationContext,
    actor_provider: Callable[[], Actor],
    *,
    scopes_provider: Callable[[], Collection[str]],
    authorize: Callable[[], Awaitable[None]],
) -> list[Tool]:
    """``bc_orient`` ("where am I?"), ``bc_next`` ("what next?") and ``bc_next_respond`` over an
    :class:`OrientationContext`, registered by the caller only when ``[orientation]`` is enabled
    (step F spec §2, §8). Disabled is handled by ``overview``/``what_next``/``respond``
    themselves (``{"outcome": "disabled"}``), same as ``run_review``."""

    from brain_core.blocks import Override
    from brain_core.orientation import OrientationSignal

    from gremlin_town.review.orient import agenda, overview, respond, weekly, what_next

    async def _authorized_actor(*, write: bool) -> Actor | CallToolResult:
        await authorize()
        actor = actor_provider()
        if not isinstance(actor, Actor) or not actor.actor_id or actor.is_operator:
            return _error("ACCESS_DENIED", "participant authentication required")
        scopes = frozenset(scopes_provider())
        if _READ_SCOPE not in scopes or (write and _WRITE_SCOPE not in scopes):
            return _error("ACCESS_DENIED", "required Brain Core scope not granted")
        return actor

    async def bc_orient() -> CallToolResult:
        try:
            actor = await _authorized_actor(write=False)
            if isinstance(actor, CallToolResult):
                return actor
            return _result(overview(context, datetime.now(UTC)))
        except Exception as exc:
            return _mapped_error(exc)

    async def bc_next(
        signal: Literal["pc_on", "task_finished", "calendar_gap"] | None = None,
        gap_minutes: Annotated[StrictInt, Field(ge=1, le=1440)] | None = None,
    ) -> CallToolResult:
        try:
            actor = await _authorized_actor(write=False)
            if isinstance(actor, CallToolResult):
                return actor
            orientation_signal = OrientationSignal(kind=signal, gap_minutes=gap_minutes) if signal else None
            return _result(what_next(context, datetime.now(UTC), signal=orientation_signal))
        except Exception as exc:
            return _mapped_error(exc)

    # PRD §4.3, only while time blocks are on: bc_next also takes Nic's one-line override. Off,
    # the tool is exactly as before, so its schema and text don't change until he switches it on.
    async def bc_next_with_blocks(
        signal: Literal["pc_on", "task_finished", "calendar_gap"] | None = None,
        gap_minutes: Annotated[StrictInt, Field(ge=1, le=1440)] | None = None,
        override: Literal["out", "wiped", "minutes"] | None = None,
        override_words: ShortText | None = None,
        minutes: Annotated[StrictInt, Field(ge=1, le=1440)] | None = None,
    ) -> CallToolResult:
        try:
            actor = await _authorized_actor(write=False)
            if isinstance(actor, CallToolResult):
                return actor
            if override is not None and not override_words:
                return _error("brain.invalid_argument", "override needs override_words, Nic's own line")
            if override == "minutes" and minutes is None:
                return _error("brain.invalid_argument", "override=minutes needs minutes")
            orientation_signal = OrientationSignal(kind=signal, gap_minutes=gap_minutes) if signal else None
            line = Override(mode=override, words=override_words, minutes=minutes) if override is not None else None
            return _result(what_next(context, datetime.now(UTC), signal=orientation_signal, override=line))
        except Exception as exc:
            return _mapped_error(exc)

    bc_next_with_blocks.__name__ = "bc_next"

    async def bc_next_respond(
        surfacing_id: OpaqueReference,
        response: Literal["yes", "no", "not_now", "other"],
        words: ShortText | None = None,
        target: OpaqueReference | None = None,
        first_step: Annotated[
            StrictStr,
            Field(
                min_length=1,
                max_length=8_192,
                description="Compatibility only; cannot invent or change Nic's next action",
            ),
        ] | None = None,
        said_by: Literal["owner", "other"] | None = None,
        message: Annotated[StrictStr, Field(min_length=1, max_length=8_192,
                            description="Nic's original message, word for word; required for Diablo's Start")] | None = None,
    ) -> CallToolResult:
        try:
            actor = await _authorized_actor(write=True)
            if isinstance(actor, CallToolResult):
                return actor
            value = respond(
                context, datetime.now(UTC), actor=BrainCaller(actor.actor_id, frozenset(scopes_provider())), surfacing_id=surfacing_id,
                response=response, words=words, target=target, first_step=first_step,
                said_by=said_by, message=message,
            )
            from inspect import isawaitable
            if isawaitable(value):
                value = await value
            return _result(value)
        except Exception as exc:
            # A yes starts a note: a storage failure means it wasn't saved (respond reads nothing after the
            # note starts that could fail it, review/orient.py).
            return _mapped_error(exc, nothing_saved=True)

    async def bc_agenda(
        person: Annotated[StrictStr, Field(min_length=1, max_length=128)], exact: StrictBool = False,
    ) -> CallToolResult:
        try:
            actor = await _authorized_actor(write=False)
            if isinstance(actor, CallToolResult):
                return actor
            return _result(agenda(context, datetime.now(UTC), person=person, exact=exact))
        except Exception as exc:
            return _mapped_error(exc)

    async def bc_weekly(ideas: StrictBool = False) -> CallToolResult:
        try:
            actor = await _authorized_actor(write=False)
            if isinstance(actor, CallToolResult):
                return actor
            return _result(weekly(context, datetime.now(UTC), ideas=ideas))
        except Exception as exc:
            return _mapped_error(exc)

    blocks_on = getattr(getattr(context, "orientation_settings", None), "blocks", False)
    definitions = [
        (bc_orient, _OVERVIEW_TEXT, False, False),
        (bc_next_with_blocks, _NEXT_TEXT + _BLOCKS_TEXT, False, False) if blocks_on else (bc_next, _NEXT_TEXT, False, False),
        (bc_next_respond, _NEXT_RESPOND_TEXT, True, False),
        (bc_agenda, _AGENDA_TEXT, False, False),
        (bc_weekly, _WEEKLY_TEXT, False, False),
    ]
    tools: list[Tool] = []
    for function, description, write, destructive in definitions:
        required_scopes = ["gt:read", _READ_SCOPE, *([_WRITE_SCOPE] if write else [])]
        tool = Tool.from_function(
            function,
            description=description,
            structured_output=False,
            annotations=ToolAnnotations(
                readOnlyHint=not write,
                destructiveHint=destructive,
                idempotentHint=True,
                openWorldHint=True,
            ),
            meta={"securitySchemes": [{"type": "oauth2", "scopes": required_scopes}]},
        )
        model = tool.fn_metadata.arg_model
        model.model_config = ConfigDict(**{**model.model_config, "extra": "forbid"})
        model.model_rebuild(force=True)
        tool.fn_metadata = _StrictArguments(arg_model=model)
        tool.parameters = _slim_schema(model.model_json_schema(by_alias=True))
        tools.append(tool)
    return tools
