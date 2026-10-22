"""Root-owned runtime composition for the single Gremlin Town service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import shutil
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gremlin_town.api import create_app, create_public_app
from gremlin_town.api.auth import AuthenticationError, get_actor
from gremlin_town.contracts import Actor, ErrorCode
from gremlin_town.domain import GremlinTownError, Participant, Policy
from gremlin_town.github_app import GitHubAppError, GitHubAppTokenProvider, load_private_key
from gremlin_town.service import GremlinTownService
from gremlin_town.sources.google.settings import GoogleSettings
from gremlin_town.flowsavvy.settings import FlowSavvySettings, ALLOWED_KEYS as FLOWSAVVY_KEYS
from gremlin_town.dashboard.settings import DashboardSettings, ALLOWED_KEYS as DASHBOARD_KEYS
from gremlin_town.review_card.settings import DiscordSettings, ALLOWED_KEYS as DISCORD_KEYS
from gremlin_town.storage import SQLiteStore
from gremlin_town.transport import AgentMailConfig, AgentMailTransport, GitHubConfig, GitHubTransport, BoundTransport
from gremlin_town.workers import OutboxWorker

if TYPE_CHECKING:
    # brain_core is an optional, heavier dependency; [review]/[orientation] are off
    # by default and their own modules are imported lazily in build_runtime (as
    # brain/mcp already are), so a plain `import gremlin_town.runtime` never pulls it in.
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.orient import OrientationContext
    from gremlin_town.review.settings import OrientationSettings, ReviewSettings
    from gremlin_town.review.why_log import WhyLog

logger = logging.getLogger(__name__)

RUNTIME_CONFIG_SCHEMA: dict[str, frozenset[str]] = {
    "discord": DISCORD_KEYS,
    "dashboard": DASHBOARD_KEYS,
    "flowsavvy": FLOWSAVVY_KEYS,
    "server": frozenset({"host", "port", "database", "operator_only"}),
    "public_gateway": frozenset({"host", "port", "actor_id", "allowed_operations"}),
    "transport": frozenset({"default_backend"}),
    "github": frozenset({
        "owner", "repository", "writer_user_id", "api_key_file_env",
        "app_id", "installation_id", "app_private_key_file_env", "legacy_writer_user_ids",
    }),
    "agentmail": frozenset({"base_url", "inbox_id", "api_key_file_env"}),
    "participants": frozenset({"file"}),
    "recovery": frozenset({"normal_clients_allowed", "outbound_dispatch_allowed", "mcp_allowed"}),
    "mcp": frozenset({
        "enabled", "host", "port", "public_url", "private_url", "state_dir", "client",
        "client_id", "client_secret_file", "redirect_uris", "max_grant_days",
    }),
    "brain_core": frozenset({
        "enabled", "connector_url", "connector_token_file", "github_repository",
        "github_branch", "github_token_file", "github_app_id", "github_installation_id",
        "github_app_private_key_file", "policy_file", "decision_staging_dir",
        "ownership_catalog_file", "source_registry_file", "archive_superseded",
    }),
    # Accepted for work-01; the work-02 door reads it in W12. No gate changes here.
    "work": frozenset({"door_participant", "review_card", "dashboard"}),
    "worker": frozenset({"poll_seconds"}),
    "google": frozenset({"enabled", "account", "credential_env", "calendars"}),
    "review": frozenset({
        "enabled", "sender_participant", "delivery_participant", "project",
        "why_log_file", "gap_minutes", "gap_hours", "telegram_spool_dir", "handoff_notes",
        "sweep_participant", "names_at", "important_mail", "important_mail_log",
        "important_mail_deadline_seconds", "opening_deadlines", "past_appointment_done", "tidy", "new_lengths",
    }),
    "orientation": frozenset({"enabled", "ranking", "blocks", "schedule"}),
    # The placement engine's runner (docs/product/steps/i-placement-engine.md, section 8): off unless
    # enabled = true, plan-only unless write = true; needs [review].
    "placement": frozenset({
        "enabled", "write", "buffer_after_meeting_minutes", "deep_focus_min_minutes", "deep_focus_block_min_minutes",
        "deep_focus_max_minutes", "split_min_minutes", "settle_after_minutes", "busy", "repair_hour",
    }),
    # Task and project sitreps in Brain Home (docs/product/steps/b-project-sitreps.md): off
    # unless enabled = true; needs [brain_core].
    "sitreps": frozenset({"enabled"}),
    # The one-way Notion view (docs/product/steps/h-notion-view.md): read only by `gremlin-town notion-view`,
    # which parses it itself (notion_view/settings.py); off unless enabled = true.
    "notion_view": frozenset({
        "enabled", "token_file", "database_id", "state_file", "check_seconds", "repair_hours", "done_days",
    }),
}
PARTICIPANT_CONFIG_KEYS = frozenset(
    {"id", "display_name", "address", "role", "enabled", "auth_mode", "token_sha256", "projects", "sensitivities", "aliases", "read_all_requests"}
)
ROUTE_CONFIG_KEYS = frozenset({"project", "source", "destination", "sensitivity"})
MCP_CLIENT_CONFIG_KEYS = frozenset({"participant", "name", "client_id", "client_secret_file", "redirect_uris", "origins"})
# The original single-client [mcp] keys (client_id, client_secret_file,
# redirect_uris) always described the ChatGPT connector.
SINGLE_CLIENT_MCP = {"participant": "chatgpt-web", "name": "ChatGPT web", "origins": ["https://chatgpt.com"]}
# [mcp] max_grant_days: how long one approved app connection lasts before the app
# must reconnect (security review L1). Whole days; the default applies when omitted.
MCP_MAX_GRANT_DAYS = 90
MCP_MAX_GRANT_DAYS_RANGE = (1, 365)
CAPACITY_WARNING_THRESHOLDS = {
    "pending_operations": 100,
    "retained_payload_bytes": 64 * 1024 * 1024,
    "database_footprint_bytes": 512 * 1024 * 1024,
    "filesystem_free_bytes": 2 * 1024 * 1024 * 1024,
}


class ConfigurationError(RuntimeError):
    """Raised before binding a listener when runtime configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str
    port: int
    database: Path
    operator_only: bool
    normal_clients_allowed: bool
    outbound_dispatch_allowed: bool
    worker_poll_seconds: float
    mcp_allowed: bool = True


@dataclass(frozen=True, slots=True)
class PublicGatewaySettings:
    host: str
    port: int
    actor_id: str


@dataclass(frozen=True, slots=True)
class MCPClientSettings:
    participant: str
    name: str
    client_id: str
    client_secret_file: Path | None
    redirect_uris: tuple[str, ...]
    origins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MCPSettings:
    host: str
    port: int
    public_url: str
    private_url: str
    state_dir: Path
    clients: tuple[MCPClientSettings, ...]
    max_grant_days: int = MCP_MAX_GRANT_DAYS


@dataclass(frozen=True, slots=True)
class BrainMCPSettings:
    """Brain Home GitHub access uses either a fixed token file or a GitHub App."""

    connector_url: str
    connector_token_file: Path
    github_repository: str
    github_branch: str
    github_token_file: Path | None
    policy_file: Path
    decision_staging_dir: Path
    ownership_catalog_file: Path
    source_registry_file: Path
    github_app_id: int | None = None
    github_installation_id: int | None = None
    github_app_private_key_file: Path | None = None
    archive_superseded: bool = False


@dataclass(frozen=True, slots=True)
class ParticipantSecret:
    actor_id: str
    token_sha256: str
    is_operator: bool


@dataclass(slots=True)
class Runtime:
    settings: ServerSettings
    policy: Policy
    store: SQLiteStore
    service: GremlinTownService
    worker: OutboxWorker
    app: FastAPI
    public_app: FastAPI | None
    public_gateway: PublicGatewaySettings | None
    token_resolver: Callable[[str], Actor | None]
    mcp: MCPSettings | None = None
    mcp_app: Any | None = None
    mcp_server: Any | None = None
    oauth_provider: Any | None = None
    mcp_auth: Any | None = None
    brain: BrainMCPSettings | None = None
    brain_runtime: Any | None = None
    brain_usage: Any | None = None
    google: GoogleSettings | None = None
    flowsavvy: FlowSavvySettings = field(default_factory=FlowSavvySettings)
    dashboard: DashboardSettings = field(default_factory=DashboardSettings)
    discord: DiscordSettings = field(default_factory=DiscordSettings)
    google_calendar_reader: Callable[[datetime, datetime], tuple[Any, ...]] | None = None
    # N8 (off by default): set only while [review] is on and the Google credential carries
    # calendar.events; then saves add calendar entries and bc_calendar_remove is registered.
    calendar_writer: Any | None = None
    review: ReviewSettings | None = None
    review_why_log: WhyLog | None = None
    door_participant: str = "hermes-wa"
    code_callers: frozenset[str] = frozenset()
    review_sources: ReviewSources | None = None
    review_read_snapshot: Callable[[], Any] | None = None
    orientation: OrientationSettings | None = None
    orientation_context: OrientationContext | None = None
    # `[placement]` (step I): the runner's context, built only while [review] and [placement] are on.
    placement_context: Any | None = None
    # Placement runs still going after the signal route replied plan_pending (api/review.py); shutdown
    # waits for them before closing the why-log.
    placement_late_runs: set[asyncio.Task[Any]] = field(default_factory=set)
    # `[review] handoff_notes` (N12a): the hub's note keeper, and the copy of Brain Home its
    # storage reads through (closed with the runtime). Both None while the setting is off.
    review_handoff_notes: Any | None = None
    review_blob_store: Any | None = None
    sitreps_enabled: bool = False
    worker_task: asyncio.Task[None] | None = None
    worker_error: BaseException | None = None
    participants_file: Path | None = None
    lifecycle: Any | None = None

    def refresh_participants(self) -> dict[str, object]:
        """Apply one complete local candidate; invalid input leaves live state intact.

        Synchronous: invoke off the async event loop. The policy lock waits for an
        already-authorized dispatch to settle before committing revocation. Callers
        must not hold a SQLiteStore transaction while waiting for refresh.
        """
        if self.participants_file is None or not isinstance(self.token_resolver, _ParticipantResolver):
            raise ConfigurationError("runtime participant refresh is not configured")
        with self.policy.dispatch_lock, self.policy.lock:
            candidate, secrets = _load_participants(self.participants_file)
            for client in self.mcp.clients if self.mcp is not None else ():
                enrolled = candidate._participants.get(client.participant)
                if ((enrolled is not None and enrolled.is_operator)
                        or any(item.actor_id == client.participant for item in secrets)):
                    raise ConfigurationError(f"MCP requires non-operator OAuth-only {client.participant}")
            if self.public_gateway is not None:
                public = candidate._participants.get(self.public_gateway.actor_id)
                if public is not None and public.is_operator:
                    raise ConfigurationError("public gateway cannot accept an operator identity")
            replacement = _token_resolver(self.policy, secrets,
                operator_only=self.settings.operator_only,
                normal_clients_allowed=self.settings.normal_clients_allowed)
            result = self.policy.refresh_from(candidate)
            self.token_resolver.replace(replacement)
            return result

    def close(self) -> None:
        if self.brain_usage is not None:
            self.brain_usage.close()
        if self.review_why_log is not None:
            self.review_why_log.close()
        if self.review_blob_store is not None:
            self.review_blob_store.close()
        self.store.close()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot load configuration: {path}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"configuration is not a table: {path}")
    return value


def _required_table(value: dict[str, Any], name: str) -> dict[str, Any]:
    table = value.get(name)
    if not isinstance(table, dict):
        raise ConfigurationError(f"missing [{name}] configuration")
    return table


def _required_text(value: dict[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ConfigurationError(f"missing configuration value: {name}")
    return item.strip()


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"unknown {location} configuration: {', '.join(unknown)}")


def _boolean(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a boolean")
    return value


def _loopback_host(value: Any, name: str) -> str:
    host = str(value).strip()
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if not is_loopback:
        raise ConfigurationError(f"{name} must be loopback-only")
    return host


def _port(value: Any, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer port") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{name} must be between 1 and 65535")
    return port


def _load_participants(path: Path) -> tuple[Policy, tuple[ParticipantSecret, ...]]:
    document = _read_toml(path)
    _reject_unknown(document, frozenset({"participant", "route"}), "participants top-level")
    raw_participants = document.get("participant")
    if not isinstance(raw_participants, list) or not raw_participants:
        raise ConfigurationError("participants file must contain [[participant]] entries")
    participants: list[Participant] = []
    secrets: list[ParticipantSecret] = []
    actor_ids: set[str] = set()
    token_hashes: set[str] = set()
    for raw in raw_participants:
        if not isinstance(raw, dict):
            raise ConfigurationError("participant entry must be a table")
        _reject_unknown(raw, PARTICIPANT_CONFIG_KEYS, "participant")
        actor_id = _required_text(raw, "id")
        if actor_id in actor_ids:
            raise ConfigurationError(f"duplicate participant id: {actor_id}")
        actor_ids.add(actor_id)
        role = str(raw.get("role", "participant"))
        if role not in {"participant", "operator"}:
            raise ConfigurationError(f"participant {actor_id} has an invalid role")
        is_operator = role == "operator"
        auth_mode = raw.get("auth_mode", "bearer")
        if auth_mode not in ("bearer", "oauth"):
            raise ConfigurationError(f"participant {actor_id} has an invalid auth_mode")
        if auth_mode == "oauth":
            if is_operator or "token_sha256" in raw:
                raise ConfigurationError("OAuth-only participants cannot be operators or have bearer token hashes")
        else:
            digest = _required_text(raw, "token_sha256").lower()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ConfigurationError(f"participant {actor_id} has an invalid token hash")
            if digest == "0" * 64 or digest.startswith("replace"):
                raise ConfigurationError(f"participant {actor_id} still has a placeholder token hash")
            if digest in token_hashes:
                raise ConfigurationError("participant token hashes must be unique")
            token_hashes.add(digest)
            secrets.append(ParticipantSecret(actor_id, digest, is_operator))
        projects = raw.get("projects", [])
        sensitivities = raw.get("sensitivities", [])
        aliases = raw.get("aliases", [])
        if not all(isinstance(items, list) for items in (projects, sensitivities, aliases)):
            raise ConfigurationError(f"participant {actor_id} lists must be arrays")
        participants.append(
            Participant(
                participant_id=actor_id,
                display_name=str(raw.get("display_name", actor_id)),
                address=_required_text(raw, "address"),
                projects=frozenset(str(item) for item in projects),
                sensitivities=frozenset(str(item) for item in sensitivities),
                aliases=tuple(str(item) for item in aliases),
                enabled=_boolean(raw.get("enabled"), f"participant {actor_id}.enabled", default=True),
                is_operator=is_operator,
                read_all_requests=_boolean(
                    raw.get("read_all_requests"), f"participant {actor_id}.read_all_requests", default=False
                ),
            )
        )
    raw_routes = document.get("route", [])
    if not isinstance(raw_routes, list):
        raise ConfigurationError("[[route]] entries must be tables")
    routes: set[tuple[str, str, str, str]] = set()
    for raw in raw_routes:
        if not isinstance(raw, dict):
            raise ConfigurationError("route entry must be a table")
        _reject_unknown(raw, ROUTE_CONFIG_KEYS, "route")
        routes.add(
            (
                _required_text(raw, "project"),
                _required_text(raw, "source"),
                _required_text(raw, "destination"),
                _required_text(raw, "sensitivity"),
            )
        )
    policy = Policy(participants, frozenset(routes))
    for project, source, destination, sensitivity in routes:
        try:
            policy.require_route_configuration(project, source, destination, sensitivity)
        except GremlinTownError as exc:
            raise ConfigurationError("route references an unavailable participant or scope") from exc
    return policy, tuple(secrets)


def _token_resolver(
    policy: Policy,
    secrets: tuple[ParticipantSecret, ...],
    *,
    operator_only: bool,
    normal_clients_allowed: bool,
) -> Callable[[str], Actor | None]:
    def resolve(token: str) -> Actor | None:
        if not isinstance(token, str) or len(token) < 32 or len(token) > 1024:
            return None
        supplied = hashlib.sha256(token.encode("utf-8")).hexdigest()
        match: ParticipantSecret | None = None
        for candidate in secrets:
            if hmac.compare_digest(supplied, candidate.token_sha256):
                match = candidate
        if match is None:
            return None
        try:
            participant = policy.participant(match.actor_id)
        except GremlinTownError:
            return None
        if operator_only and not participant.is_operator:
            return None
        if not normal_clients_allowed and not participant.is_operator:
            return None
        return Actor(participant.participant_id, is_operator=participant.is_operator)

    return resolve


class _ParticipantResolver:
    """Stable listener callback; credentials and policy publish under one lock."""
    def __init__(self, policy: Policy, resolver: Callable[[str], Actor | None]):
        self.policy = policy
        self._resolver = resolver

    def __call__(self, token: str) -> Actor | None:
        with self.policy.lock:
            return self._resolver(token)

    def replace(self, resolver: Callable[[str], Actor | None]) -> None:
        with self.policy.lock:
            self._resolver = resolver


def _credential_path_from_env(section: dict[str, Any], field: str) -> Path:
    env_name = _required_text(section, field)
    path_value = os.environ.get(env_name)
    if not path_value:
        raise ConfigurationError(f"provider credential path environment is missing: {env_name}")
    return Path(path_value)


def _google_calendar_event(item: Any) -> Any:
    from brain_core.review import CalendarEvent
    from brain_core.timewords import SINGAPORE
    from datetime import date as _date, time as _time

    start_raw = item.fields.get("start")
    end_raw = item.fields.get("end")
    all_day = bool(start_raw) and "T" not in start_raw
    if all_day:
        start = datetime.combine(_date.fromisoformat(start_raw), _time.min, tzinfo=SINGAPORE)
        end = datetime.combine(_date.fromisoformat(end_raw), _time.min, tzinfo=SINGAPORE)
    else:
        start = datetime.fromisoformat(start_raw)
        end = datetime.fromisoformat(end_raw)
    return CalendarEvent(
        title=item.fields.get("summary") or "",
        start=start,
        end=end,
        all_day=all_day,
        description=item.fields.get("description") or "",
        busy=item.fields.get("transparency") != "transparent",
        exosuit_role=item.fields.get("exosuit_role"),
    )


def _google_access(google: GoogleSettings) -> tuple[Any, Any] | None:
    """`(credential, token provider)` for `[google]`, or `None` when it is absent or disabled.

    The credential is loaded once and its scopes checked before anything is sent; one token
    provider serves both the calendar reader and the calendar writer, so they share one
    access token."""

    if not google.enabled:
        return None
    from gremlin_town.sources.google.auth import GoogleCredential, GoogleTokenProvider

    credential_path = _credential_path_from_env({"credential_env": google.credential_env}, "credential_env")
    try:
        credential = GoogleCredential.load(credential_path)
    except Exception as exc:
        raise ConfigurationError("invalid Google credential configuration") from exc
    return credential, GoogleTokenProvider(credential)


def _google_calendar_writer(
    google: GoogleSettings, access: tuple[Any, Any] | None, *, review_enabled: bool,
) -> Any | None:
    """Exosuit's own calendar entries (N8; step E spec §5), or `None`: only while `[review]` is
    on and the credential carries `calendar.events`. `None` keeps every save exactly as before."""

    if access is None or not review_enabled:
        return None
    credential, token_provider = access
    if not credential.writes_calendar:
        return None
    from gremlin_town.sources.google.calendar_writer import CalendarWriter

    return CalendarWriter(google, token_provider)


def _google_calendar_reader(
    google: GoogleSettings, access: tuple[Any, Any] | None, *, strict: bool = False,
) -> Callable[[datetime, datetime], tuple[Any, ...]] | None:
    """A `ReviewSources.calendar_events` adapter over step D's `CalendarReader`, or `None`
    when `[google]` is absent or disabled - Google isn't connected yet (morning queue)."""

    if access is None:
        return None
    from gremlin_town.review.collect import SourceUnavailable
    from gremlin_town.sources.google.calendar import CalendarReader
    from gremlin_town.sources.result import ReadStatus

    _credential, token_provider = access
    calendar_reader = CalendarReader(google, token_provider)

    def read(start: datetime, end: datetime) -> tuple[Any, ...]:
        result = calendar_reader.events(start, end)
        if result.status is ReadStatus.UNAVAILABLE:
            raise SourceUnavailable(result.reason.value)
        # The placement runner reads strictly (step I section 4): a calendar it couldn't read could
        # hold a meeting it would write over, so a partial read counts as no read.
        if strict and result.status is not ReadStatus.OK:
            raise SourceUnavailable("partial")
        events = tuple(_google_calendar_event(item) for item in result.items)
        # Keep usable events, but never turn an incomplete read into success.
        # Source identifiers stay inside the reader; surfaces get a safe status.
        if result.status is ReadStatus.PARTIAL:
            raise SourceUnavailable("partial", items=events)
        return events

    return read


def _review_important_emails(google: GoogleSettings, access: tuple[Any, Any] | None, review: ReviewSettings):
    """Off by default; a missing Google reader is a source failure at build time."""
    if not review.enabled or not review.important_mail:
        return None
    from gremlin_town.review.important_mail import MAIL_REQUEST_TIMEOUT, ImportantMailSource
    from gremlin_town.sources.google.mail import MailReader

    reader = (MailReader(google, access[1], request_timeout=min(MAIL_REQUEST_TIMEOUT, review.important_mail_deadline_seconds))
              if access is not None else None)
    return ImportantMailSource(review.important_mail_log, reader.is_unread if reader is not None else None,
                               deadline_seconds=review.important_mail_deadline_seconds)


def _review_open_requests_reader(
    service: GremlinTownService, policy: Policy, sender_participant: str,
) -> Callable[[], tuple[Any, ...]] | None:
    """A review reader for recent open work, stale work, and proved results,
    or `None` when `sender_participant` (`exosuit-review`) is not yet an enrolled
    participant - it needs a read-only grant Nic has not created yet (morning queue).

    Leaves out the sender's own deliveries (the review and daytime returns it hands to
    Diablo). The enabled poll closes those on courier evidence or a settled day;
    they never belong in Nic's "Waiting on" section."""

    try:
        policy.participant(sender_participant)
    except GremlinTownError:
        return None

    def read() -> tuple[Any, ...]:
        from brain_core.review import OpenRequest
        from brain_core.timewords import today_in_singapore

        actor = Actor(actor_id=sender_participant)
        items = {}
        for filters in ({"open": True}, {"stale": True}, {"work_state": "result_returned"}):
            cursor = None
            while True:
                page = service.requests_list(actor, filters, cursor)
                for item in page.items:
                    if filters.get("work_state") and not item.get("proof_verified"):
                        continue
                    items[item["request_id"]] = item
                cursor = page.next_cursor
                if cursor is None:
                    break
        return tuple(
            OpenRequest(
                id=item["request_id"], handed_to=item["recipient_id"],
                # The Singapore date, like every other date in the review (was the UTC date).
                since=(
                    today_in_singapore(item["updated_at"])
                    if item["updated_at"].tzinfo is not None
                    else item["updated_at"].date()
                ),
                objective=item["objective"] or "",
                # So "Waiting on" can say a result is back but not yet checked (step E §12).
                work_state=item["work_state"] or "", check=item["check"],
                proof=item.get("proof"), proof_verified=item.get("proof_verified", False),
                last_activity=item.get("last_activity"),
            )
            for item in items.values()
            if item["requester_id"] != sender_participant
        )

    return read


def _review_request_status_reader(
    service: GremlinTownService, policy: Policy, sender_participant: str,
) -> Callable[[tuple[str, ...]], tuple[Any, ...]] | None:
    """A `ReviewSources.request_status` adapter (step E §12, ROA-21), or `None` until
    `sender_participant` is enrolled, like `_review_open_requests_reader`.

    Of the requests asked about, returns those whose result has come back, with step B's
    check state and proof. Results all have `work_state` `result_returned`, so it reads
    their pages, leaving stale unproved work to the weekly review. A request still
    being worked on is simply not in it, and nothing comes back for it. The sender's own
    deliveries are left out, as in "Waiting on"."""

    try:
        policy.participant(sender_participant)
    except GremlinTownError:
        return None

    def read(request_ids: tuple[str, ...]) -> tuple[Any, ...]:
        from brain_core.moments import RequestStatus

        wanted = frozenset(request_ids)
        items = []
        cursor = None
        while True:
            page = service.requests_list(Actor(actor_id=sender_participant), {"work_state": "result_returned", "stale": False}, cursor)
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        return tuple(
            RequestStatus(
                id=item["request_id"], handed_to=item["recipient_id"],
                work_state=item["work_state"], check=item["check"], objective=item["objective"] or "",
                proof=item.get("proof"), proof_verified=item.get("proof_verified", False),
                last_activity=item.get("last_activity"),
            )
            for item in items
            if item["request_id"] in wanted and item["requester_id"] != sender_participant
        )

    return read


def _schedule_reader(settings: FlowSavvySettings):
    """No network or credential access; the service owns snapshot refreshes."""
    from gremlin_town.flowsavvy.snapshot import read_snapshot
    return lambda now: read_snapshot(settings.state_file, now, max_age_seconds=settings.max_age_seconds)


def _schedule_wake(settings: FlowSavvySettings):
    from gremlin_town.flowsavvy.state import notify_change
    return (lambda: notify_change(settings)) if settings.enabled else None


def _orientation_brain_storage(brain: BrainMCPSettings | None) -> Any | None:
    """The `GitHubBrainHomeStorage` orientation's `brain_source.py` reads through, or
    `None` when `[brain_core]` isn't configured. Reuses `brain_assembly`'s own credential
    handling (a fixed token or a GitHub App) rather than re-deriving it (step F spec §8:
    the item reader/continuity.list adapter, read-only, independent of the async
    `BrainRuntime`/MCP authorization layer `build_brain_runtime` also builds from `brain`)."""

    if brain is None:
        return None
    from brain_core.github_storage import GitHubBrainHomeStorage
    from gremlin_town.mcp.brain_assembly import _brain_home_credentials

    token, transport = _brain_home_credentials(brain, None, None)
    return GitHubBrainHomeStorage(
        repository=brain.github_repository, branch=brain.github_branch, token=token, transport=transport,
        archive_superseded=brain.archive_superseded,
    )


def _cached_brain_storage(brain: BrainMCPSettings) -> tuple[Any, Any | None]:
    """A `GitHubBrainHomeStorage` that reuses the hub's own copy of Brain Home (the Brain MCP's
    `brain-home-copy.sqlite3` beside its decision state), so the handoff-note keeper's scan each
    poll downloads only what changed. Returns the storage and the copy's file, to close. The
    copy is never canonical: GitHub is still read for every scan, and a missing or broken copy
    only costs downloads (`mcp/brain_home_copy.py`)."""

    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache
    from gremlin_town.mcp.brain_assembly import _brain_home_credentials
    from gremlin_town.mcp.brain_home_copy import open_copy_store

    token, transport = _brain_home_credentials(brain, None, None)
    blob_store = open_copy_store(brain.decision_staging_dir)
    storage = GitHubBrainHomeStorage(
        repository=brain.github_repository, branch=brain.github_branch, token=token, transport=transport,
        object_cache=GitHubObjectCache(blob_store=blob_store),
        archive_superseded=brain.archive_superseded,
    )
    return storage, blob_store


def _brain_home_owner(brain: BrainMCPSettings | None) -> Any | None:
    """The personal Brain Home owner from the Brain owner policy - whose notes agents' own
    `bc_continuity_*` tools read - or None without `[brain_core]` or such an owner."""

    if brain is None:
        return None
    from gremlin_town.mcp.brain_assembly import load_owner_policy

    policy = load_owner_policy(brain.policy_file)
    return next((owner for owner in policy.owners if owner.selector.kind == "personal"), None)


def _positive_int(section: dict[str, Any], field: str, label: str) -> int:
    value = section.get(field)
    if type(value) is not int or value <= 0:
        raise ConfigurationError(f"{label} must be a positive integer")
    return value


def _github_app_provider(app_id: int, installation_id: int, key_path: Path, repository: str,
                         permissions: dict[str, str]) -> GitHubAppTokenProvider:
    """Parse the key now so a bad file fails at startup; tokens are minted on first use."""
    try:
        private_key = load_private_key(key_path)
    except GitHubAppError as exc:
        raise ConfigurationError(str(exc)) from exc
    return GitHubAppTokenProvider(app_id=app_id, installation_id=installation_id, private_key=private_key,
                                  repository=repository, permissions=permissions)


def _read_provider_secret(agentmail: dict[str, Any]) -> str:
    path = _credential_path_from_env(agentmail, "api_key_file_env")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError("provider credential file is unreadable") from exc
    if not secret:
        raise ConfigurationError("provider credential file is empty")
    return secret


def _mcp_settings(config: dict[str, Any], base: Path, settings: ServerSettings,
                  public: PublicGatewaySettings | None) -> MCPSettings | None:
    raw = config.get("mcp", {})
    if not isinstance(raw, dict):
        raise ConfigurationError("[mcp] must be a table")
    _reject_unknown(raw, RUNTIME_CONFIG_SCHEMA["mcp"], "mcp")
    if not _boolean(raw.get("enabled"), "mcp.enabled", default=False):
        return None
    # Do this before reading a secret or constructing an OAuth provider/store.
    if settings.operator_only or not settings.normal_clients_allowed or not settings.outbound_dispatch_allowed or not settings.mcp_allowed:
        raise ConfigurationError("MCP must be disabled in recovery mode")

    def origin(name: str) -> str:
        value = _required_text(raw, name).rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ConfigurationError(f"mcp.{name} must be an HTTPS origin")
        try:
            parsed.port
        except ValueError as exc:
            raise ConfigurationError(f"mcp.{name} has an invalid port") from exc
        return value

    def path(value: Any, name: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"missing configuration value: {name}")
        resolved = Path(value.strip())
        return (resolved if resolved.is_absolute() else base / resolved).resolve()

    if type(raw.get("port", 8769)) is not int:
        raise ConfigurationError("mcp.port must be an integer port")
    port = _port(raw.get("port", 8769), "mcp.port")
    if port in {settings.port, public.port if public else None}:
        raise ConfigurationError("MCP must use a distinct integer listener port")
    public_url, private_url = origin("public_url"), origin("private_url")
    if public_url == private_url:
        raise ConfigurationError("MCP public and private origins must differ")
    single = {"client_id", "client_secret_file", "redirect_uris"} & set(raw)
    if "client" in raw:
        if single:
            raise ConfigurationError("[mcp] uses [[mcp.client]] entries or the single-client keys, not both")
        entries = raw["client"]
        if not isinstance(entries, list) or not entries or not all(isinstance(entry, dict) for entry in entries):
            raise ConfigurationError("[[mcp.client]] must list at least one client table")
    elif single:
        # The single-client form still requires its secret file, as it always has.
        path(raw.get("client_secret_file"), "client_secret_file")
        entries = [{**SINGLE_CLIENT_MCP, **{key: raw[key] for key in single}}]
    else:
        raise ConfigurationError("[mcp] requires at least one [[mcp.client]] entry")
    clients = []
    for entry in entries:
        _reject_unknown(entry, MCP_CLIENT_CONFIG_KEYS, "mcp.client")
        redirects, origins = entry.get("redirect_uris"), entry.get("origins", [])
        if not isinstance(redirects, list) or not redirects or any(not isinstance(uri, str) or not uri for uri in redirects):
            raise ConfigurationError("mcp.redirect_uris must be a nonempty exact callback list")
        if not isinstance(origins, list) or any(not isinstance(value, str) or not value for value in origins):
            raise ConfigurationError("mcp.client origins must be a list of exact HTTPS origins")
        secret = entry.get("client_secret_file")
        clients.append(MCPClientSettings(
            participant=_required_text(entry, "participant"), name=_required_text(entry, "name"),
            client_id=_required_text(entry, "client_id"),
            client_secret_file=None if secret is None else path(secret, "client_secret_file"),
            redirect_uris=tuple(redirects), origins=tuple(origins),
        ))
    if (len({client.client_id for client in clients}) != len(clients)
            or len({client.participant for client in clients}) != len(clients)):
        raise ConfigurationError("MCP clients need distinct client IDs and participants")
    state_dir = path(raw.get("state_dir"), "state_dir")
    if settings.database.resolve().is_relative_to(state_dir):
        raise ConfigurationError("MCP OAuth state must be separate from the work database directory")
    max_grant_days = raw.get("max_grant_days", MCP_MAX_GRANT_DAYS)
    lowest, highest = MCP_MAX_GRANT_DAYS_RANGE
    if type(max_grant_days) is not int or not lowest <= max_grant_days <= highest:
        raise ConfigurationError(f"mcp.max_grant_days must be a whole number of days from {lowest} to {highest}")
    return MCPSettings(
        host=_loopback_host(raw.get("host", "127.0.0.1"), "mcp.host"), port=port,
        public_url=public_url, private_url=private_url, state_dir=state_dir, clients=tuple(clients),
        max_grant_days=max_grant_days,
    )


def _brain_mcp_settings(
    config: dict[str, Any],
    base: Path,
    settings: ServerSettings,
    mcp: MCPSettings | None,
) -> BrainMCPSettings | None:
    raw = config.get("brain_core", {})
    if not isinstance(raw, dict):
        raise ConfigurationError("[brain_core] must be a table")
    _reject_unknown(raw, RUNTIME_CONFIG_SCHEMA["brain_core"], "brain_core")
    if not _boolean(raw.get("enabled"), "brain_core.enabled", default=False):
        return None
    if mcp is None:
        raise ConfigurationError("Brain Core MCP requires [mcp].enabled=true")
    if settings.operator_only or not settings.normal_clients_allowed or not settings.mcp_allowed:
        raise ConfigurationError("Brain Core MCP must be disabled in recovery mode")

    def path(name: str) -> Path:
        value = Path(_required_text(raw, name))
        return (value if value.is_absolute() else base / value).resolve()

    connector_url = _required_text(raw, "connector_url").rstrip("/")
    try:
        parsed = urlsplit(connector_url)
        connector_hostname = parsed.hostname or ""
        connector_port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("brain_core.connector_url must use a valid origin and port") from exc
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path:
        raise ConfigurationError("brain_core.connector_url must be an origin without credentials, path, query or fragment")
    if parsed.scheme not in {"http", "https"}:
        raise ConfigurationError("brain_core.connector_url must use HTTP loopback or HTTPS")
    if not connector_port:
        raise ConfigurationError("brain_core.connector_url must use an exact non-wildcard host and port")
    try:
        connector_address = ipaddress.ip_address(connector_hostname)
    except ValueError:
        connector_address = None
        labels = connector_hostname.split(".")
        if (
            len(connector_hostname) > 253
            or connector_hostname.endswith(".")
            or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                for label in labels
            )
        ):
            raise ConfigurationError("brain_core.connector_url must use a valid exact DNS hostname or IP literal")
    if connector_address is not None and (connector_address.is_unspecified or connector_address.is_multicast):
        raise ConfigurationError("brain_core.connector_url must use an exact non-wildcard host and port")
    if parsed.scheme == "http" and (connector_address is None or not connector_address.is_loopback):
        raise ConfigurationError("non-loopback Brain connector URLs require HTTPS")

    repository = _required_text(raw, "github_repository")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ConfigurationError("brain_core.github_repository must be an exact owner/name")
    branch = _required_text(raw, "github_branch")
    if (
        branch.startswith(("/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or any(value in branch for value in ("..", "//", "@{", "\\", "\0", "\r", "\n", " "))
    ):
        raise ConfigurationError("brain_core.github_branch is invalid")
    app_fields = ("github_app_id", "github_installation_id", "github_app_private_key_file")
    app_mode = any(field in raw for field in app_fields)
    if app_mode == ("github_token_file" in raw):
        raise ConfigurationError(
            "[brain_core] needs exactly one of github_token_file or the github_app_id, "
            "github_installation_id and github_app_private_key_file settings"
        )
    return BrainMCPSettings(
        connector_url=connector_url,
        connector_token_file=path("connector_token_file"),
        github_repository=repository,
        github_branch=branch,
        github_token_file=None if app_mode else path("github_token_file"),
        policy_file=path("policy_file"),
        decision_staging_dir=path("decision_staging_dir"),
        ownership_catalog_file=path("ownership_catalog_file"),
        source_registry_file=path("source_registry_file"),
        github_app_id=_positive_int(raw, "github_app_id", "brain_core.github_app_id") if app_mode else None,
        github_installation_id=(
            _positive_int(raw, "github_installation_id", "brain_core.github_installation_id") if app_mode else None
        ),
        github_app_private_key_file=path("github_app_private_key_file") if app_mode else None,
        archive_superseded=_boolean(raw.get("archive_superseded"), "brain_core.archive_superseded", default=False),
    )


def build_runtime(
    config_path: str | Path,
    *,
    provider: Any | None = None,
    start_worker: bool = True,
) -> Runtime:
    config_path = Path(config_path).resolve(strict=True)
    config = _read_toml(config_path)
    _reject_unknown(config, frozenset(RUNTIME_CONFIG_SCHEMA), "top-level")
    server = _required_table(config, "server")
    _reject_unknown(server, RUNTIME_CONFIG_SCHEMA["server"], "server")
    recovery = config.get("recovery", {})
    if not isinstance(recovery, dict):
        raise ConfigurationError("[recovery] must be a table")
    _reject_unknown(recovery, RUNTIME_CONFIG_SCHEMA["recovery"], "recovery")
    participant_config = _required_table(config, "participants")
    _reject_unknown(participant_config, RUNTIME_CONFIG_SCHEMA["participants"], "participants")
    participant_path = Path(_required_text(participant_config, "file"))
    if not participant_path.is_absolute():
        participant_path = (config_path.parent / participant_path).resolve()
    policy, secrets = _load_participants(participant_path)
    worker_config = config.get("worker", {})
    if not isinstance(worker_config, dict):
        raise ConfigurationError("[worker] must be a table")
    _reject_unknown(worker_config, RUNTIME_CONFIG_SCHEMA["worker"], "worker")
    poll_seconds = worker_config.get("poll_seconds", 1.0)
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)):
        raise ConfigurationError("worker.poll_seconds must be a number")
    settings = ServerSettings(
        host=_loopback_host(server.get("host", "127.0.0.1"), "server.host"),
        port=_port(server.get("port", 8765), "server.port"),
        database=Path(_required_text(server, "database")),
        operator_only=_boolean(server.get("operator_only"), "server.operator_only", default=False),
        normal_clients_allowed=_boolean(recovery.get("normal_clients_allowed"), "recovery.normal_clients_allowed", default=True),
        outbound_dispatch_allowed=_boolean(recovery.get("outbound_dispatch_allowed"), "recovery.outbound_dispatch_allowed", default=True),
        worker_poll_seconds=max(0.05, float(poll_seconds)),
        mcp_allowed=_boolean(recovery.get("mcp_allowed"), "recovery.mcp_allowed", default=True),
    )
    public_gateway: PublicGatewaySettings | None = None
    public_config = config.get("public_gateway")
    if public_config is not None:
        if not isinstance(public_config, dict):
            raise ConfigurationError("[public_gateway] must be a table")
        _reject_unknown(public_config, RUNTIME_CONFIG_SCHEMA["public_gateway"], "public_gateway")
        public_gateway = PublicGatewaySettings(
            host=_loopback_host(public_config.get("host", "127.0.0.1"), "public_gateway.host"),
            port=_port(public_config.get("port", 8766), "public_gateway.port"),
            actor_id=_required_text(public_config, "actor_id"),
        )
        if public_gateway.port == settings.port:
            raise ConfigurationError("private and public listeners must use different ports")
        expected_operations = {
            "participants.list",
            "requests.create",
            "requests.list",
            "requests.read",
            "requests.report",
            "requests.reply",
            "requests.cancel",
            "requests.check",
            "operations.get",
            "events.list",
        }
        configured_operations = public_config.get("allowed_operations")
        if not isinstance(configured_operations, list) or set(configured_operations) != expected_operations or len(configured_operations) != len(expected_operations):
            raise ConfigurationError("public_gateway.allowed_operations must be the exact ten-operation allowlist")
        try:
            public_participant = policy.participant(
                public_gateway.actor_id,
                require_enabled=False,
            )
        except Exception as exc:
            raise ConfigurationError("public_gateway.actor_id is not a configured participant") from exc
        if public_participant.is_operator:
            raise ConfigurationError("public gateway cannot accept an operator identity")
    mcp = _mcp_settings(config, config_path.parent, settings, public_gateway)
    brain = _brain_mcp_settings(config, config_path.parent, settings, mcp)
    sitreps_config = config.get("sitreps", {})
    if not isinstance(sitreps_config, dict):
        raise ConfigurationError("[sitreps] must be a table")
    _reject_unknown(sitreps_config, RUNTIME_CONFIG_SCHEMA["sitreps"], "sitreps")
    sitreps_enabled = _boolean(sitreps_config.get("enabled"), "sitreps.enabled", default=False)
    if sitreps_enabled and brain is None:
        raise ConfigurationError("[sitreps] enabled requires [brain_core] enabled")
    for client in mcp.clients if mcp is not None else ():
        try:
            enrolled = policy.participant(client.participant)
        except GremlinTownError as exc:
            raise ConfigurationError(f"MCP requires enabled {client.participant} enrollment") from exc
        if enrolled.is_operator or any(item.actor_id == client.participant for item in secrets):
            raise ConfigurationError(f"MCP requires a non-operator OAuth-only {client.participant} participant")
    transport_config = config.get("transport", {})
    if not isinstance(transport_config, dict):
        raise ConfigurationError("[transport] must be a table")
    _reject_unknown(transport_config, RUNTIME_CONFIG_SCHEMA["transport"], "transport")
    default_kind = transport_config.get("default_backend", "agentmail")
    if default_kind not in ("agentmail", "github"):
        raise ConfigurationError("transport.default_backend must be agentmail or github")
    for section in ("agentmail", "github", "work"):
        if section in config:
            if not isinstance(config[section], dict):
                raise ConfigurationError(f"[{section}] must be a table")
            _reject_unknown(config[section], RUNTIME_CONFIG_SCHEMA[section], section)
    default_binding = "agentmail"
    door_participant = config.get("work", {}).get("door_participant", "hermes-wa")
    if not isinstance(door_participant, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", door_participant):
        raise ConfigurationError("work.door_participant must be a participant ID")
    from gremlin_town.mcp.surface_door import configured_code_callers
    try:
        code_callers = configured_code_callers(config.get("work"))
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    configured_providers = {}
    github_config = config.get("github")
    if github_config is not None:
        writer_id = github_config.get("writer_user_id")
        if type(writer_id) is not int or writer_id <= 0:
            raise ConfigurationError("github.writer_user_id must be a verified positive integer")
        owner = _required_text(github_config, "owner")
        repository = _required_text(github_config, "repository")
        legacy_writers = github_config.get("legacy_writer_user_ids", [])
        if (not isinstance(legacy_writers, list)
                or any(type(item) is not int or item <= 0 for item in legacy_writers)
                or writer_id in legacy_writers):
            raise ConfigurationError("github.legacy_writer_user_ids must list other positive integer user IDs")
        app_keys = ("app_id", "installation_id", "app_private_key_file_env")
        app_mode = any(key in github_config for key in app_keys)
        if app_mode == ("api_key_file_env" in github_config):
            raise ConfigurationError(
                "[github] needs exactly one of api_key_file_env or the app_id, installation_id "
                "and app_private_key_file_env settings"
            )
        if app_mode:
            app_id = _positive_int(github_config, "app_id", "github.app_id")
            installation_id = _positive_int(github_config, "installation_id", "github.installation_id")
        github_binding = f"github:{owner}/{repository}"
        if default_kind == "github":
            default_binding = github_binding
        if provider is None:
            try:
                if app_mode:
                    token, token_provider = "", _github_app_provider(
                        app_id, installation_id,
                        _credential_path_from_env(github_config, "app_private_key_file_env"),
                        f"{owner}/{repository}", {"issues": "write"},
                    )
                else:
                    token, token_provider = _read_provider_secret(github_config), None
                configured_providers[github_binding] = GitHubTransport(GitHubConfig(
                    owner=owner, repository=repository, token=token, writer_user_id=writer_id,
                    token_provider=token_provider, legacy_writer_user_ids=tuple(legacy_writers),
                ))
            except ValueError as exc:
                raise ConfigurationError("invalid GitHub transport configuration") from exc
    elif default_kind == "github":
        raise ConfigurationError("github default requires [github] configuration")
    if provider is None:
        if "agentmail" in config:
            agentmail = config["agentmail"]
            base_url = str(agentmail.get("base_url", "https://api.agentmail.to"))
            configured_providers["agentmail"] = AgentMailTransport(AgentMailConfig(
                inbox_id=_required_text(agentmail, "inbox_id"),
                api_key=_read_provider_secret(agentmail),
                base_url=base_url.rstrip("/") + "/v0/",
            ))
        if default_binding not in configured_providers:
            raise ConfigurationError("default transport is not configured")
    store = SQLiteStore(settings.database)
    if provider is None:
        provider = BoundTransport(store, configured_providers)
    service = GremlinTownService(store, policy, provider=provider, default_backend=default_binding)
    worker = OutboxWorker(store, policy, provider)
    resolver = _ParticipantResolver(policy, _token_resolver(
        policy, secrets, operator_only=settings.operator_only,
        normal_clients_allowed=settings.normal_clients_allowed,
    ))
    def login_resolver(username: str, password: str) -> Actor | None:
        actor = resolver(password)
        if actor is None or not actor.is_operator:
            return None
        if not hmac.compare_digest(username, actor.actor_id):
            return None
        return actor

    app = create_app(service, actor_resolver=resolver, login_resolver=login_resolver)
    public_app = (
        create_public_app(service, resolver, public_gateway.actor_id)
        if public_gateway is not None
        else None
    )
    runtime = Runtime(
        door_participant=door_participant,
        code_callers=code_callers,
        settings=settings,
        policy=policy,
        store=store,
        service=service,
        worker=worker,
        app=app,
        public_app=public_app,
        public_gateway=public_gateway,
        token_resolver=resolver,
        participants_file=participant_path,
        brain=brain,
        sitreps_enabled=sitreps_enabled,
    )
    from gremlin_town.review.brain_source import (
        brain_facts_reader,
        brain_home_size_reader,
        brain_items_by_path_reader,
        hub_scans,
        brain_items_reader,
        review_snapshot_reader,
        brain_preferences_reader,
        brain_task_sitreps_reader,
        open_note_records_reader,
        open_notes_reader,
    )
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.continuity_source import GitHubContinuityStarter
    from brain_core.github_storage import SNAPSHOT_MAX_BYTES, SNAPSHOT_MAX_RECORDS

    from gremlin_town.review.handoff_notes import SCAN_MAX_BYTES, SCAN_MAX_RECORDS, HandoffNoteWriter
    from gremlin_town.review.orient import OrientationContext, OrientationSources
    from gremlin_town.review.settings import OrientationSettings, ReviewSettings
    from gremlin_town.review.why_log import WhyLog

    google = GoogleSettings.from_table(config.get("google"))
    review = ReviewSettings.from_table(config.get("review"))
    runtime.google = google
    runtime.flowsavvy = FlowSavvySettings.from_table(config.get("flowsavvy"), base=config_path.parent)
    runtime.dashboard = DashboardSettings.from_table(config.get("dashboard"), base=config_path.parent)
    runtime.discord = DiscordSettings.from_table(config.get("discord"), base=config_path.parent)
    if runtime.flowsavvy.enabled and brain is None:
        raise ConfigurationError("[flowsavvy] needs [brain_core] enabled")
    runtime.review = review
    google_access = _google_access(google)
    runtime.google_calendar_reader = _google_calendar_reader(google, google_access)
    runtime.calendar_writer = _google_calendar_writer(google, google_access, review_enabled=review.enabled)
    if runtime.flowsavvy.enabled and runtime.flowsavvy.busy_calendar and runtime.calendar_writer is None:
        raise ConfigurationError("[flowsavvy] busy_calendar needs [review] enabled and Google calendar.events")
    # Brain Home item reading, shared by review_sources (run_review/run_moments/placement) and
    # orientation, read through brain_source.py's public-interface adapters - independent of
    # runtime.brain_runtime (async, MCP-authorization-shaped; not needed for a read-only,
    # host-configured capability).
    orientation_storage = _orientation_brain_storage(brain)
    # The personal Brain Home owner stamps notes the hub starts, so agents' own continuity
    # tools read them too (N12a, N12c).
    brain_home_owner = _brain_home_owner(brain)
    if review.enabled and review.handoff_notes and (brain is None or brain_home_owner is None):
        raise ConfigurationError(
            "[review] handoff_notes requires [brain_core] with a personal Brain Home owner in its policy"
        )
    notes_storage = None
    if review.enabled:
        runtime.review_why_log = WhyLog(review.why_log_file)
        if brain is not None:
            # Open notes (N12b-d) and the hub's handoff notes (N12a) read through the hub's
            # copy of Brain Home, so each poll downloads only what changed.
            notes_storage, runtime.review_blob_store = _cached_brain_storage(brain)
    else:
        runtime.review_why_log = None
    # While [review] is on, items, facts and preferences read through that same copy too, so a
    # placement run, the 06:00 review and "where am I?" fetch only the branch and what changed,
    # not one GitHub request per record file (30 Sep 2026: settling the day took 19.2 s of
    # Diablo's 20 s; docs/product/2026-09-30-diablo-speed-and-token-fixes-plan.md, finding 6).
    # The copy keeps Git objects by their ID, so it can't go stale; the branch is still read on
    # every call. With [review] off, the plain storage, as before.
    reader_storage = notes_storage if notes_storage is not None else orientation_storage
    # Through the copy, every reader scans within the hub's one limit, 5,000 files or 32 MiB a scan
    # (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1). Only with the copy's file,
    # though: the review, the moments poll and placement are fresh processes every few minutes, so with
    # the object cache in memory alone each would download every file each time, and at 5,000 files could
    # spend GitHub's hourly allowance and take agents' saves down with it. So with the file missing, and
    # with the plain storage, every reader keeps Brain Core's defaults, 512 files and 8 MiB (Brain Core
    # refuses a higher cap with no object cache at all).
    copied = notes_storage is not None and runtime.review_blob_store is not None
    if notes_storage is not None and not copied:
        logger.warning(
            "Brain Home copy file unavailable: Brain Home readers keep the 512-file limit and read from GitHub"
        )
    limits: dict[str, int] = {"max_records": SCAN_MAX_RECORDS, "max_bytes": SCAN_MAX_BYTES} if copied else {}
    brain_items = brain_items_reader(reader_storage, **limits) if reader_storage is not None else None
    runtime.review_read_snapshot = review_snapshot_reader(reader_storage, **limits) if reader_storage is not None else None
    brain_items_by_path = (
        brain_items_by_path_reader(reader_storage, **limits) if reader_storage is not None else None
    )
    if review.enabled:
        runtime.review_sources = ReviewSources(
            important_emails=_review_important_emails(google, google_access, review),
            brain_items=brain_items,
            brain_items_by_path=brain_items_by_path,
            calendar_events=runtime.google_calendar_reader,
            open_requests=_review_open_requests_reader(service, policy, review.sender_participant),
            # Nic's answers to the review's clarifying questions (§3.1), same storage.
            brain_facts=brain_facts_reader(reader_storage, **limits) if reader_storage is not None else None,
            open_notes=(
                open_note_records_reader(notes_storage, **limits) if notes_storage is not None else None
            ),
            # Requests Nic was shown in "Waiting on" that came back or closed (§12, ROA-21).
            request_status=_review_request_status_reader(service, policy, review.sender_participant),
            # N8: with calendar entries on, a task with a start time is added automatically,
            # so "Calendar entries to add" leaves it out (step E spec §3).
            calendar_adds_timed=runtime.calendar_writer is not None,
            # Nic's focus projects rank the review's "Coming up" (N16; step F §3, rule 5).
            brain_preferences=(
                brain_preferences_reader(reader_storage, **limits) if reader_storage is not None else None
            ),
            # Focus projects with no next action (PRD §4.10), only while [sitreps] is on: read
            # through the hub's copy of Brain Home, within the hub's own sitrep read limits.
            task_sitreps=(
                brain_task_sitreps_reader(
                    notes_storage if notes_storage is not None else orientation_storage, **limits,
                )
                if sitreps_enabled and orientation_storage is not None else None
            ),
            # The 06:00 review's "Brain Home size" line, from one tree read (the storage-limit plan,
            # section 5): said from 50% and again from 80% of any scan's limit, nothing below.
            # Against the limits in force: without the copy file the readers stop at 512, so it warns before that.
            brain_home_size=(
                brain_home_size_reader(
                    notes_storage,
                    hub_scans(**(limits or {"max_records": SNAPSHOT_MAX_RECORDS, "max_bytes": SNAPSHOT_MAX_BYTES})),
                )
                if notes_storage is not None else None
            ),
        )
        if review.handoff_notes:
            runtime.review_handoff_notes = HandoffNoteWriter(
                notes_storage, owner_id=brain_home_owner.owner_id, owner_locator=brain_home_owner.owner_locator,
                # The hub's limits only with the copy's file, as for the readers above.
                **(limits or {"max_records": SNAPSHOT_MAX_RECORDS, "max_bytes": SNAPSHOT_MAX_BYTES}),
            )
    else:
        runtime.review_sources = ReviewSources(brain_items=brain_items, brain_items_by_path=brain_items_by_path)
    from gremlin_town.review.settings import PlacementSettings

    placement_settings = PlacementSettings.from_table(config.get("placement"), review_enabled=review.enabled)
    if placement_settings.enabled and not runtime.flowsavvy.enabled:
        from gremlin_town.review.placement import PlacementContext

        if placement_settings.write and runtime.calendar_writer is None:
            raise ConfigurationError("[placement] write needs the Google credential with calendar.events")
        runtime.placement_context = PlacementContext(
            settings=placement_settings, why_log=runtime.review_why_log, sources=runtime.review_sources,
            writer=runtime.calendar_writer,
            calendar_events=_google_calendar_reader(google, google_access, strict=True),
        )
    if review.enabled:
        from gremlin_town.api.review import install_review_routes

        install_review_routes(app, runtime)

    orientation = OrientationSettings.from_table(config.get("orientation"), review_enabled=review.enabled)
    if orientation.schedule and not runtime.flowsavvy.enabled:
        raise ConfigurationError("[orientation] schedule needs [flowsavvy] enabled")
    if runtime.flowsavvy.enabled:
        orientation = replace(orientation, blocks=False)
    runtime.orientation = orientation
    # Built unconditionally, like runtime.review_sources above: overview()/what_next()/respond()'s
    # own `settings.enabled` check (step F spec §2) is what actually gates reads and writes.
    # Orientation reads the same why-log review does, so it needs no resource of its own,
    # and now shares runtime.review_sources directly (its brain_items/brain_items_by_path
    # are wired above, alongside review's own calendar/open_requests readers - one
    # ReviewSources instance, not two). open_notes, preferences, task_sitreps and continuity
    # (starting the running note on yes/other) are orientation-only, via brain_source.py/
    # continuity_source.py against the same storage; continuity's own confirmed-record-or-clear-failure
    # guarantee comes from publish_record, independent of brain_runtime too.
    async def orientation_work_door(caller, request):
        """Relay the authenticated orientation caller through the existing owner door."""
        from gremlin_town.mcp.brain_runtime import BrainRuntimeError, OwnerSelector

        if runtime.brain_runtime is None:
            raise BrainRuntimeError("NOT_CONFIGURED", "Orientation Start has no Brain runtime",
                                    field="work_door", fix="configure the host BrainRuntime.change_work door")
        return await runtime.brain_runtime.change_work(caller, OwnerSelector("personal"), request)

    runtime.orientation_context = OrientationContext(
        orientation_settings=orientation,
        review_settings=review,
        placement_enabled=runtime.placement_context is not None,
        schedule_enabled=orientation.schedule,
        why_log=runtime.review_why_log,
        work_door=orientation_work_door,
        door_participant=runtime.door_participant,
        sources=OrientationSources(
            review=runtime.review_sources,
            schedule=(
                _schedule_reader(runtime.flowsavvy) if orientation.schedule else None
            ),
            # Through the hub's copy of Brain Home when [review] is on (it always is with
            # [orientation] on): the hub's own notes (N12a) soon outgrow an uncached scan.
            open_notes=(
                open_notes_reader(notes_storage, **limits) if notes_storage is not None
                else open_notes_reader(orientation_storage) if orientation_storage is not None
                else None
            ),
            # Nic's preferences, for his focus projects (N4; step F §3, rule 5).
            preferences=brain_preferences_reader(reader_storage, **limits) if reader_storage is not None else None,
            # His projects' open tasks from the hub's task sitreps (N4, ROA-60), only while
            # [sitreps] is on: off, nothing is read and both asks answer exactly as before. Read
            # through the hub's copy of Brain Home when there is one, within the hub's own sitrep
            # read limits, since every note save adds a sitrep version.
            task_sitreps=(
                brain_task_sitreps_reader(
                    notes_storage if notes_storage is not None else orientation_storage, **limits,
                )
                if sitreps_enabled and orientation_storage is not None else None
            ),
        ),
        # The note a yes starts first looks for an open one: through the hub's copy, within the
        # hub's limits, like every other reader (the storage-limit plan, layer 1).
        continuity=(
            GitHubContinuityStarter(
                reader_storage,
                **limits,
                **(
                    {"owner_id": brain_home_owner.owner_id, "owner_locator": brain_home_owner.owner_locator}
                    if brain_home_owner is not None else {}
                ),
            )
            if reader_storage is not None else None
        ),
    )
    app.state.runtime = runtime

    if mcp is not None:
        from gremlin_town.oauth import OAuthClient, OAuthConfig, OAuthProvider
        from gremlin_town.mcp.auth import ProductionAuth
        from gremlin_town.mcp.brain_tools import build_brain_tools
        from gremlin_town.mcp.server import build_server
        from gremlin_town.mcp.private_controls import install_private_controls

        def mcp_allowed() -> bool:
            active = runtime.settings
            return not (active.operator_only or not active.normal_clients_allowed
                        or not active.outbound_dispatch_allowed or not active.mcp_allowed)

        def participant_enabled(participant_id: str) -> bool:
            if not mcp_allowed():
                return False
            try:
                participant = runtime.policy.participant(participant_id)
            except GremlinTownError:
                return False
            return not participant.is_operator

        try:
            oauth_config = OAuthConfig(
                db_path=mcp.state_dir / "oauth.sqlite3", issuer_url=mcp.public_url,
                resource_url=mcp.public_url + "/mcp", private_base_url=mcp.private_url,
                clients=tuple(OAuthClient(
                    client_id=client.client_id, participant=client.participant, name=client.name,
                    redirect_uris=client.redirect_uris, origins=client.origins,
                    client_secret=None if client.client_secret_file is None
                    else client.client_secret_file.read_text(encoding="utf-8").strip(),
                ) for client in mcp.clients),
                max_grant_seconds=mcp.max_grant_days * 86400,
            )
            oauth = OAuthProvider(oauth_config, participant_enabled=participant_enabled)
            auth = ProductionAuth(oauth, policy, enabled=mcp_allowed)
            brain_tools = ()
            if brain is not None:
                from gremlin_town.mcp.brain_assembly import build_brain_runtime

                runtime.brain_runtime = build_brain_runtime(
                    brain, review_enabled=review.enabled, calendar=runtime.calendar_writer,
                    sitreps_enabled=sitreps_enabled,
                    why_log=runtime.review_why_log, door_participant=runtime.door_participant,
                    code_callers=runtime.code_callers,
                    schedule_wake=_schedule_wake(runtime.flowsavvy),
                    schedule_lengths=runtime.flowsavvy.length_flow,
                    past_appointment_done=review.past_appointment_done,
                )
                from gremlin_town.mcp.brain_usage import BrainUsageLog
                runtime.brain_usage = BrainUsageLog(brain.decision_staging_dir / "brain-usage.sqlite3")
                from gremlin_town.api.brain import install_brain_routes
                install_brain_routes(app, runtime, door_enabled=True)
                if public_app is not None:
                    install_brain_routes(public_app, runtime)
                brain_tools = build_brain_tools(
                    runtime.brain_runtime,
                    auth.actor,
                    scopes_provider=auth.scopes,
                    authorize=auth.authorize,
                    usage_log=runtime.brain_usage,
                    transport="mcp",
                    # bc_calendar_remove, only while calendar entries are on (N8).
                    door=False,
                    # bc_project_save, a Ciel feature, only while [orientation] is on (PRD §4.10).
                )
                # bc_orient, bc_next and bc_next_respond, registered only when [orientation] is
                # enabled (step F spec §2, §8); disabled is 422 "unsupported Brain operation" over
                # HTTP and simply absent from the MCP tool list, matching every other bc_*
                # operation this codebase doesn't register - not a literal 404, which would
                # need special-casing this one operation in api/brain.py's generic dispatch.
                if orientation.enabled:
                    from gremlin_town.mcp.brain_tools import build_orientation_tools

                    brain_tools = list(brain_tools) + build_orientation_tools(
                        runtime.orientation_context,
                        auth.actor,
                        scopes_provider=auth.scopes,
                        authorize=auth.authorize,
                    )
            bundle = build_server(service, oauth, auth, brain_tools=brain_tools)
            runtime.mcp, runtime.oauth_provider, runtime.mcp_auth = mcp, oauth, auth
            runtime.mcp_server, runtime.mcp_app = bundle.server, bundle.app
            install_private_controls(app, oauth)
        except BaseException:
            store.close()
            raise

    @app.get("/health/live", include_in_schema=False)
    async def health_live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False, response_model=None)
    async def health_ready() -> Any:
        if runtime.worker_error is not None:
            return JSONResponse(
                {"status": "not_ready", "reason": "outbox_worker_failed"},
                status_code=503,
            )
        runtime.store.fetchone("SELECT 1")
        return {"status": "ready"}

    @app.get("/health/capacity", include_in_schema=False, response_model=None)
    async def health_capacity(request: Request) -> Any:
        try:
            actor = get_actor(request)
            runtime.policy.require_operator(actor)
        except (AuthenticationError, GremlinTownError):
            return JSONResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "authentication required"}},
                status_code=401,
            )
        usage = runtime.store.local_usage()
        filesystem = shutil.disk_usage(runtime.store.path.parent)
        pending = usage["queued_count"] + usage["sending_count"] + usage["uncertain_count"]
        footprint = usage["database_bytes"] + usage["wal_bytes"]
        warnings: list[str] = []
        if pending >= CAPACITY_WARNING_THRESHOLDS["pending_operations"]:
            warnings.append("pending_operations")
        if usage["retained_payload_bytes"] >= CAPACITY_WARNING_THRESHOLDS["retained_payload_bytes"]:
            warnings.append("retained_payload_bytes")
        if footprint >= CAPACITY_WARNING_THRESHOLDS["database_footprint_bytes"]:
            warnings.append("database_footprint_bytes")
        if filesystem.free <= CAPACITY_WARNING_THRESHOLDS["filesystem_free_bytes"]:
            warnings.append("filesystem_free_bytes")
        return {
            "status": "ok" if not warnings else "warning",
            "usage": {
                **usage,
                "pending_operations": pending,
                "database_footprint_bytes": footprint,
                "filesystem_free_bytes": filesystem.free,
                "filesystem_total_bytes": filesystem.total,
            },
            "warning_thresholds": dict(CAPACITY_WARNING_THRESHOLDS),
            "warnings": warnings,
        }

    async def worker_loop() -> None:
        try:
            await asyncio.to_thread(runtime.worker.recover_interrupted)
            while True:
                try:
                    receipt = await asyncio.to_thread(runtime.worker.dispatch_once)
                except GremlinTownError as exc:
                    if exc.code is not ErrorCode.RECOVERY_HOLD:
                        raise
                    receipt = None
                if receipt is None:
                    await asyncio.to_thread(runtime.worker.reconcile_once)
                await asyncio.sleep(runtime.settings.worker_poll_seconds)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            runtime.worker_error = exc
            logger.exception("outbox worker stopped")

    async def start() -> None:
        if start_worker and settings.outbound_dispatch_allowed:
            runtime.worker_task = asyncio.create_task(worker_loop(), name="gremlin-town-outbox")

    async def stop() -> None:
        if runtime.placement_late_runs:
            # A run past the signal's 12 s reply still writes the calendar, then records its plan and
            # releases its lock in the why-log: let it finish before that closes (bounded).
            from gremlin_town.api.review import finish_late_placement_runs

            await finish_late_placement_runs(runtime)
        if runtime.worker_task is not None:
            runtime.worker_task.cancel()
            try:
                await runtime.worker_task
            except asyncio.CancelledError:
                pass
        if runtime.brain_runtime is not None:
            await runtime.brain_runtime.aclose()
        runtime.close()

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", stop)
    if runtime.mcp_server is not None:
        existing_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application: FastAPI):
            # MCP must drain shielded service calls before shutdown closes SQLite.
            # The outer runtime also cleans up if MCP manager startup fails.
            async with existing_lifespan(application):
                async with runtime.mcp_server.session_manager.run():
                    yield

        app.router.lifespan_context = lifespan
    from gremlin_town.api.lifecycle import install_lifecycle_routes
    install_lifecycle_routes(runtime, participant_path)
    return runtime
