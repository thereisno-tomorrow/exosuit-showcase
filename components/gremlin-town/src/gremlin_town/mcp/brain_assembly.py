"""Lead-owned production composition for Brain Core in the existing MCP."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from brain_core.github_storage import MAX_DECISION_CANDIDATES, GitHubBrainHomeStorage, GitHubObjectCache
from brain_core.records import Evidence, evidence_from_mapping

from gremlin_town.mcp.brain_home_copy import open_copy_store
from gremlin_town.mcp.brain_policy import PROJECT_OPERATIONS, TrustedBrainAuthorizer, TrustedOwnerRule
from gremlin_town.mcp.brain_runtime import (
    BrainOperation,
    BrainRuntime,
    BrainRuntimeError,
    OwnerAuthorization,
    OwnerSelector,
)
from gremlin_town.mcp.local_project_connector import (
    LocalConnectorContextProvider,
    LocalConnectorContinuityProvider,
    LocalProjectConnectorClient,
    LocalProjectContextCapability,
)


POLICY_SCHEMA = "gt.brain-owner-policy/v1"
SOURCE_REGISTRY_SCHEMA = "gt.brain-source-evidence/v1"
_OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_AUTHENTICATED_REQUEST_REFERENCE = re.compile(
    r"^mcp-request:([A-Za-z0-9][A-Za-z0-9._-]{0,95})$"
)
# continuity.list joined on 28 Sep 2026 (ROA-15; decision "Agents working in a project can list
# and read every open 'where I stopped' note in that project"): a new chat finds where earlier
# chats stopped, as D18 already intends for requests. The notes stay in the project's own folder.
_PROJECT_OPERATIONS = PROJECT_OPERATIONS
_GITHUB_OPERATIONS = frozenset({
    BrainOperation.WORK,
    BrainOperation.DECISION_RECORD,
    BrainOperation.MEMORY_SAVE,
    BrainOperation.MEMORY_CHANGE,
    BrainOperation.MEMORY_READ,
    BrainOperation.MEMORY_SEARCH,
    BrainOperation.MEMORY_LIST,
    BrainOperation.CONTINUITY_START,
    BrainOperation.CONTINUITY_APPEND,
    BrainOperation.CONTINUITY_RECOVER,
    BrainOperation.CONTINUITY_LIST,
    BrainOperation.CONTINUITY_CLOSE,
})
# Sitreps (docs/product/steps/b-project-sitreps.md; decision 20260928-sitreps-come-in-two-classes-
# and-every-agent-nic--2c0a3a8d246f): reading them is granted on the Brain Home cross-project
# owner, so its readers - every agent Nic uses - can read any project's sitreps, enrolled on the
# project or not. Allowed on Brain Home owners only, and left out of the missing-operation
# warning below: it matters only once `[sitreps]` is switched on (see operations.md).
_SITREP_OPERATIONS = frozenset({BrainOperation.SITREP_READ})

logger = logging.getLogger(__name__)


class BrainAssemblyError(RuntimeError):
    """Trusted configuration is absent, invalid, or unsafe."""


@dataclass(frozen=True, slots=True)
class OwnerEnrollment:
    selector: OwnerSelector
    owner_id: str
    owner_locator: str
    readers: frozenset[str]
    writers: frozenset[str]
    operations: frozenset[BrainOperation]
    session_id: str | None


@dataclass(frozen=True, slots=True)
class OwnerPolicy:
    revision: str
    owners: tuple[OwnerEnrollment, ...]
    subject_scopes: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GitHubOwnerCapability:
    """Opaque host-issued marker; repository settings remain in the provider."""

    owner_id: str
    owner_locator: str


class DurableProviderState:
    """Host-owned evidence enrollment and stable operation time state.

    The SQLite file is local coordination state, not a canonical Brain record.
    Canonical record and semantic lookup always goes back to the configured,
    coherently read GitHub branch.
    """

    def __init__(
        self,
        state_dir: Path,
        source_registry_file: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._sources = self._load_sources(source_registry_file)
        try:
            self._database = sqlite3.connect(
                state_dir / "provider-state.sqlite3",
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            self._database.execute("PRAGMA journal_mode=WAL")
            self._database.execute("PRAGMA synchronous=FULL")
            self._database.execute(
                """
                CREATE TABLE IF NOT EXISTS accepted_operations (
                    operation_id TEXT PRIMARY KEY,
                    accepted_at TEXT NOT NULL
                )
                """
            )
        except sqlite3.Error as exc:
            raise BrainAssemblyError("Brain provider state cannot be opened") from exc

    @staticmethod
    def _load_sources(path: Path) -> dict[tuple[str, str, str], Evidence]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BrainAssemblyError("Brain source registry is unreadable or invalid") from exc
        if not isinstance(raw, dict) or set(raw) != {"schema", "revision", "sources"}:
            raise BrainAssemblyError("Brain source registry fields differ from the contract")
        if raw["schema"] != SOURCE_REGISTRY_SCHEMA:
            raise BrainAssemblyError("Brain source registry schema is unsupported")
        _text(raw["revision"], "source registry revision")
        entries = raw["sources"]
        if not isinstance(entries, list):
            raise BrainAssemblyError("Brain source registry sources must be a list")
        sources: dict[tuple[str, str, str], Evidence] = {}
        for position, item in enumerate(entries):
            if not isinstance(item, dict) or set(item) != {
                "reference", "owner_id", "owner_locator", "evidence"
            }:
                raise BrainAssemblyError(
                    f"sources[{position}] fields differ from the contract"
                )
            key = (
                _text(item["owner_id"], f"sources[{position}].owner_id"),
                _text(item["owner_locator"], f"sources[{position}].owner_locator"),
                _text(item["reference"], f"sources[{position}].reference"),
            )
            if key in sources:
                raise BrainAssemblyError("Brain source registry entries must be unique")
            try:
                sources[key] = evidence_from_mapping(
                    item["evidence"], f"source registry entry {position}"
                )
            except Exception as exc:
                raise BrainAssemblyError(
                    f"sources[{position}].evidence is not a valid Brain Core evidence record"
                ) from exc
        return sources

    def operation_time(self, operation_id: str) -> datetime:
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            raise BrainRuntimeError("brain.invalid_argument", "operation_id is invalid")
        return self._pinned_time(operation_id)

    def operation_time_scoped(self, caller: str, operation_id: str) -> datetime:
        """Pin ``accepted_at`` for one operation id scoped to its authenticated caller
        (P3, finding 15): two callers using the same ``operation_id`` each pin their
        own ``"<caller>:<operation_id>"`` row instead of racing to fill one shared row.

        This is a new, additive entry point - it does not change :meth:`operation_time`
        itself in any way, so continuity's and decisions' use of it is unchanged, and
        its validation is not loosened: both ``caller`` and ``operation_id`` are each
        checked against the exact same pattern ``operation_time`` checks its one
        argument against.
        """

        if not isinstance(caller, str) or _OPERATION_ID.fullmatch(caller.removeprefix("system:")) is None:
            raise BrainRuntimeError("brain.invalid_argument", "caller is invalid")
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            raise BrainRuntimeError("brain.invalid_argument", "operation_id is invalid")
        return self._pinned_time(f"{caller}:{operation_id}")

    def _pinned_time(self, key: str) -> datetime:
        with self._lock:
            try:
                row = self._database.execute(
                    "SELECT accepted_at FROM accepted_operations WHERE operation_id = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    accepted_at = self._clock()
                    if (
                        not isinstance(accepted_at, datetime)
                        or accepted_at.tzinfo is None
                        or accepted_at.utcoffset() is None
                    ):
                        raise BrainAssemblyError("Brain provider clock returned an invalid time")
                    rendered = accepted_at.astimezone(UTC).isoformat()
                    self._database.execute("BEGIN IMMEDIATE")
                    try:
                        self._database.execute(
                            "INSERT OR IGNORE INTO accepted_operations(operation_id, accepted_at) VALUES (?, ?)",
                            (key, rendered),
                        )
                        self._database.execute("COMMIT")
                    except BaseException:
                        self._database.execute("ROLLBACK")
                        raise
                    row = self._database.execute(
                        "SELECT accepted_at FROM accepted_operations WHERE operation_id = ?",
                        (key,),
                    ).fetchone()
                if row is None or not isinstance(row[0], str):
                    raise BrainAssemblyError("Brain provider operation state is corrupt")
                accepted_at = datetime.fromisoformat(row[0])
                if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
                    raise ValueError("naive accepted time")
                return accepted_at
            except (sqlite3.Error, ValueError) as exc:
                raise BrainAssemblyError("Brain provider operation state is corrupt") from exc

    def source_evidence(
        self, authorization: OwnerAuthorization, reference: str
    ) -> Evidence:
        key = (authorization.owner_id, authorization.owner_locator, reference)
        evidence = self._sources.get(key)
        if evidence is not None:
            return evidence
        request_reference = _AUTHENTICATED_REQUEST_REFERENCE.fullmatch(reference)
        if request_reference is None:
            raise BrainRuntimeError(
                "brain.policy.excluded",
                "Document source reference is not enrolled for the authorized owner",
            )
        provenance = {
            "schema": "gt.authenticated-request-provenance/v1",
            "reference": reference,
            "owner_id": authorization.owner_id,
            "owner_locator": authorization.owner_locator,
            "policy_revision": authorization.policy_revision,
        }
        encoded = json.dumps(
            provenance, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        grant = json.dumps(
            {
                "owner_id": authorization.owner_id,
                "owner_locator": authorization.owner_locator,
                "policy_revision": authorization.policy_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return Evidence(
            source_type="authenticated_request",
            locator=f"gt-mcp://authenticated-request/{request_reference.group(1)}",
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            excerpt=(
                "Authenticated MCP request provenance only; this hash identifies the "
                "authorization envelope and does not independently verify unseen conversation content."
            ),
            actor="authenticated-mcp-principal",
            grant_sha256=hashlib.sha256(grant).hexdigest(),
        )

    def close(self) -> None:
        with self._lock:
            self._database.close()

    async def aclose(self) -> None:
        self.close()


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrainAssemblyError(f"{field} must be non-empty text")
    return value.strip()


def _subjects(value: Any, field: str, *, required: bool) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise BrainAssemblyError(f"{field} must be a list of non-empty subjects")
    result = frozenset(item.strip() for item in value)
    if required and not result:
        raise BrainAssemblyError(f"{field} cannot be empty")
    return result


def load_owner_policy(path: Path) -> OwnerPolicy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrainAssemblyError("Brain owner policy is unreadable or invalid") from exc
    if not isinstance(raw, dict) or set(raw) not in ({"schema", "revision", "owners"}, {"schema", "revision", "owners", "subject_scopes"}):
        raise BrainAssemblyError("Brain owner policy fields differ from the contract")
    if raw["schema"] != POLICY_SCHEMA:
        raise BrainAssemblyError("Brain owner policy schema is unsupported")
    revision = _text(raw["revision"], "revision")
    owners_raw = raw["owners"]
    if not isinstance(owners_raw, list) or not owners_raw:
        raise BrainAssemblyError("Brain owner policy requires enrolled owners")
    owners: list[OwnerEnrollment] = []
    selectors: set[OwnerSelector] = set()
    for position, item in enumerate(owners_raw):
        if not isinstance(item, dict) or set(item) != {
            "kind", "project_id", "owner_id", "owner_locator", "readers", "writers",
            "operations", "session_id",
        }:
            raise BrainAssemblyError(f"owners[{position}] fields differ from the contract")
        kind = _text(item["kind"], f"owners[{position}].kind")
        project_id = item["project_id"]
        if project_id is not None:
            project_id = _text(project_id, f"owners[{position}].project_id")
        try:
            selector = OwnerSelector(kind, project_id)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise BrainAssemblyError(f"owners[{position}] selector is invalid") from exc
        if selector in selectors:
            raise BrainAssemblyError("Brain owner policy selectors must be unique")
        selectors.add(selector)
        operations_raw = item["operations"]
        if not isinstance(operations_raw, list) or not operations_raw:
            raise BrainAssemblyError(f"owners[{position}].operations must be non-empty")
        try:
            operations = frozenset(BrainOperation(_text(value, "operation")) for value in operations_raw)
        except ValueError as exc:
            raise BrainAssemblyError(f"owners[{position}] contains an unsupported operation") from exc
        owner_locator = _text(item["owner_locator"], f"owners[{position}].owner_locator")
        session_id = item["session_id"]
        if session_id is not None:
            session_id = _text(session_id, f"owners[{position}].session_id")
        if selector.kind == "project":
            if BrainOperation.DECISION_RECORD in operations:
                raise BrainAssemblyError(
                    "system, fleet, and project decisions require a different canonical owner adapter"
                )
            if not operations <= _PROJECT_OPERATIONS or session_id is None:
                raise BrainAssemblyError("project enrollment supports only scoped context/continuity and requires session_id")
            if owner_locator != f"vault://{selector.project_id}":
                raise BrainAssemblyError("project owner locator must match the enrolled project")
        elif selector.kind in {"personal", "cross-project"}:
            if not operations <= _GITHUB_OPERATIONS | _SITREP_OPERATIONS or session_id is not None:
                raise BrainAssemblyError("shared Brain Home enrollment supports only memory, decisions, general continuity and sitrep reads")
            if owner_locator != "vault://brain-home":
                raise BrainAssemblyError("personal and cross-project memory must use Brain Home")
        else:
            raise BrainAssemblyError("system, fleet, and project decisions require a different canonical owner adapter")
        owners.append(OwnerEnrollment(
            selector=selector,
            owner_id=_text(item["owner_id"], f"owners[{position}].owner_id"),
            owner_locator=owner_locator,
            readers=_subjects(item["readers"], f"owners[{position}].readers", required=True),
            writers=_subjects(item["writers"], f"owners[{position}].writers", required=False),
            operations=operations,
            session_id=session_id,
        ))
    scopes_raw = raw.get("subject_scopes", {})
    if not isinstance(scopes_raw, dict):
        raise BrainAssemblyError("subject_scopes must be an object")
    subject_scopes = {}
    for subject, scopes in scopes_raw.items():
        if not isinstance(subject, str) or not subject.strip() or subject != subject.strip():
            raise BrainAssemblyError("subject_scopes requires exact non-empty subjects")
        if not isinstance(scopes, list) or any(scope not in {"bc:read", "bc:write"} for scope in scopes):
            raise BrainAssemblyError("subject_scopes supports only bc:read and bc:write")
        subject_scopes[subject] = frozenset(scopes)
    _warn_on_incomplete_brain_home_owners(owners)
    return OwnerPolicy(revision, tuple(owners), subject_scopes)


_WARNED_INCOMPLETE_OWNERS_LOCK = threading.Lock()
_WARNED_INCOMPLETE_OWNERS: set[tuple[str, frozenset[BrainOperation]]] = set()


def _warn_on_incomplete_brain_home_owners(owners: list[OwnerEnrollment]) -> None:
    """Log, never fail, when a loaded Brain Home owner omits a supported operation.

    A release can add a new Brain operation (`_GITHUB_OPERATIONS`) without the
    live policy's `personal`/`cross-project` owner lists catching up, so every
    agent is silently refused that operation until someone notices and edits
    the policy file by hand (found live 2026-09-26). This only warns; it never
    changes which operations are allowed.

    ``load_owner_policy`` reloads on every Brain call (the per-call recheck in
    ``build_brain_runtime`` and the HTTP handler in ``api/brain.py`` both call
    it), so each distinct (owner_id, missing operations) combination is logged
    only once per process, not once per call.
    """

    for owner in owners:
        if owner.selector.kind not in {"personal", "cross-project"}:
            continue
        missing = _GITHUB_OPERATIONS - owner.operations - {BrainOperation.WORK}
        if not missing:
            continue
        key = (owner.owner_id, missing)
        with _WARNED_INCOMPLETE_OWNERS_LOCK:
            if key in _WARNED_INCOMPLETE_OWNERS:
                continue
            _WARNED_INCOMPLETE_OWNERS.add(key)
        logger.warning(
            "Brain Home owner %s (%s) is missing supported operations: %s",
            owner.owner_id,
            owner.selector.kind,
            ", ".join(sorted(missing)),
        )


def _read_secret(path: Path, purpose: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise BrainAssemblyError(f"{purpose} credential file is unreadable") from exc
    if not value or any(character in value for character in "\r\n\0"):
        raise BrainAssemblyError(f"{purpose} credential is empty or malformed")
    return value


def _result_path(result: object, purpose: str) -> str | None:
    if result is None:
        return None
    path = getattr(result, "record_path", None)
    if path is None:
        path = getattr(result, "path", None)
    if not isinstance(path, str) or not path:
        raise BrainAssemblyError(f"GitHub {purpose} lookup returned an invalid result")
    return path


def _brain_home_credentials(settings: Any, github_transport: Any | None,
                            github_app_client: Any | None) -> tuple[str, Any | None]:
    """Return the storage token and transport: a fixed token, or a GitHub App.

    With an App, the storage gets a placeholder token and a transport that sets
    a fresh one-hour installation token on every request.
    """
    app_id = getattr(settings, "github_app_id", None)
    if app_id is None:
        return _read_secret(settings.github_token_file, "Brain GitHub"), github_transport
    from brain_core.github_storage import UrllibHTTPTransport
    from gremlin_town.github_app import (
        APP_TOKEN_PLACEHOLDER, AppTokenHTTPTransport, GitHubAppError, GitHubAppTokenProvider, load_private_key,
    )
    try:
        private_key = load_private_key(settings.github_app_private_key_file)
        provider = GitHubAppTokenProvider(
            app_id=app_id,
            installation_id=settings.github_installation_id,
            private_key=private_key,
            repository=settings.github_repository,
            permissions={"contents": "write"},
            client=github_app_client,
        )
    except (GitHubAppError, ValueError) as exc:
        raise BrainAssemblyError("Brain GitHub App credential is unreadable or invalid") from exc
    return APP_TOKEN_PLACEHOLDER, AppTokenHTTPTransport(provider, github_transport or UrllibHTTPTransport())


def build_brain_runtime(
    settings: Any,
    *,
    github_transport: Any | None = None,
    github_app_client: Any | None = None,
    operation_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    review_enabled: bool = False,
    calendar: Any | None = None,
    sitreps_enabled: bool = False,
    why_log: Any | None = None,
    door_participant: str = "hermes-wa",
    code_callers: frozenset[str] = frozenset(),
    schedule_wake: Callable[[], None] | None = None,
    schedule_lengths: bool = False,
    past_appointment_done: bool = False,
) -> BrainRuntime:
    """Build the real disabled-by-default production dependency graph.

    ``settings`` is the lead-owned ``BrainMCPSettings``. Importing this module
    never reads a credential or creates state; this function is called only when
    the trusted runtime configuration explicitly enables Brain Core.
    ``github_app_client`` replaces the HTTP client that mints App tokens, for tests.
    ``review_enabled`` mirrors ``[review] enabled`` (step E, off by default).
    ``calendar`` is the Google calendar writer, passed only while calendar entries
    are on (N8): then a task with a start time gets its own entry. ``why_log``
    must be the existing review log when calendar effects are enabled; it keeps
    exact successful acknowledgments for retries, without a separate store.
    ``sitreps_enabled`` mirrors ``[sitreps] enabled`` (off by default): only then
    does the hub keep task sitreps and Focus copies in Brain Home and answer project
    lists from them (docs/product/steps/b-project-sitreps.md); its clock is
    ``operation_clock``.
    """

    from gremlin_town.mcp.github_brain_provider import BrainCoreRecordAdapter, GitHubBrainProvider

    policy = load_owner_policy(settings.policy_file)
    state = DurableProviderState(
        settings.decision_staging_dir,
        settings.source_registry_file,
        clock=operation_clock,
    )
    copy_store = None
    try:
        connector = LocalProjectConnectorClient(
            settings.connector_url,
            _read_secret(settings.connector_token_file, "Brain connector"),
        )
        context = LocalConnectorContextProvider(connector)
        continuity = LocalConnectorContinuityProvider(connector)
        token, transport = _brain_home_credentials(settings, github_transport, github_app_client)
        copy_store = open_copy_store(settings.decision_staging_dir)
        storage = GitHubBrainHomeStorage(
            repository=settings.github_repository,
            branch=settings.github_branch,
            token=token,
            transport=transport,
            object_cache=GitHubObjectCache(blob_store=copy_store),
            archive_superseded=settings.archive_superseded,
        )

        # The storage-limit plan, layer 1: an old version moved to Brain Home's archive/ folder is
        # still found by its ID (its archive/ path; the read decides whether anything still names
        # it), and the decision check reads up to Brain Core's highest bound, 1,000, not 256.
        def record_locator(
            authorization: OwnerAuthorization, record_id: str
        ) -> str | None:
            from gremlin_town.mcp.github_brain_provider import find_by_id

            del authorization
            return _result_path(find_by_id(storage, record_id).match, "record identity")

        def semantic_locator(
            authorization: OwnerAuthorization, semantic_key: str
        ) -> str | None:
            del authorization
            return _result_path(
                storage.find_decision_by_semantic_key(semantic_key, max_candidates=MAX_DECISION_CANDIDATES),
                "decision semantic identity",
            )

        record_adapter = BrainCoreRecordAdapter(
            operation_time=state.operation_time,
            scoped_operation_time=state.operation_time_scoped,
            source_evidence=state.source_evidence,
            record_locator=record_locator,
            semantic_locator=semantic_locator,
            catalog_path=settings.ownership_catalog_file,
        )
        github = GitHubBrainProvider(storage, record_adapter, review_enabled=review_enabled, calendar=calendar,
            why_log=why_log, door_participant=door_participant, code_callers=code_callers, clock=operation_clock,
            schedule_wake=schedule_wake, schedule_lengths=schedule_lengths,
            past_appointment_done=past_appointment_done,
            replay_marker=settings.decision_staging_dir / "work-replay-check-only")
        from gremlin_town.mcp.github_continuity import GitHubContinuityProvider, RoutedContinuityProvider
        routed_continuity = RoutedContinuityProvider(continuity, GitHubContinuityProvider(storage, state.operation_time))
        sitreps = None
        if sitreps_enabled:
            from gremlin_town.mcp.sitreps import SitrepService

            sitreps = SitrepService(
                storage=storage,
                connector=connector,
                state_dir=settings.decision_staging_dir,
                clock=operation_clock,
            )
    except BaseException:
        if copy_store is not None:
            copy_store.close()
        state.close()
        raise

    rules: list[TrustedOwnerRule] = []
    for owner in policy.owners:
        capability: object
        if owner.selector.kind == "project":
            capability = LocalProjectContextCapability(
                owner.selector.project_id or "", owner.session_id or "",
            )
        else:
            capability = GitHubOwnerCapability(owner.owner_id, owner.owner_locator)
        rules.append(TrustedOwnerRule(
            selector=owner.selector,
            owner_id=owner.owner_id,
            owner_locator=owner.owner_locator,
            policy_revision=policy.revision,
            readers=owner.readers,
            writers=owner.writers,
            capabilities={operation: capability for operation in owner.operations},
        ))
    def recheck(caller, operation, selector, access, rule):
        # Reload grants on every call. Capabilities remain the startup-created objects;
        # changing an owner or its storage binding requires a reviewed restart.
        current = load_owner_policy(settings.policy_file)
        owner = next((owner for owner in current.owners if owner.selector == selector), None)
        if (owner is None or owner.owner_id != rule.owner_id or owner.owner_locator != rule.owner_locator
                or operation not in owner.operations
                or caller.subject not in (owner.readers if access == "read" else owner.writers)):
            raise BrainRuntimeError("brain.policy.excluded", "owner grant is unavailable")

    components = dict(
        authorizer=TrustedBrainAuthorizer(tuple(rules), recheck=recheck, dynamic_subject_grants=True),
        context=context,
        continuity=routed_continuity,
        decisions=github,
        memory=github,
        managed_resources=(context, continuity, state)
        + (() if copy_store is None else (copy_store,))
        + (() if sitreps is None else (sitreps,)),
    )
    if sitreps is not None:
        from gremlin_town.mcp.sitreps import SitrepBrainRuntime

        return SitrepBrainRuntime(sitreps=sitreps, **components)
    return BrainRuntime(**components)
