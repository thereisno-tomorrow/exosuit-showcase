"""GitHub-backed Brain Home reads and atomic record/receipt publication.

The host supplies the trusted repository, branch, and credential. This module
does not discover credentials, create repositories, clone, or create Brain Core
records. It validates and publishes the exact record and durability receipt
already produced by the shared durability writer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections import OrderedDict
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from brain_core.contracts import canonical_json
from brain_core.durability import DurabilityReceipt, RECEIPT_SCHEMA, WRITER_ID
from brain_core.errors import BrainError
from brain_core.records import SITREP_KINDS, Record, parse_record, render_record
from brain_core.routines import is_routine


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ORIGIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_RECORD_ID = re.compile(r"^brn_[0-9]{8}_[0-9]{6}_[0-9a-f]{4,16}$")
_RECORD_PATH = re.compile(
    r"^records/(?P<kind>capture|commitment|decision|continuity|entity|preference|item|fact)/"
    r"[0-9]{4}/(?:0[1-9]|1[0-2])/(?P<record_id>brn_[0-9]{8}_[0-9]{6}_[0-9a-f]{4,16})\.md$"
)
# The clear-out's archive path is this prefix plus the record's own path, byte for byte
# (step C §7, ``clearout._archive_path``).
_ARCHIVE_PREFIX = "archive/"
# Whole-record archive defaults to sitreps; other kinds require an explicit allowlist.
_ARCHIVABLE_RECORD_PATH = re.compile(
    r"^records/(?P<kind>capture|commitment|decision|continuity|entity|preference|item|fact|sitrep|focus_copy)/"
    r"[0-9]{4}/(?:0[1-9]|1[0-2])/"
    r"(?P<record_id>brn_[0-9]{8}_[0-9]{6}_[0-9a-f]{4,16})\.md$"
)
MAX_ARCHIVE_PATHS = 500
# Ciel and Brain Home PRD §4.10 (build plan slice 7): a whole project - a goal and its steps - lands
# in one commit, at most this many records.
MAX_BATCH_RECORDS = 25
_MEMORY_OPERATION_PREFIX = "mcp-memory-operation:"
_MEMORY_OPERATION_KEY = re.compile(
    r"^mcp-memory-operation:(?P<operation_id>[a-z0-9][a-z0-9-]{0,127}):"
    r"(?P<input_sha256>[0-9a-f]{64})$"
)
_DECISION_RECORD_PATH = re.compile(
    r"^records/decision/[0-9]{4}/(?:0[1-9]|1[0-2])/"
    r"brn_[0-9]{8}_[0-9]{6}_[0-9a-f]{4,16}\.md$"
)
_RECEIPT_FIELDS = frozenset(field.name for field in fields(DurabilityReceipt))
_API_VERSION = "2022-11-28"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DECISION_CANDIDATES = 1000
MAX_MEMORY_OPERATION_CANDIDATES = 1000
MAX_RECORD_CANDIDATES = 1000
SNAPSHOT_MAX_RECORDS = 512
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
# A scan that reaches this share of its file or byte cap logs a warning, kinds and counts only
# (docs/product/2026-09-30-brain-home-storage-limit-plan.md §5).
SCAN_WARNING_SHARE = 0.8
# The record kinds a scan may read. `record_counts` counts every folder under records/, these and any other.
SCAN_KINDS = frozenset({
    "capture", "commitment", "preference", "continuity", "decision", "item", "fact", *SITREP_KINDS,
})

logger = logging.getLogger(__name__)


class GitHubStorageError(RuntimeError):
    """Base failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class GitHubConflict(GitHubStorageError):
    """The selected branch or stable publication identity conflicts."""


class GitHubTransportError(GitHubStorageError):
    """The provider reply was unavailable or uncertain."""


class GitHubPublicationUncertain(GitHubTransportError):
    """Reconciliation could not prove whether the provider accepted the write."""


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HTTPResponse: ...


class UrllibHTTPTransport:
    """Small injectable GitHub HTTP transport with no credential lookup."""

    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[no-untyped-def]
            return None

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._timeout = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._opener = build_opener(self._NoRedirect())

    def _read_bounded(self, response: Any) -> bytes:
        content = response.read(self._max_response_bytes + 1)
        if len(content) > self._max_response_bytes:
            raise GitHubTransportError(
                "github_response_too_large",
                "GitHub response exceeded the configured byte limit",
                details={"max_response_bytes": self._max_response_bytes},
            )
        return content

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return HTTPResponse(response.status, dict(response.headers.items()), self._read_bounded(response))
        except HTTPError as exc:
            return HTTPResponse(exc.code, dict(exc.headers.items()), self._read_bounded(exc))
        except (URLError, TimeoutError, OSError) as exc:
            raise GitHubTransportError(
                "github_transport_unavailable",
                "GitHub transport did not return a response",
                details={"error": type(exc).__name__},
            ) from exc


@dataclass(frozen=True, slots=True)
class GitHubRevision:
    repository: str
    branch: str
    commit_sha: str
    tree_sha: str


@dataclass(frozen=True, slots=True)
class GitHubBatchHistory:
    """Verified accepted batch and its immediate pre-change revision."""

    accepted: GitHubRevision
    before: GitHubRevision


@dataclass(frozen=True, slots=True)
class GitHubObservedCommit:
    """One immutable commit and the sole parent verified by a history read."""

    revision: GitHubRevision
    parent_sha: str
    message: str


@dataclass(frozen=True, slots=True)
class GitHubCommitHistory:
    """Pinned base/head and all intervening commits, oldest first."""

    base: GitHubRevision
    head: GitHubRevision
    commits: tuple[GitHubObservedCommit, ...]


@dataclass(frozen=True, slots=True)
class GitHubFile:
    path: str
    content: bytes
    blob_sha: str


@dataclass(frozen=True, slots=True)
class GitHubRead:
    revision: GitHubRevision
    files: tuple[GitHubFile, ...]

    def file(self, path: str) -> GitHubFile:
        matches = tuple(item for item in self.files if item.path == path)
        if len(matches) != 1:
            raise KeyError(path)
        return matches[0]


@dataclass(frozen=True, slots=True)
class GitHubPublicationResult:
    status: str
    repository: str
    branch: str
    commit_sha: str
    tree_sha: str
    origin_id: str
    semantic_key: str
    input_sha256: str
    record_path: str
    receipt_path: str
    record_sha256: str
    receipt_sha256: str
    remote_accepted: bool


@dataclass(frozen=True, slots=True)
class GitHubRecordFile:
    """One record file. ``archived`` is true only when ``read_records(archived_ok=True)`` found
    the requested path's file in Brain Home's ``archive/`` folder instead; ``path`` stays the
    path that was asked for."""

    path: str
    content: bytes
    blob_sha: str
    record: Record
    archived: bool = False


@dataclass(frozen=True, slots=True)
class GitHubRecordRead:
    revision: GitHubRevision
    records: tuple[GitHubRecordFile, ...]


@dataclass(frozen=True, slots=True)
class GitHubRecordPublicationResult:
    status: str
    repository: str
    branch: str
    commit_sha: str
    tree_sha: str
    record_id: str
    record_kind: str
    record_path: str
    record_sha256: str
    remote_accepted: bool


@dataclass(frozen=True, slots=True)
class GitHubBatchRecord:
    """One record of a batch publication, as it landed: its receipt."""

    record_id: str
    record_kind: str
    record_path: str
    record_sha256: str


@dataclass(frozen=True, slots=True)
class GitHubBatchPublicationResult:
    status: str
    repository: str
    branch: str
    commit_sha: str
    tree_sha: str
    records: tuple[GitHubBatchRecord, ...]


@dataclass(frozen=True, slots=True)
class GitHubRecordCounts:
    """What one tree read shows about Brain Home's size, with no file read.

    ``live_files`` and ``live_bytes`` are per kind folder under ``records/`` (Markdown files only,
    as a scan counts them); ``archived_files`` per kind under ``archive/records/``; ``receipts`` the
    files under ``records/.durability/``; ``other_files`` everything else; ``tree_entries`` every
    entry GitHub listed, folders included (GitHub stops listing a tree past its size limit)."""

    revision: GitHubRevision
    live_files: Mapping[str, int]
    live_bytes: Mapping[str, int]
    archived_files: Mapping[str, int]
    receipts: int
    other_files: int
    tree_entries: int


@dataclass(frozen=True, slots=True)
class GitHubRateLimit:
    """GitHub's hourly allowance for this storage's credential (the ``core`` resource).
    ``reset`` is when it refills, in seconds since the epoch."""

    limit: int
    remaining: int
    used: int
    reset: int


@dataclass(frozen=True, slots=True)
class GitHubArchiveResult:
    status: str
    repository: str
    branch: str
    commit_sha: str
    tree_sha: str
    moved: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class GitHubDecisionMatch:
    revision: GitHubRevision
    path: str
    content: bytes
    blob_sha: str
    record: Record


@dataclass(frozen=True, slots=True)
class GitHubDecisionPreflight:
    revision: GitHubRevision
    semantic_key: str
    match: GitHubDecisionMatch | None


@dataclass(frozen=True, slots=True)
class GitHubRecordMatch:
    revision: GitHubRevision
    path: str
    content: bytes
    blob_sha: str
    record: Record


@dataclass(frozen=True, slots=True)
class GitHubRecordPreflight:
    revision: GitHubRevision
    record_id: str
    match: GitHubRecordMatch | None


@dataclass(frozen=True, slots=True)
class GitHubMemoryOperationPreflight:
    revision: GitHubRevision
    operation_id: str
    input_sha256: str
    dedupe_key: str
    match: GitHubRecordMatch | None


@dataclass(frozen=True, slots=True)
class _Publication:
    record: Record
    record_bytes: bytes
    receipt: dict[str, Any]
    receipt_bytes: bytes
    record_path: str
    receipt_path: str
    record_sha256: str
    receipt_sha256: str
    origin_id: str
    semantic_key: str
    input_sha256: str


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value if isinstance(value, str) else None
    return None


def _rate_limited(response: HTTPResponse) -> dict[str, Any]:
    """``{"rate_limited": True, "reset": <epoch seconds or None>}`` when GitHub refused a request
    because the credential's allowance is used up, else nothing.

    GitHub answers 403 or 429 with ``x-ratelimit-remaining: 0`` when the hourly allowance is spent,
    and with ``retry-after`` for its shorter secondary limits. The error keeps its code
    (``github_api_error``), so existing handling is unchanged; hosts can read the detail."""

    if response.status not in {403, 429}:
        return {}
    remaining = _header(response.headers, "x-ratelimit-remaining")
    retry_after = _header(response.headers, "retry-after")
    if remaining != "0" and retry_after is None:
        return {}
    reset = _header(response.headers, "x-ratelimit-reset")
    return {"rate_limited": True, "reset": int(reset) if reset is not None and reset.isdigit() else None}


def _git_object(value: object, field: str) -> str:
    if not isinstance(value, str) or _GIT_OBJECT.fullmatch(value) is None:
        raise GitHubStorageError("github_response_invalid", f"GitHub {field} is invalid")
    return value


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GitHubStorageError("github_response_invalid", f"GitHub {field} must be an object")
    return dict(value)


def _safe_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")) or "\\" in value:
        raise GitHubStorageError("github_path_invalid", f"{field} is not a repository-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or "\0" in part or "\r" in part or "\n" in part for part in parts):
        raise GitHubStorageError("github_path_invalid", f"{field} is not a safe repository-relative path")
    return value


def _git_blob_sha(content: bytes, hexadecimal_length: int) -> str:
    framed = f"blob {len(content)}\0".encode("ascii") + content
    if hexadecimal_length == 40:
        return hashlib.sha1(framed, usedforsecurity=False).hexdigest()
    if hexadecimal_length == 64:
        return hashlib.sha256(framed).hexdigest()
    raise GitHubStorageError("github_response_invalid", "GitHub blob hash length is unsupported")


def _record_path(record: Record) -> str:
    moment = record.created_at.astimezone(UTC)
    return f"records/{record.kind}/{moment:%Y}/{moment:%m}/{record.id}.md"


_RECORD_ID_TIMESTAMP = re.compile(r"^brn_(\d{8})_(\d{6})_")


def _record_id_created_at(record_id: str) -> datetime:
    """The UTC moment encoded in a Brain Core record ID's own timestamp prefix."""

    match = _RECORD_ID_TIMESTAMP.match(record_id)
    if match is None:  # pragma: no cover - callers only pass already-validated IDs
        raise GitHubStorageError(
            "github_record_id_invalid", "Record ID must use the Brain Core brn_YYYYMMDD_HHMMSS_<hex> form"
        )
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def _older_than(path: str, cutoff: datetime) -> bool:
    """True only when ``path`` is a canonical record path whose ID predates ``cutoff``.

    ROA-11's near-duplicate window (docs/product/2026-09-26-gtbc-speed-goal.md
    Wave 3) filters candidate paths by this before any blob is read, so a warm
    copy costs nothing extra and a cold one reads only the last day's records.
    A path that doesn't match the canonical shape is never dropped here - it is
    kept so the existing per-record validation still catches it, instead of a
    performance filter silently hiding a corrupt or unexpected path.
    """

    match = _RECORD_PATH.fullmatch(path)
    if match is None:
        return False
    return _record_id_created_at(match.group("record_id")) < cutoff


def memory_operation_dedupe_key(operation_id: str, input_sha256: str) -> str:
    """Bind one stable memory operation to its exact pre-materialization input."""

    if not isinstance(operation_id, str) or _ORIGIN_ID.fullmatch(operation_id) is None:
        raise GitHubStorageError(
            "github_memory_operation_id_invalid",
            "Memory operation ID must be lowercase alphanumeric text with optional hyphens",
        )
    if not isinstance(input_sha256, str) or _SHA256.fullmatch(input_sha256) is None:
        raise GitHubStorageError(
            "github_memory_operation_input_invalid",
            "Memory operation input identity must be 64 lowercase hexadecimal characters",
        )
    return f"{_MEMORY_OPERATION_PREFIX}{operation_id}:{input_sha256}"


def memory_operation_record_suffix(operation_id: str, *, caller: str | None = None) -> str:
    """Derive the record-ID suffix every materialization of this operation must share.

    Unscoped (``caller=None``) reproduces the legacy suffix. Scoped (P3, finding 15):
    derived from ``"<caller>:<operation_id>"`` instead, so two different callers using
    the same ``operation_id`` no longer collide - each gets its own record.
    :func:`preflight_memory_operation` recognizes both forms, so a record saved before
    caller scoping still resolves for a replay by its own author.
    """

    if not isinstance(operation_id, str) or _ORIGIN_ID.fullmatch(operation_id) is None:
        raise GitHubStorageError(
            "github_memory_operation_id_invalid",
            "Memory operation ID must be lowercase alphanumeric text with optional hyphens",
        )
    if caller is None:
        key = operation_id
    else:
        if not isinstance(caller, str) or _ORIGIN_ID.fullmatch(caller.removeprefix("system:")) is None:
            raise GitHubStorageError(
                "github_memory_operation_id_invalid",
                "Memory operation caller must be lowercase alphanumeric text with optional hyphens and optional system: prefix",
            )
        key = f"{caller}:{operation_id}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class GitHubBlobStore(Protocol):
    """Durable copies of Brain Home file contents, keyed by Git blob ID.

    An implementation only keeps and returns bytes. :class:`GitHubObjectCache`
    re-hashes every copy it loads and ignores one that does not match its ID.
    """

    def load(self, blob_sha: str) -> bytes | None: ...

    def store(self, blob_sha: str, content: bytes) -> None: ...


class GitHubObjectCache:
    """Git objects already checked against GitHub, reused by their object ID.

    A Git commit, tree or blob never changes once it has an ID, so a kept copy
    cannot go stale. Freshness still comes from reading the branch reference
    from GitHub on every call; only what that reference points to is looked up
    here. Only objects that passed this module's checks are kept, a copy loaded
    from ``blob_store`` is re-hashed before use, and parsed records are shared
    between calls, so callers must not modify them. A record publication also
    keeps the commit, tree and file it created, once GitHub has moved the branch
    to that commit. Safe for concurrent use.
    """

    def __init__(
        self,
        *,
        blob_store: GitHubBlobStore | None = None,
        max_blob_bytes: int = 64 * 1024 * 1024,
        max_trees: int = 4,
        max_commits: int = 4096,
        max_records: int = 10_000,
    ) -> None:
        for name, value in (
            ("max_blob_bytes", max_blob_bytes),
            ("max_trees", max_trees),
            ("max_commits", max_commits),
            ("max_records", max_records),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._blob_store = blob_store
        self._max_blob_bytes = max_blob_bytes
        self._max_trees = max_trees
        self._max_commits = max_commits
        self._max_records = max_records
        self._lock = threading.Lock()
        self._commits: OrderedDict[str, str] = OrderedDict()
        self._trees: OrderedDict[str, tuple[dict[str, str], dict[str, object] | None, int | None]] = OrderedDict()
        self._blobs: OrderedDict[str, bytes] = OrderedDict()
        self._blob_bytes = 0
        self._records: OrderedDict[str, Record] = OrderedDict()

    @staticmethod
    def _touch(items: OrderedDict[str, Any], key: str) -> Any:
        value = items.get(key)
        if value is not None:
            items.move_to_end(key)
        return value

    @staticmethod
    def _keep(items: OrderedDict[str, Any], key: str, value: Any, limit: int) -> None:
        items[key] = value
        items.move_to_end(key)
        while len(items) > limit:
            items.popitem(last=False)

    def commit_tree(self, commit_sha: str) -> str | None:
        with self._lock:
            return self._touch(self._commits, commit_sha)

    def remember_commit(self, commit_sha: str, tree_sha: str) -> None:
        with self._lock:
            self._keep(self._commits, commit_sha, tree_sha, self._max_commits)

    def tree(self, tree_sha: str) -> dict[str, str] | None:
        with self._lock:
            entry = self._touch(self._trees, tree_sha)
            return None if entry is None else dict(entry[0])

    def remember_tree(self, tree_sha: str, blobs: Mapping[str, str]) -> None:
        with self._lock:
            current = self._trees.get(tree_sha)
            metadata = current[1:] if current is not None and current[0] == dict(blobs) else (None, None)
            self._keep(self._trees, tree_sha, (dict(blobs), *metadata), self._max_trees)

    def tree_listing(self, tree_sha: str):
        """Copies of a complete verified listing, including raw size metadata."""
        with self._lock:
            entry = self._touch(self._trees, tree_sha)
            if entry is None or entry[1] is None or entry[2] is None:
                return None
            return dict(entry[0]), dict(entry[1]), entry[2]

    def remember_tree_listing(self, tree_sha: str, blobs, sizes, entries):
        with self._lock:
            self._keep(self._trees, tree_sha, (dict(blobs), dict(sizes), entries), self._max_trees)

    def blob(self, blob_sha: str) -> bytes | None:
        with self._lock:
            content = self._touch(self._blobs, blob_sha)
        if content is not None or self._blob_store is None:
            return content
        stored = self._blob_store.load(blob_sha)
        if stored is None:
            return None
        if not isinstance(stored, bytes) or _git_blob_sha(stored, len(blob_sha)) != blob_sha:
            logger.warning("Ignoring a stored Brain Home copy that does not match blob %s", blob_sha)
            return None
        self._keep_blob(blob_sha, stored)
        return stored

    def remember_blob(self, blob_sha: str, content: bytes) -> None:
        self._keep_blob(blob_sha, content)
        if self._blob_store is not None:
            self._blob_store.store(blob_sha, content)

    def _keep_blob(self, blob_sha: str, content: bytes) -> None:
        if len(content) > self._max_blob_bytes:
            return
        with self._lock:
            previous = self._blobs.pop(blob_sha, None)
            if previous is not None:
                self._blob_bytes -= len(previous)
            self._blobs[blob_sha] = content
            self._blob_bytes += len(content)
            while self._blob_bytes > self._max_blob_bytes:
                _, evicted = self._blobs.popitem(last=False)
                self._blob_bytes -= len(evicted)

    def record(self, blob_sha: str) -> Record | None:
        with self._lock:
            return self._touch(self._records, blob_sha)

    def remember_record(self, blob_sha: str, record: Record) -> None:
        with self._lock:
            self._keep(self._records, blob_sha, record, self._max_records)


_request_observer = ContextVar("brain_github_request_observer", default=None)


@contextmanager
def observe_github_requests(observer):
    """Request-local timings: fixed operation category only, never URL or payload."""
    token = _request_observer.set(observer)
    try:
        yield
    finally:
        _request_observer.reset(token)


def _request_category(method, path):
    if "/git/ref" in path:
        return "read_ref" if method == "GET" else "publish_ref"
    if "/git/commits" in path:
        return "read_commit" if method == "GET" else "create_commit"
    if "/git/trees" in path:
        return "read_tree" if method == "GET" else "create_tree"
    if "/git/blobs" in path:
        return "read_blob" if method == "GET" else "create_blob"
    return "other_read" if method == "GET" else "other_write"


class GitHubBrainHomeStorage:
    """Read and publish one host-configured Brain Home GitHub branch."""

    def __init__(
        self,
        *,
        repository: str,
        branch: str,
        token: str,
        transport: HTTPTransport | None = None,
        api_base: str = "https://api.github.com",
        object_cache: GitHubObjectCache | None = None,
        archive_superseded: bool = False,
    ) -> None:
        """``object_cache`` lets reads reuse objects already checked; the branch is still read every call."""

        if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("repository must be an exact owner/name")
        if (
            not isinstance(branch, str)
            or not branch
            or branch.startswith(("/", "."))
            or branch.endswith(("/", ".", ".lock"))
            or any(value in branch for value in ("..", "//", "@{", "\\", "\0", "\r", "\n", " "))
        ):
            raise ValueError("branch is invalid")
        if not isinstance(token, str) or not token or token != token.strip() or any(c in token for c in "\r\n\0"):
            raise ValueError("token must be supplied explicitly by the trusted host")
        if not isinstance(api_base, str) or not api_base.startswith("https://") or api_base.endswith("/"):
            raise ValueError("api_base must be an HTTPS origin without a trailing slash")
        if object_cache is not None and not isinstance(object_cache, GitHubObjectCache):
            raise TypeError("object_cache must be a GitHubObjectCache")
        if not isinstance(archive_superseded, bool):
            raise TypeError("archive_superseded must be a boolean")
        self.archive_superseded = archive_superseded
        self.repository = repository
        self.branch = branch
        self._token = token
        self._transport = transport or UrllibHTTPTransport()
        self._api_base = api_base
        self._object_cache = object_cache
        owner, name = repository.split("/", 1)
        self._repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: Sequence[int],
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        body = None if payload is None else canonical_json(dict(payload)).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "brain-core-github-storage",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        started = time.monotonic()
        try:
            response = self._transport.request(method, self._api_base + path, headers=headers, body=body)
        finally:
            observer = _request_observer.get()
            if observer is not None:
                observer(_request_category(method, path), time.monotonic() - started)
        if response.status not in expected:
            code = "github_conflict" if response.status in {409, 422} else "github_api_error"
            error = GitHubConflict if response.status in {409, 422} else GitHubStorageError
            raise error(
                code,
                "GitHub rejected the requested operation",
                details={"method": method, "status": response.status, **_rate_limited(response)},
            )
        if not response.body:
            return None
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubStorageError("github_response_invalid", "GitHub returned invalid JSON") from exc

    def _revision_at(self, commit_sha: str) -> GitHubRevision:
        commit_sha = _git_object(commit_sha, "commit SHA")
        cache = self._object_cache
        tree_sha = None if cache is None else cache.commit_tree(commit_sha)
        if tree_sha is None:
            value = _mapping(
                self._request("GET", f"{self._repo_path}/git/commits/{commit_sha}", expected=(200,)),
                "commit",
            )
            if _git_object(value.get("sha"), "commit SHA") != commit_sha:
                raise GitHubStorageError("github_response_invalid", "GitHub commit identity changed")
            tree = _mapping(value.get("tree"), "commit tree")
            tree_sha = _git_object(tree.get("sha"), "tree SHA")
            if cache is not None:
                cache.remember_commit(commit_sha, tree_sha)
        return GitHubRevision(self.repository, self.branch, commit_sha, tree_sha)

    def revision(self) -> GitHubRevision:
        encoded = quote(self.branch, safe="")
        value = _mapping(
            self._request("GET", f"{self._repo_path}/git/ref/heads/{encoded}", expected=(200,)),
            "branch reference",
        )
        target = _mapping(value.get("object"), "branch target")
        return self._revision_at(_git_object(target.get("sha"), "branch commit SHA"))

    def _pinned(self, revision: GitHubRevision) -> GitHubRevision:
        """Check a revision this storage returned earlier, so an operation can stay on it.

        The branch is not read again. The commit's tree is confirmed, which costs
        nothing once the commit is in the object cache.
        """

        if not isinstance(revision, GitHubRevision):
            raise TypeError("revision must be a GitHubRevision")
        if revision.repository != self.repository or revision.branch != self.branch:
            raise ValueError("revision belongs to a different repository or branch")
        if self._revision_at(revision.commit_sha) != revision:
            raise ValueError("revision tree does not match its commit")
        return revision

    def _tree_blobs(self, revision: GitHubRevision) -> dict[str, str]:
        """Return the verified blob identities in one already-pinned tree."""

        cache = self._object_cache
        if cache is not None:
            cached = cache.tree(revision.tree_sha)
            if cached is not None:
                return cached
        blobs, _sizes, _entries = self._tree_listing(revision)
        return blobs

    def _tree_listing(self, revision: GitHubRevision) -> tuple[dict[str, str], dict[str, object], int]:
        """Read one pinned tree from GitHub: its blob identities, each blob's listed size (as
        GitHub gave it, unchecked) and how many entries it listed, folders included. The blob
        identities and sizes are kept together in the immutable object cache."""

        cache = self._object_cache
        if cache is not None:
            cached = cache.tree_listing(revision.tree_sha)
            if cached is not None:
                return cached
        tree_value = _mapping(
            self._request(
                "GET",
                f"{self._repo_path}/git/trees/{revision.tree_sha}?{urlencode({'recursive': '1'})}",
                expected=(200,),
            ),
            "tree",
        )
        tree_matches = _git_object(tree_value.get("sha"), "tree SHA") == revision.tree_sha
        truncated = tree_value.get("truncated") is not False
        if not tree_matches or truncated:
            # `truncated`: GitHub stopped listing a tree too big for one reply, which won't pass by itself.
            # Otherwise GitHub answered with a different tree than the one asked for.
            raise GitHubStorageError(
                "github_tree_incomplete",
                "GitHub did not return the complete pinned tree",
                details={"truncated": truncated, "tree_matches": tree_matches},
            )
        entries = tree_value.get("tree")
        if not isinstance(entries, list):
            raise GitHubStorageError("github_response_invalid", "GitHub tree entries are invalid")
        blobs: dict[str, str] = {}
        sizes: dict[str, object] = {}
        for raw in entries:
            entry = _mapping(raw, "tree entry")
            if entry.get("mode") in {"120000", "160000"}:
                raise GitHubStorageError(
                    "github_tree_unsafe",
                    "GitHub tree contains a symlink or gitlink",
                    details={"path": entry.get("path"), "mode": entry.get("mode")},
                )
            if entry.get("type") == "blob" and isinstance(entry.get("path"), str):
                path = entry["path"]
                if path in blobs:
                    raise GitHubStorageError(
                        "github_response_invalid",
                        "GitHub tree contains a duplicate path",
                        details={"path": path},
                    )
                blobs[path] = _git_object(entry.get("sha"), "blob SHA")
                sizes[path] = entry.get("size")
        if cache is not None:
            cache.remember_tree_listing(revision.tree_sha, blobs, sizes, len(entries))
        return blobs, sizes, len(entries)

    def _read_blobs(
        self,
        revision: GitHubRevision,
        paths: Sequence[str],
        blobs: Mapping[str, str],
        *,
        missing_ok: bool,
    ) -> GitHubRead:
        requested = tuple(dict.fromkeys(_safe_path(path, "path") for path in paths))
        cache = self._object_cache
        result: list[GitHubFile] = []
        for path in requested:
            blob_sha = blobs.get(path)
            if blob_sha is None:
                if missing_ok:
                    continue
                raise GitHubStorageError("github_path_missing", "Required GitHub path is absent", details={"path": path})
            content = None if cache is None else cache.blob(blob_sha)
            if content is None:
                content = self._fetch_blob(path, blob_sha)
                if cache is not None:
                    cache.remember_blob(blob_sha, content)
            result.append(GitHubFile(path, content, blob_sha))
        return GitHubRead(revision, tuple(result))

    def _fetch_blob(self, path: str, blob_sha: str) -> bytes:
        blob = _mapping(
            self._request("GET", f"{self._repo_path}/git/blobs/{blob_sha}", expected=(200,)),
            "blob",
        )
        if _git_object(blob.get("sha"), "blob SHA") != blob_sha or blob.get("encoding") != "base64":
            raise GitHubStorageError("github_response_invalid", "GitHub blob identity or encoding is invalid")
        content = blob.get("content")
        if not isinstance(content, str):
            raise GitHubStorageError("github_response_invalid", "GitHub blob content is invalid")
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
        except (ValueError, TypeError) as exc:
            raise GitHubStorageError("github_response_invalid", "GitHub blob base64 is invalid") from exc
        if blob.get("size") != len(decoded):
            raise GitHubStorageError("github_response_invalid", "GitHub blob size is inconsistent")
        if _git_blob_sha(decoded, len(blob_sha)) != blob_sha:
            raise GitHubStorageError(
                "github_blob_hash_mismatch",
                "GitHub blob content does not match its Git object identity",
                details={"path": path},
            )
        return decoded

    def _parse(self, item: GitHubFile, revision: GitHubRevision) -> Record:
        """Parse one checked file, reusing an earlier parse of identical content."""

        cache = self._object_cache
        if cache is not None:
            record = cache.record(item.blob_sha)
            if record is not None:
                return record
        record = parse_record(item.content.decode("utf-8"), f"github:{revision.commit_sha}:{item.path}")
        if cache is not None:
            cache.remember_record(item.blob_sha, record)
        return record

    def _read_revision(
        self,
        revision: GitHubRevision,
        paths: Sequence[str],
        *,
        missing_ok: bool,
    ) -> GitHubRead:
        return self._read_blobs(revision, paths, self._tree_blobs(revision), missing_ok=missing_ok)

    def read(self, paths: Sequence[str]) -> GitHubRead:
        """Read all requested files from one observed branch revision."""

        return self._read_revision(self.revision(), paths, missing_ok=False)

    def read_records(self, paths: Sequence[str], *, archived_ok: bool = False) -> GitHubRecordRead:
        """Read and validate existing-schema records from one branch revision.

        ``archived_ok``: a ``records/...`` path that is gone from the branch but sits at
        ``archive/`` plus that path (moved there byte for byte) is read from there. The file keeps
        the requested path and has ``archived=True``. An archived file that doesn't parse, or
        whose envelope doesn't match its path, is skipped and logged; a current file that doesn't
        still fails the read. A path in neither place is ``github_path_missing``, as without it.
        """

        if archived_ok:
            return self._read_records_or_archived(paths)
        result = self.read(paths)
        records: list[GitHubRecordFile] = []
        for item in result.files:
            try:
                record = self._parse(item, result.revision)
            except UnicodeDecodeError as exc:
                raise GitHubStorageError(
                    "github_record_invalid",
                    "GitHub record is not UTF-8",
                    details={"path": item.path},
                ) from exc
            if _record_path(record) != item.path:
                raise GitHubStorageError(
                    "github_record_invalid",
                    "GitHub record path differs from its validated envelope",
                    details={"path": item.path, "record_id": record.id},
                )
            records.append(GitHubRecordFile(item.path, item.content, item.blob_sha, record))
        return GitHubRecordRead(result.revision, tuple(records))

    def _read_records_or_archived(self, paths: Sequence[str]) -> GitHubRecordRead:
        revision = self.revision()
        blobs = self._tree_blobs(revision)
        requested = tuple(dict.fromkeys(_safe_path(path, "path") for path in paths))
        records: list[GitHubRecordFile] = []
        for path in requested:
            archived = path not in blobs and path.startswith("records/") and _ARCHIVE_PREFIX + path in blobs
            source = _ARCHIVE_PREFIX + path if archived else path
            # A missing path reads as the requested one, so the error names what was asked for.
            item = self._read_blobs(revision, (source if archived else path,), blobs, missing_ok=False).files[0]
            try:
                record = self._parse(item, revision)
            except UnicodeDecodeError as exc:
                if not archived:
                    raise GitHubStorageError(
                        "github_record_invalid", "GitHub record is not UTF-8", details={"path": path},
                    ) from exc
                logger.warning("Skipping an archived Brain Home file that is not a valid record: %s", source)
                continue
            except (BrainError, ValueError):
                if not archived:
                    raise
                logger.warning("Skipping an archived Brain Home file that is not a valid record: %s", source)
                continue
            invalid = _record_path(record) != path
            if invalid:
                if archived:
                    logger.warning("Skipping an archived Brain Home file whose record differs from its path: %s", source)
                    continue
                raise GitHubStorageError(
                    "github_record_invalid",
                    "GitHub record path differs from its validated envelope",
                    details={"path": path, "record_id": record.id},
                )
            records.append(GitHubRecordFile(path, item.content, item.blob_sha, record, archived=archived))
        return GitHubRecordRead(revision, tuple(records))

    def record_counts(self, revision: GitHubRevision | None = None) -> GitHubRecordCounts:
        """Brain Home's size from one tree read, with no file read: per kind, the live files and
        their bytes (GitHub's tree gives each file's size) and the archived files; the receipts; the
        other files; and the tree's entries. ``revision``, one this storage returned earlier, pins
        it; otherwise the branch is read. A complete immutable listing can be reused by tree SHA;
        an unpinned call still reads the current branch."""

        revision = self.revision() if revision is None else self._pinned(revision)
        blobs, sizes, entries = self._tree_listing(revision)
        live: dict[str, int] = {}
        live_bytes: dict[str, int] = {}
        archived: dict[str, int] = {}
        receipts = other = 0
        for path in blobs:
            parts = path.split("/")
            if parts[:2] == ["records", ".durability"]:
                receipts += 1
            elif parts[0] == "records" and len(parts) > 2 and path.endswith(".md"):
                size = sizes.get(path)
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise GitHubStorageError(
                        "github_response_invalid", "GitHub tree entry size is invalid", details={"path": path},
                    )
                live[parts[1]] = live.get(parts[1], 0) + 1
                live_bytes[parts[1]] = live_bytes.get(parts[1], 0) + size
            elif parts[:2] == ["archive", "records"] and len(parts) > 3:
                archived[parts[2]] = archived.get(parts[2], 0) + 1
            else:
                other += 1
        return GitHubRecordCounts(
            revision=revision,
            live_files=dict(sorted(live.items())),
            live_bytes=dict(sorted(live_bytes.items())),
            archived_files=dict(sorted(archived.items())),
            receipts=receipts,
            other_files=other,
            tree_entries=entries,
        )

    def rate_limit(self) -> GitHubRateLimit:
        """GitHub's hourly allowance for this storage's credential and what is left of it. GitHub
        doesn't count this request against the allowance."""

        core = _mapping(
            _mapping(self._request("GET", "/rate_limit", expected=(200,)), "rate limit").get("resources"),
            "rate limit resources",
        ).get("core")
        core = _mapping(core, "core rate limit")
        values = {}
        for key in ("limit", "remaining", "used", "reset"):
            value = core.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise GitHubStorageError("github_response_invalid", f"GitHub rate limit {key} is invalid")
            values[key] = value
        return GitHubRateLimit(**values)

    def snapshot_records(
        self,
        *,
        kinds: frozenset[str],
        max_records: int = SNAPSHOT_MAX_RECORDS,
        max_bytes: int = SNAPSHOT_MAX_BYTES,
        revision: GitHubRevision | None = None,
        min_created_at: datetime | None = None,
    ) -> GitHubRecordRead:
        """Bounded, validated records at one immutable revision; never silently partial.

        ``revision``, one this storage returned earlier, pins the scan to it instead
        of reading the branch again. ``min_created_at``, when given, drops a
        candidate path whose record-ID timestamp predates it before any blob is
        read (ROA-11's near-duplicate window) - a warm copy then costs nothing
        extra and a cold one reads only the paths still in range.

        Past a cap the scan refuses with ``github_scan_limit``; its details name the kinds, the
        files found and the cap (``limit`` says which: ``files``, or ``bytes`` with the bytes
        counted when it stopped, a lower bound). A scan at 80% of a cap or more logs a warning,
        kinds and counts only. Without an object cache, every file is one GitHub request, so a
        cap above the defaults (512 files, 8 MiB) is refused with ``ValueError``.
        """
        if not kinds or not kinds <= SCAN_KINDS:
            raise ValueError("Unsupported record kinds")
        if min_created_at is not None and (not isinstance(min_created_at, datetime) or min_created_at.tzinfo is None):
            raise ValueError("min_created_at must be a timezone-aware datetime")
        if self._object_cache is None and (max_records > SNAPSHOT_MAX_RECORDS or max_bytes > SNAPSHOT_MAX_BYTES):
            raise ValueError(
                f"a scan cap above {SNAPSHOT_MAX_RECORDS} files or {SNAPSHOT_MAX_BYTES} bytes needs an object cache"
            )
        revision = self.revision() if revision is None else self._pinned(revision)
        blobs = self._tree_blobs(revision)
        paths = sorted(p for p in blobs if any(p.startswith(f"records/{kind}/") for kind in kinds) and p.endswith(".md"))
        if min_created_at is not None:
            paths = [p for p in paths if not _older_than(p, min_created_at)]
        scanned = sorted(kinds)
        if len(paths) > max_records:
            raise GitHubStorageError(
                "github_scan_limit",
                "Brain Home record scan exceeds its explicit limit",
                details={"kinds": scanned, "limit": "files", "files": len(paths), "max_records": max_records},
            )
        if paths and len(paths) >= max_records * SCAN_WARNING_SHARE:
            logger.warning(
                "Brain Home scan of %s: %d files, at or past %d%% of its %d-file limit",
                ",".join(scanned), len(paths), round(SCAN_WARNING_SHARE * 100), max_records,
            )
        files = []
        total_bytes = 0
        for path in paths:
            item = self._read_blobs(revision, (path,), blobs, missing_ok=False).files[0]
            total_bytes += len(item.content)
            if total_bytes > max_bytes:
                raise GitHubStorageError(
                    "github_scan_limit",
                    "Brain Home record scan exceeds its byte limit",
                    details={
                        "kinds": scanned, "limit": "bytes", "files": len(paths), "bytes": total_bytes,
                        "max_bytes": max_bytes,
                    },
                )
            files.append(item)
        if paths and total_bytes >= max_bytes * SCAN_WARNING_SHARE:
            logger.warning(
                "Brain Home scan of %s: %d bytes, at or past %d%% of its %d-byte limit",
                ",".join(scanned), total_bytes, round(SCAN_WARNING_SHARE * 100), max_bytes,
            )
        records = []
        for item in files:
            record = self._parse(item, revision)
            if _record_path(record) != item.path or record.kind not in kinds:
                raise GitHubStorageError("github_record_invalid", "Record path or kind differs from its envelope")
            records.append(GitHubRecordFile(item.path, item.content, item.blob_sha, record))
        return GitHubRecordRead(revision, tuple(records))

    def preflight_decision_by_semantic_key(
        self,
        semantic_key: str,
        *,
        max_candidates: int = 256,
    ) -> GitHubDecisionPreflight:
        """Pin a revision and find one validated decision by semantic key.

        This bounded seam reads only canonical decision-record paths. It does not
        expose the repository tree or provide a generic traversal interface. The
        pinned revision is retained when no matching decision exists.
        """

        if not isinstance(semantic_key, str) or _SHA256.fullmatch(semantic_key) is None:
            raise GitHubStorageError(
                "github_semantic_key_invalid",
                "Decision semantic key must be 64 lowercase hexadecimal characters",
            )
        if (
            not isinstance(max_candidates, int)
            or isinstance(max_candidates, bool)
            or max_candidates <= 0
            or max_candidates > MAX_DECISION_CANDIDATES
        ):
            raise ValueError(f"max_candidates must be from 1 through {MAX_DECISION_CANDIDATES}")

        revision = self.revision()
        blobs = self._tree_blobs(revision)
        candidates = tuple(sorted(path for path in blobs if _DECISION_RECORD_PATH.fullmatch(path)))
        if len(candidates) > max_candidates:
            raise GitHubStorageError(
                "github_decision_candidate_limit",
                "Decision reconciliation exceeded its configured candidate bound",
                details={"candidates": len(candidates), "max_candidates": max_candidates},
            )

        observed = self._read_blobs(revision, candidates, blobs, missing_ok=False)
        matches: list[GitHubDecisionMatch] = []
        for item in observed.files:
            try:
                record = self._parse(item, revision)
            except (UnicodeDecodeError, BrainError) as exc:
                raise GitHubStorageError(
                    "github_decision_corrupt",
                    "GitHub decision candidate is not a valid Brain Core record",
                    details={"path": item.path},
                ) from exc
            if record.kind != "decision" or _record_path(record) != item.path:
                raise GitHubStorageError(
                    "github_decision_corrupt",
                    "GitHub decision candidate differs from its canonical record path",
                    details={"path": item.path, "record_id": record.id, "kind": record.kind},
                )
            if record.dedupe_key == semantic_key:
                matches.append(GitHubDecisionMatch(revision, item.path, item.content, item.blob_sha, record))

        if len(matches) > 1:
            raise GitHubConflict(
                "github_decision_duplicate",
                "Multiple validated decisions have the requested semantic key",
                details={"semantic_key": semantic_key, "paths": [match.path for match in matches]},
            )
        return GitHubDecisionPreflight(revision, semantic_key, matches[0] if matches else None)

    def find_decision_by_semantic_key(
        self,
        semantic_key: str,
        *,
        max_candidates: int = 256,
    ) -> GitHubDecisionMatch | None:
        """Find one validated decision while preserving the legacy optional result."""

        return self.preflight_decision_by_semantic_key(
            semantic_key,
            max_candidates=max_candidates,
        ).match

    def preflight_record_by_id(
        self,
        record_id: str,
        *,
        max_candidates: int = 256,
        revision: GitHubRevision | None = None,
        include_archive: bool = False,
    ) -> GitHubRecordPreflight:
        """Pin a revision and find one validated Brain record by ID.

        Only canonical paths for existing Brain Core record kinds whose filename
        is the exact requested ID are fetched. Repository traversal is not
        exposed to callers. The pinned revision is retained when no record exists.
        ``revision``, one this storage returned earlier, is used instead of
        reading the branch again.

        ``include_archive`` also counts ``archive/`` plus a canonical path as a
        candidate, as ``preflight_memory_operation`` does; the match's ``path`` is
        then the ``archive/...`` path. An archived candidate that doesn't parse, or
        whose envelope differs from its path, is skipped and logged, as
        ``read_records(archived_ok=True)`` skips it; a live one still fails. A valid
        record in both places is a conflict (``github_record_duplicate``).
        """

        if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
            raise GitHubStorageError(
                "github_record_id_invalid",
                "Record ID must use the Brain Core brn_YYYYMMDD_HHMMSS_<hex> form",
            )
        if (
            not isinstance(max_candidates, int)
            or isinstance(max_candidates, bool)
            or max_candidates <= 0
            or max_candidates > MAX_RECORD_CANDIDATES
        ):
            raise ValueError(f"max_candidates must be from 1 through {MAX_RECORD_CANDIDATES}")

        revision = self.revision() if revision is None else self._pinned(revision)
        blobs = self._tree_blobs(revision)
        candidates: list[tuple[str, re.Match[str]]] = []
        for path in sorted(blobs):
            canonical = path.removeprefix(_ARCHIVE_PREFIX) if include_archive else path
            match = _RECORD_PATH.fullmatch(canonical)
            if match is not None and match.group("record_id") == record_id:
                candidates.append((path, match))
        if len(candidates) > max_candidates:
            raise GitHubStorageError(
                "github_record_candidate_limit",
                "Record lookup exceeded its configured candidate bound",
                details={"candidates": len(candidates), "max_candidates": max_candidates},
            )

        paths = tuple(path for path, _ in candidates)
        kinds = {path: match.group("kind") for path, match in candidates}
        observed = self._read_blobs(revision, paths, blobs, missing_ok=False)
        matches: list[GitHubRecordMatch] = []
        for item in observed.files:
            archived = item.path.startswith(_ARCHIVE_PREFIX)
            try:
                record = self._parse(item, revision)
            except (UnicodeDecodeError, BrainError, ValueError) as exc:
                if archived:
                    logger.warning("Skipping an archived Brain Home file that is not a valid record: %s", item.path)
                    continue
                if isinstance(exc, ValueError) and not isinstance(exc, UnicodeDecodeError):
                    raise  # as before this change: only archived candidates are tolerated
                raise GitHubStorageError(
                    "github_record_corrupt",
                    "GitHub record candidate is not a valid Brain Core record",
                    details={"path": item.path},
                ) from exc
            if (
                record.id != record_id
                or record.kind != kinds[item.path]
                or _record_path(record) != item.path.removeprefix(_ARCHIVE_PREFIX)
            ):
                if archived:
                    logger.warning(
                        "Skipping an archived Brain Home file whose record differs from its path: %s", item.path,
                    )
                    continue
                raise GitHubStorageError(
                    "github_record_corrupt",
                    "GitHub record candidate differs from its canonical record path",
                    details={"path": item.path, "record_id": record.id, "kind": record.kind},
                )
            matches.append(GitHubRecordMatch(revision, item.path, item.content, item.blob_sha, record))

        if len(matches) > 1:
            raise GitHubConflict(
                "github_record_duplicate",
                "Multiple validated Brain records have the requested ID",
                details={"record_id": record_id, "paths": [match.path for match in matches]},
            )
        return GitHubRecordPreflight(revision, record_id, matches[0] if matches else None)

    def find_record_by_id(
        self,
        record_id: str,
        *,
        max_candidates: int = 256,
        include_archive: bool = False,
    ) -> GitHubRecordMatch | None:
        """Find one validated record while preserving the legacy optional result."""

        return self.preflight_record_by_id(
            record_id, max_candidates=max_candidates, include_archive=include_archive,
        ).match

    def preflight_work_records(
        self,
        *,
        record_suffixes: Sequence[str],
        caller: str,
        revision: GitHubRevision,
        max_candidates: int = MAX_BATCH_RECORDS,
    ) -> tuple[GitHubRecordMatch, ...]:
        """Read exact planned work-ID suffixes in one supplied pinned tree.

        Search canonical item filenames, including their exact archive paths,
        and validate every matching record and authenticated caller. Refuse
        malformed, duplicate or over-bound candidates; never search titles or
        operation keys. The host verifies owner/vault, public hash and exact
        accepted batch before treating these records as an idempotent retry.
        This lookup grants no ordinary archive-read or undo admission.
        """
        suffixes = tuple(record_suffixes)
        if (not suffixes or len(suffixes) > 20 * 26 or len(set(suffixes)) != len(suffixes)
            or any(not isinstance(suffix, str) or re.fullmatch(r"[0-9a-f]{16}", suffix) is None for suffix in suffixes)
            or not isinstance(caller, str) or not caller.strip()):
            raise GitHubStorageError("github_work_lookup_invalid", "Work lookup requires distinct exact planned suffixes and a caller")
        if (not isinstance(max_candidates, int) or isinstance(max_candidates, bool)
            or not 1 <= max_candidates <= MAX_BATCH_RECORDS):
            raise ValueError(f"max_candidates must be from 1 through {MAX_BATCH_RECORDS}")
        revision = self._pinned(revision)
        blobs = self._tree_blobs(revision)
        wanted = set(suffixes)
        candidates = []
        for path in sorted(blobs):
            match = _RECORD_PATH.fullmatch(path.removeprefix(_ARCHIVE_PREFIX))
            if match is not None and match.group("kind") == "item" and match.group("record_id").rsplit("_", 1)[-1] in wanted:
                candidates.append((path, match.group("record_id")))
        if len(candidates) > max_candidates:
            raise GitHubStorageError("github_work_candidate_limit", "Work retry lookup exceeds the atomic batch bound",
                details={"candidates": len(candidates), "max_candidates": max_candidates})
        # Read only bounded exact candidates. Unlike ordinary archive reads,
        # malformed candidates are a refusal, never a silently skipped retry.
        found = []
        seen_ids, seen_suffixes = set(), set()
        total_bytes = 0
        for path, record_id in candidates:
            suffix = record_id.rsplit("_", 1)[-1]
            if record_id in seen_ids or suffix in seen_suffixes:
                raise GitHubConflict("github_work_identity_conflict", "A planned work suffix names multiple records",
                    details={"record_id": record_id})
            seen_ids.add(record_id)
            seen_suffixes.add(suffix)
            item = self._read_blobs(revision, (path,), blobs, missing_ok=False).files[0]
            total_bytes += len(item.content)
            if total_bytes > SNAPSHOT_MAX_BYTES:
                raise GitHubStorageError("github_work_byte_limit", "Work retry lookup exceeds its byte bound")
            try:
                record = self._parse(item, revision)
            except (UnicodeDecodeError, BrainError, ValueError) as exc:
                raise GitHubStorageError("github_record_corrupt", "Work retry candidate is not a valid record",
                    details={"path": path}) from exc
            if record.id != record_id or record.kind != "item" or _record_path(record) != path.removeprefix(_ARCHIVE_PREFIX):
                raise GitHubStorageError("github_record_corrupt", "Work retry candidate differs from its exact path",
                    details={"path": path})
            if record.source is None or record.source.get("recorded_by") != caller:
                raise GitHubConflict("github_work_caller_conflict", "A planned work record belongs to a different caller",
                    details={"record_id": record_id})
            found.append(GitHubRecordMatch(revision, path, item.content, item.blob_sha, record))
        return tuple(found)

    def preflight_memory_operation(
        self,
        *,
        operation_id: str,
        input_sha256: str,
        caller: str | None = None,
        max_candidates: int = 256,
        revision: GitHubRevision | None = None,
    ) -> GitHubMemoryOperationPreflight:
        """Reconcile a stable memory operation before time-based materialization.

        The returned dedupe key uses the existing Record ``dedupe_key`` field to
        bind the stable operation ID to a hash of its exact pre-materialization
        input. Only capture, commitment, preference, item, and fact record paths
        whose ID ends in this operation's record suffix are read, so save time
        no longer grows with the total number of stored memory records. Records
        the clear-out moved to ``archive/records/...`` are candidates too, found
        in the same tree, so a replay of an archived record is not re-created.

        ``caller`` scopes the search to one authenticated subject (P3, finding 15):
        a candidate whose ID ends in the caller-scoped suffix always counts; a
        candidate whose ID ends only in the legacy, unscoped suffix (from before
        caller scoping) counts only when its own ``source.recorded_by`` equals
        ``caller`` - so a pre-release record is still found by a replay from its own
        author, and is silently not a candidate for anyone else's identically-named
        operation_id (not a conflict). ``caller=None`` reproduces the exact legacy,
        unscoped search. Archived candidates follow the same suffix and author rules.

        ``revision`` is a trusted revision this storage returned earlier. It pins
        reconciliation to that immutable tree without reading the branch again.
        A publisher must still guard its branch update, and a caller retrying a
        revision conflict must discard the old revision and reconcile afresh.
        """

        dedupe_key = memory_operation_dedupe_key(operation_id, input_sha256)
        legacy_suffix = memory_operation_record_suffix(operation_id)
        scoped_suffix = None if caller is None else memory_operation_record_suffix(operation_id, caller=caller)
        if (
            not isinstance(max_candidates, int)
            or isinstance(max_candidates, bool)
            or max_candidates <= 0
            or max_candidates > MAX_MEMORY_OPERATION_CANDIDATES
        ):
            raise ValueError(
                f"max_candidates must be from 1 through {MAX_MEMORY_OPERATION_CANDIDATES}"
            )

        revision = self.revision() if revision is None else self._pinned(revision)
        blobs = self._tree_blobs(revision)
        # Each candidate remembers whether it matched the caller-scoped suffix (always
        # a candidate) or only the legacy one (a candidate only for its own author -
        # gated below once its parsed record's source is known).
        candidates: list[tuple[str, str, bool]] = []
        for path in sorted(blobs):
            match = _RECORD_PATH.fullmatch(path.removeprefix(_ARCHIVE_PREFIX))
            if match is None or match.group("kind") not in {"capture", "commitment", "preference", "item", "fact"}:
                continue
            record_id = match.group("record_id")
            if scoped_suffix is not None and record_id.endswith(f"_{scoped_suffix}"):
                candidates.append((path, match.group("kind"), True))
            elif record_id.endswith(f"_{legacy_suffix}"):
                candidates.append((path, match.group("kind"), False))
        if len(candidates) > max_candidates:
            raise GitHubStorageError(
                "github_memory_operation_candidate_limit",
                "Memory operation reconciliation exceeded its configured candidate bound",
                details={"candidates": len(candidates), "max_candidates": max_candidates},
            )

        paths = tuple(path for path, _, _ in candidates)
        kinds = {path: kind for path, kind, _ in candidates}
        scoped_match = {path: is_scoped for path, _, is_scoped in candidates}
        observed = self._read_blobs(revision, paths, blobs, missing_ok=False)
        matches: list[GitHubRecordMatch] = []
        for item in observed.files:
            try:
                record = self._parse(item, revision)
            except (UnicodeDecodeError, BrainError) as exc:
                raise GitHubStorageError(
                    "github_memory_operation_corrupt",
                    "GitHub memory operation candidate is not a valid Brain Core record",
                    details={"path": item.path},
                ) from exc
            if record.kind != kinds[item.path] or _record_path(record) != item.path.removeprefix(_ARCHIVE_PREFIX):
                raise GitHubStorageError(
                    "github_memory_operation_corrupt",
                    "GitHub memory operation candidate differs from its canonical record path",
                    details={"path": item.path, "record_id": record.id, "kind": record.kind},
                )
            binding = record.dedupe_key
            if not isinstance(binding, str) or not binding.startswith(_MEMORY_OPERATION_PREFIX):
                continue
            identity = _MEMORY_OPERATION_KEY.fullmatch(binding)
            if identity is None:
                raise GitHubStorageError(
                    "github_memory_operation_corrupt",
                    "GitHub memory operation binding is malformed",
                    details={"path": item.path},
                )
            if identity.group("operation_id") != operation_id:
                continue
            if not scoped_match[item.path] and caller is not None and (record.source or {}).get("recorded_by") != caller:
                continue
            if identity.group("input_sha256") != input_sha256:
                raise GitHubConflict(
                    "github_memory_operation_identity_conflict",
                    "Stable memory operation ID is already bound to different input",
                    details={"operation_id": operation_id},
                )
            matches.append(GitHubRecordMatch(revision, item.path, item.content, item.blob_sha, record))

        if len(matches) > 1:
            raise GitHubConflict(
                "github_memory_operation_duplicate",
                "Multiple validated records have the requested memory operation binding",
                details={"operation_id": operation_id, "paths": [match.path for match in matches]},
            )
        return GitHubMemoryOperationPreflight(
            revision,
            operation_id,
            input_sha256,
            dedupe_key,
            matches[0] if matches else None,
        )

    @staticmethod
    def _validate_publication(record_bytes: bytes, receipt_bytes: bytes) -> _Publication:
        if not isinstance(record_bytes, bytes) or not isinstance(receipt_bytes, bytes):
            raise TypeError("record_bytes and receipt_bytes must be bytes")
        try:
            record_text = record_bytes.decode("utf-8")
            receipt_text = receipt_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubStorageError("github_publication_invalid", "Publication inputs must be UTF-8") from exc
        record = parse_record(record_text, "github publication record")
        try:
            raw_receipt = json.loads(receipt_text)
        except json.JSONDecodeError as exc:
            raise GitHubStorageError("github_publication_invalid", "Durability receipt is invalid JSON") from exc
        receipt = _mapping(raw_receipt, "durability receipt")
        if frozenset(receipt) != _RECEIPT_FIELDS:
            raise GitHubStorageError("github_publication_invalid", "Durability receipt fields differ from the writer contract")
        if receipt_bytes != (canonical_json(receipt) + "\n").encode("utf-8"):
            raise GitHubStorageError("github_publication_invalid", "Durability receipt is not the writer's exact canonical encoding")
        if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("writer_id") != WRITER_ID:
            raise GitHubStorageError("github_publication_invalid", "Durability receipt writer identity is invalid")
        if receipt.get("status") not in {"created", "duplicate/no-op"} or receipt.get("refusal_code") is not None:
            raise GitHubStorageError("github_publication_invalid", "Only successful durability receipts may be published")
        origin_id = receipt.get("origin_id")
        semantic_key = receipt.get("semantic_key")
        if not isinstance(origin_id, str) or _ORIGIN_ID.fullmatch(origin_id) is None:
            raise GitHubStorageError("github_publication_invalid", "Durability receipt origin is invalid")
        if not isinstance(semantic_key, str) or _SHA256.fullmatch(semantic_key) is None:
            raise GitHubStorageError("github_publication_invalid", "Durability semantic key is invalid")
        record_sha256 = hashlib.sha256(record_bytes).hexdigest()
        if (
            record.kind != "decision"
            or record.dedupe_key != semantic_key
            or receipt.get("canonical_identity") != record.id
            or receipt.get("canonical_sha256") != record_sha256
            or receipt.get("owner_locator") != "vault://brain-home"
            or receipt.get("writer_locator") != "vault://brain-home"
            or receipt.get("owner_id") != "brain-home"
            or receipt.get("model_calls") != 0
            or receipt.get("network_calls") != 0
        ):
            raise GitHubStorageError("github_publication_invalid", "Record and durability receipt do not describe one accepted decision")
        expected_record_path = _record_path(record)
        locator = receipt.get("canonical_locator")
        prefix = "vault://brain-home/"
        if not isinstance(locator, str) or not locator.startswith(prefix):
            raise GitHubStorageError("github_publication_invalid", "Durability canonical locator is invalid")
        record_path = _safe_path(locator[len(prefix):], "record path")
        if record_path != expected_record_path:
            raise GitHubStorageError("github_publication_invalid", "Record path differs from the validated record envelope")
        receipt_path = _safe_path(f"records/.durability/receipts/{origin_id}.json", "receipt path")
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        identity = {
            "origin_id": origin_id,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
            "record_path": record_path,
            "record_sha256": record_sha256,
            "semantic_key": semantic_key,
        }
        input_sha256 = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
        return _Publication(
            record,
            record_bytes,
            receipt,
            receipt_bytes,
            record_path,
            receipt_path,
            record_sha256,
            receipt_sha256,
            origin_id,
            semantic_key,
            input_sha256,
        )

    @staticmethod
    def _message(publication: _Publication) -> str:
        return (
            f"Publish Brain Core decision {publication.origin_id}\n\n"
            f"Brain-Core-Origin: {publication.origin_id}\n"
            f"Brain-Core-Input-SHA256: {publication.input_sha256}\n"
            f"Brain-Core-Semantic-Key: {publication.semantic_key}\n"
        )

    @staticmethod
    def _message_identity(message: object, origin_id: str) -> str | None:
        if not isinstance(message, str):
            return None
        origin = re.search(r"(?m)^Brain-Core-Origin: ([a-z0-9][a-z0-9-]{0,127})$", message)
        digest = re.search(r"(?m)^Brain-Core-Input-SHA256: ([0-9a-f]{64})$", message)
        if origin is None or origin.group(1) != origin_id:
            return None
        return digest.group(1) if digest is not None else ""

    def _result(
        self,
        status: str,
        publication: _Publication,
        revision: GitHubRevision,
    ) -> GitHubPublicationResult:
        return GitHubPublicationResult(
            status,
            self.repository,
            self.branch,
            revision.commit_sha,
            revision.tree_sha,
            publication.origin_id,
            publication.semantic_key,
            publication.input_sha256,
            publication.record_path,
            publication.receipt_path,
            publication.record_sha256,
            publication.receipt_sha256,
            True,
        )

    def _observe_exact(
        self,
        publication: _Publication,
        commit_sha: str,
        *,
        status: str,
    ) -> GitHubPublicationResult:
        revision = self._revision_at(commit_sha)
        observed = self._read_revision(
            revision,
            (publication.record_path, publication.receipt_path),
            missing_ok=False,
        )
        if (
            observed.file(publication.record_path).content != publication.record_bytes
            or observed.file(publication.receipt_path).content != publication.receipt_bytes
        ):
            raise GitHubConflict(
                "github_publication_identity_conflict",
                "Accepted commit does not contain the exact record and receipt",
            )
        return self._result(status, publication, revision)

    def _lookup(self, publication: _Publication, *, status: str) -> GitHubPublicationResult | None:
        query = urlencode({"sha": self.branch, "path": publication.receipt_path, "per_page": "100"})
        value = self._request("GET", f"{self._repo_path}/commits?{query}", expected=(200,))
        if not isinstance(value, list):
            raise GitHubStorageError("github_response_invalid", "GitHub commit list is invalid")
        for raw in value:
            row = _mapping(raw, "commit list entry")
            commit = _mapping(row.get("commit"), "commit list metadata")
            input_sha256 = self._message_identity(commit.get("message"), publication.origin_id)
            if input_sha256 is None:
                continue
            if input_sha256 != publication.input_sha256:
                raise GitHubConflict(
                    "github_publication_identity_conflict",
                    "Stable publication origin is already bound to different input",
                )
            return self._observe_exact(publication, _git_object(row.get("sha"), "commit SHA"), status=status)
        return None

    def lookup_accepted(
        self,
        *,
        record_bytes: bytes,
        receipt_bytes: bytes,
    ) -> GitHubPublicationResult | None:
        """Find and verify a remotely accepted exact publication."""

        return self._lookup(self._validate_publication(record_bytes, receipt_bytes), status="reconciled")

    @staticmethod
    def _validate_standalone_record(record_bytes: bytes) -> tuple[Record, str, str]:
        if not isinstance(record_bytes, bytes):
            raise TypeError("record_bytes must be bytes")
        try:
            text = record_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubStorageError("github_record_invalid", "Record publication must be UTF-8") from exc
        record = parse_record(text, "github standalone record publication")
        if record.kind not in {"capture", "commitment", "preference", "continuity", "item", "fact", *SITREP_KINDS}:
            raise GitHubStorageError(
                "github_record_kind_requires_owner_route",
                "Standalone GitHub publication supports capture, commitment, preference, continuity, item, fact, sitrep, and focus_copy records only; decisions require their exact durability receipt",
                details={"kind": record.kind},
            )
        return record, _record_path(record), hashlib.sha256(record_bytes).hexdigest()

    @staticmethod
    def _record_message(record: Record, record_sha256: str) -> str:
        return (
            f"Publish Brain Core {record.kind} {record.id}\n\n"
            f"Brain-Core-Record-ID: {record.id}\n"
            f"Brain-Core-Record-SHA256: {record_sha256}\n"
        )

    @staticmethod
    def _record_message_identity(message: object, record_id: str) -> str | None:
        if not isinstance(message, str):
            return None
        identity = re.search(r"(?m)^Brain-Core-Record-ID: (brn_[0-9]{8}_[0-9]{6}_[0-9a-f]{4,16})$", message)
        digest = re.search(r"(?m)^Brain-Core-Record-SHA256: ([0-9a-f]{64})$", message)
        if identity is None or identity.group(1) != record_id:
            return None
        return digest.group(1) if digest is not None else ""

    def _record_result(
        self,
        status: str,
        record: Record,
        path: str,
        record_sha256: str,
        revision: GitHubRevision,
    ) -> GitHubRecordPublicationResult:
        return GitHubRecordPublicationResult(
            status,
            self.repository,
            self.branch,
            revision.commit_sha,
            revision.tree_sha,
            record.id,
            record.kind,
            path,
            record_sha256,
            True,
        )

    def _observe_record(
        self,
        record: Record,
        record_bytes: bytes,
        path: str,
        record_sha256: str,
        commit_sha: str,
        *,
        status: str,
        archive_sources: Mapping[str, str] | None = None,
    ) -> GitHubRecordPublicationResult:
        revision = self._revision_at(commit_sha)
        observed = self._read_revision(revision, (path,), missing_ok=False).file(path)
        if observed.content != record_bytes:
            raise GitHubConflict(
                "github_record_identity_conflict",
                "Accepted commit does not contain the exact validated record",
                details={"record_id": record.id},
            )
        if archive_sources and not self._archived(tuple(archive_sources), self._tree_blobs(revision), archive_sources):
            raise GitHubConflict("github_archive_conflict", "Accepted record commit does not contain its exact archive moves")
        return self._record_result(status, record, path, record_sha256, revision)

    def _lookup_record(
        self,
        record: Record,
        record_bytes: bytes,
        path: str,
        record_sha256: str,
        *,
        status: str,
        archive_sources: Mapping[str, str] | None = None,
    ) -> GitHubRecordPublicationResult | None:
        query = urlencode({"sha": self.branch, "path": path, "per_page": "100"})
        value = self._request("GET", f"{self._repo_path}/commits?{query}", expected=(200,))
        if not isinstance(value, list):
            raise GitHubStorageError("github_response_invalid", "GitHub commit list is invalid")
        for raw in value:
            row = _mapping(raw, "commit list entry")
            commit = _mapping(row.get("commit"), "commit list metadata")
            observed_sha256 = self._record_message_identity(commit.get("message"), record.id)
            if observed_sha256 is None:
                continue
            if observed_sha256 != record_sha256:
                raise GitHubConflict(
                    "github_record_identity_conflict",
                    "Stable Brain Core record ID is already bound to different content",
                    details={"record_id": record.id},
                )
            return self._observe_record(
                record,
                record_bytes,
                path,
                record_sha256,
                _git_object(row.get("sha"), "commit SHA"),
                status=status,
                archive_sources=archive_sources,
            )
        return None

    def _create_blob(self, content: bytes) -> str:
        value = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/blobs",
                expected=(201,),
                payload={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            ),
            "created blob",
        )
        return _git_object(value.get("sha"), "created blob SHA")

    def _patch_ref(self, commit_sha: str) -> Any:
        encoded = quote(self.branch, safe="")
        return self._request(
            "PATCH",
            f"{self._repo_path}/git/refs/heads/{encoded}",
            expected=(200,),
            payload={"sha": commit_sha, "force": False},
        )

    @staticmethod
    def _updated_head(reply: object) -> str | None:
        """The commit an accepted branch update's reply names, or None when it names none."""

        target = reply.get("object") if isinstance(reply, Mapping) else None
        sha = target.get("sha") if isinstance(target, Mapping) else None
        return sha if isinstance(sha, str) and _GIT_OBJECT.fullmatch(sha) is not None else None

    def _confirm_record_head(
        self,
        record: Record,
        record_bytes: bytes,
        path: str,
        record_sha256: str,
        candidate_sha: str,
        archive_sources: Mapping[str, str] | None = None,
    ) -> GitHubRecordPublicationResult:
        """Read the branch and the record back when no reply named the new head."""

        head = self.revision()
        if head.commit_sha != candidate_sha:
            raise GitHubConflict(
                "github_revision_conflict",
                "GitHub did not expose the accepted record commit at the configured branch head",
                details={"candidate": candidate_sha, "actual": head.commit_sha},
            )
        return self._observe_record(record, record_bytes, path, record_sha256, candidate_sha,
                                    status="created", archive_sources=archive_sources)

    def _remember_publication(
        self,
        base_blobs: Mapping[str, str],
        path: str,
        blob_sha: str,
        content: bytes,
        tree_sha: str,
        commit_sha: str,
    ) -> None:
        """Keep what an accepted publication created, so the next read needs only the branch."""

        cache = self._object_cache
        if cache is None:
            return
        cache.remember_blob(blob_sha, content)
        cache.remember_commit(commit_sha, tree_sha)
        # The new tree is the base tree plus this one file, unless a file sits where
        # one of its folders goes (or a folder where the file goes); then it is
        # simply not kept, and the next read fetches it.
        folders = {path[:index] for index, character in enumerate(path) if character == "/"}
        if folders.isdisjoint(base_blobs) and not any(item.startswith(f"{path}/") for item in base_blobs):
            cache.remember_tree(tree_sha, {**base_blobs, path: blob_sha})

    def _superseded_sources(
        self, records: Sequence[Record], revision: GitHubRevision, blobs: Mapping[str, str]
    ) -> dict[str, str]:
        if not self.archive_superseded:
            return {}
        sources: dict[str, str] = {}
        for record in records:
            if is_routine(record):
                continue
            for old_id in record.supersedes:
                at = _record_id_created_at(old_id)
                path = f"records/{record.kind}/{at:%Y/%m}/{old_id}.md"
                archived = _ARCHIVE_PREFIX + path
                source = path if path in blobs else archived if archived in blobs else None
                if source is None:
                    logger.warning("Brain Home superseded record is missing: %s %s", record.kind, old_id)
                    continue
                older = self._parse(self._read_blobs(revision, (source,), blobs, missing_ok=False).file(source), revision)
                if _record_path(older) != path or older.kind != record.kind:
                    raise GitHubStorageError("github_record_invalid", "Superseded record differs from its path", details={"path": path})
                if is_routine(older):
                    continue
                if path in blobs and archived in blobs and blobs[path] != blobs[archived]:
                    raise GitHubConflict("github_archive_conflict", "Archive contains a different blob", details={"path": archived})
                sources[path] = blobs[source]
        return sources

    @staticmethod
    def _move_entries(sources: Mapping[str, str], blobs: Mapping[str, str], *, restore: bool = False) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path, blob in sources.items():
            source, target = (_ARCHIVE_PREFIX + path, path) if restore else (path, _ARCHIVE_PREFIX + path)
            if source not in blobs:
                continue
            entries.extend([
                {"path": target, "mode": "100644", "type": "blob", "sha": blob},
                {"path": source, "mode": "100644", "type": "blob", "sha": None},
            ])
        return entries

    @staticmethod
    def _moved_tree(blobs: Mapping[str, str], sources: Mapping[str, str], *, restore: bool = False) -> dict[str, str]:
        tree = dict(blobs)
        for path, blob in sources.items():
            source, target = (_ARCHIVE_PREFIX + path, path) if restore else (path, _ARCHIVE_PREFIX + path)
            tree.pop(source, None)
            tree[target] = blob
        return tree

    def publish_record(
        self,
        *,
        record_bytes: bytes,
        expected_revision: str | None = None,
        retry_after_lost_reply: bool = True,
        base_revision: GitHubRevision | None = None,
    ) -> GitHubRecordPublicationResult:
        """Publish one existing validated capture, commitment, or preference.

        Decisions are deliberately excluded: they must use :meth:`publish` so
        the shared writer's exact record and durability receipt land together.

        ``base_revision`` is for a caller that has already reconciled the record
        at a revision this storage returned, as the memory preflight does for
        live and archived records. The record is then written on exactly that
        revision, without reading the branch or its commit list first. The
        branch update is fast-forward only, so if the branch moved since,
        nothing lands and the refusal is ``github_revision_conflict``. On every
        path the accepted update's reply confirms the new head, and a lost reply
        is reconciled before any retry.
        """

        record, path, record_sha256 = self._validate_standalone_record(record_bytes)
        if base_revision is not None and expected_revision is not None:
            raise ValueError("expected_revision and base_revision cannot be combined")
        base: GitHubRevision | None = None if base_revision is None else self._pinned(base_revision)
        if expected_revision is not None:
            _git_object(expected_revision, "expected revision")
            base = self.revision()
            if expected_revision != base.commit_sha:
                raise GitHubConflict(
                    "github_revision_conflict",
                    "Configured branch advanced beyond the expected revision",
                    details={"expected": expected_revision, "actual": base.commit_sha},
                )
        prior = (
            None
            if base_revision is not None
            else self._lookup_record(record, record_bytes, path, record_sha256, status="duplicate/no-op")
        )
        if expected_revision is not None:
            after_reconciliation = self.revision()
            if after_reconciliation.commit_sha != expected_revision:
                raise GitHubConflict(
                    "github_revision_conflict",
                    "Configured branch changed during conditional record reconciliation",
                    details={"expected": expected_revision, "actual": after_reconciliation.commit_sha},
                )
            base = after_reconciliation
        if prior is not None:
            return prior
        if base is None:
            base = self.revision()
        base_blobs = self._tree_blobs(base)
        current = self._read_blobs(base, (path,), base_blobs, missing_ok=True)
        if current.files:
            if current.file(path).content != record_bytes:
                raise GitHubConflict(
                    "github_record_conflict",
                    "Remote record path contains different content",
                    details={"record_id": record.id},
                )
            return self._record_result("duplicate/no-op", record, path, record_sha256, base)

        archive_sources = self._superseded_sources((record,), base, base_blobs)
        moves = self._move_entries(archive_sources, base_blobs)
        blob_sha = self._create_blob(record_bytes)
        if _git_blob_sha(record_bytes, len(blob_sha)) != blob_sha:
            raise GitHubStorageError(
                "github_blob_hash_mismatch",
                "GitHub stored the record under an ID that does not match its content",
                details={"record_id": record.id},
            )
        tree = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/trees",
                expected=(201,),
                payload={
                    "base_tree": base.tree_sha,
                    "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}, *moves],
                },
            ),
            "created tree",
        )
        tree_sha = _git_object(tree.get("sha"), "created tree SHA")
        commit = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/commits",
                expected=(201,),
                payload={"message": self._record_message(record, record_sha256) + (f"Brain-Core-Archived: {len(moves) // 2}\n" if self.archive_superseded else ""), "tree": tree_sha, "parents": [base.commit_sha]},
            ),
            "created commit",
        )
        candidate_sha = _git_object(commit.get("sha"), "created commit SHA")
        try:
            reply = self._patch_ref(candidate_sha)
        except GitHubConflict as refused:
            # A pinned base skipped the branch read, so a refused fast-forward most
            # likely means the branch moved: say so, and the caller reconciles again.
            latest = None if base_revision is None else self.revision()
            if latest is None or latest.commit_sha == base.commit_sha:
                raise
            raise GitHubConflict(
                "github_revision_conflict",
                "Configured branch advanced beyond the pinned revision",
                details={"expected": base.commit_sha, "actual": latest.commit_sha},
            ) from refused
        except GitHubTransportError as first_error:
            accepted = self._lookup_record(record, record_bytes, path, record_sha256, status="reconciled", archive_sources=archive_sources)
            if accepted is not None:
                return accepted
            latest = self.revision()
            if latest.commit_sha == candidate_sha:
                return self._observe_record(
                    record,
                    record_bytes,
                    path,
                    record_sha256,
                    candidate_sha,
                    status="reconciled",
                    archive_sources=archive_sources,
                )
            if latest.commit_sha != base.commit_sha:
                raise GitHubConflict(
                    "github_revision_conflict",
                    "Branch changed while reconciling an uncertain record publication",
                    details={"expected": base.commit_sha, "actual": latest.commit_sha},
                ) from first_error
            if not retry_after_lost_reply:
                raise GitHubPublicationUncertain(
                    "github_publication_uncertain",
                    "GitHub reply was lost and record reconciliation found no accepted commit",
                    details={"record_id": record.id, "record_sha256": record_sha256},
                ) from first_error
            try:
                self._patch_ref(candidate_sha)
            except GitHubTransportError as second_error:
                accepted = self._lookup_record(record, record_bytes, path, record_sha256, status="reconciled", archive_sources=archive_sources)
                if accepted is not None:
                    return accepted
                raise GitHubPublicationUncertain(
                    "github_publication_uncertain",
                    "GitHub reply was lost again and record reconciliation found no accepted commit",
                    details={"record_id": record.id, "record_sha256": record_sha256},
                ) from second_error
            return self._confirm_record_head(record, record_bytes, path, record_sha256, candidate_sha, archive_sources)
        head = self._updated_head(reply)
        if head is None:
            return self._confirm_record_head(record, record_bytes, path, record_sha256, candidate_sha, archive_sources)
        if head != candidate_sha:
            raise GitHubConflict(
                "github_revision_conflict",
                "GitHub did not expose the accepted record commit at the configured branch head",
                details={"candidate": candidate_sha, "actual": head},
            )
        self._remember_publication(self._moved_tree(base_blobs, archive_sources), path, blob_sha, record_bytes, tree_sha, candidate_sha)
        return self._record_result(
            "created",
            record,
            path,
            record_sha256,
            GitHubRevision(self.repository, self.branch, candidate_sha, tree_sha),
        )

    @staticmethod
    def _batch_message(records: Sequence[tuple[Record, str, str]]) -> str:
        """One line per record, distinct from a single record's trailers, so a lookup by one record's
        ID never mistakes a batch commit for that record's own."""

        lines = "".join(f"Brain-Core-Batch-Record: {record.id} {digest}\n" for record, _path, digest in records)
        return f"Publish {len(records)} Brain Core records together\n\n{lines}"

    def observe_commits_since(
        self, expected_head: str, *, revision: GitHubRevision | None = None,
        max_commits: int = 100,
    ) -> GitHubCommitHistory:
        """Read every commit after an exact expected ancestor, oldest first.

        ``revision`` pins the observed head; when absent the branch is read once.
        The base itself is excluded and the head included. An equal base/head
        returns an empty tuple. Missing ancestry, multiple parents, malformed or
        cyclic metadata and history beyond ``max_commits`` (1..100) refuse with
        no partial result. Every returned revision is usable by snapshot_records.
        This interface is read-only and never publishes or advances a branch.
        """
        expected_head = _git_object(expected_head, "expected head SHA")
        if not isinstance(max_commits, int) or isinstance(max_commits, bool) or not 1 <= max_commits <= 100:
            raise ValueError("max_commits must be from 1 through 100")
        head = self.revision() if revision is None else self._pinned(revision)
        current = head.commit_sha
        newest_first: list[GitHubObservedCommit] = []
        seen: set[str] = set()
        while current != expected_head:
            if current in seen:
                raise GitHubStorageError("github_response_invalid", "GitHub commit history contains a cycle")
            if len(newest_first) >= max_commits:
                raise GitHubStorageError("github_work_history_unavailable", "Expected ancestor is outside the bounded history")
            seen.add(current)
            value = _mapping(self._request("GET", f"{self._repo_path}/git/commits/{current}", expected=(200,)), "commit")
            if _git_object(value.get("sha"), "commit SHA") != current:
                raise GitHubStorageError("github_response_invalid", "GitHub commit identity changed")
            tree_sha = _git_object(_mapping(value.get("tree"), "commit tree").get("sha"), "tree SHA")
            message, parents = value.get("message"), value.get("parents")
            if not isinstance(message, str) or not isinstance(parents, list):
                raise GitHubStorageError("github_response_invalid", "GitHub commit history metadata is invalid")
            if len(parents) != 1:
                raise GitHubStorageError("github_work_history_unavailable", "Expected ancestor is not on a complete single-parent chain")
            parent = _git_object(_mapping(parents[0], "commit parent").get("sha"), "parent SHA")
            observed = self._revision_at(current)
            if observed.tree_sha != tree_sha:
                raise GitHubStorageError("github_response_invalid", "GitHub commit tree identity changed")
            newest_first.append(GitHubObservedCommit(observed, parent, message))
            current = parent
        base = self._revision_at(expected_head)
        return GitHubCommitHistory(base, head, tuple(reversed(newest_first)))

    def revision_before_batch(
        self, *, records: Sequence[Record], revision: GitHubRevision | None = None,
    ) -> GitHubBatchHistory:
        """Read the exact accepted batch's parent, within 100 first-parent commits.

        Every supplied ID and canonical byte hash must belong to the same batch.
        The accepted tree must contain those exact bytes and its single parent
        must contain none of their IDs. Missing, partial or conflicting history
        refuses; this helper never publishes or changes the branch.
        """
        if not records or len(records) > 25 or len({r.id for r in records}) != len(records):
            raise ValueError("a batch needs 1 to 25 distinct records")
        expected = {r.id: hashlib.sha256(render_record(r).encode("utf-8")).hexdigest() for r in records}
        pinned = self.revision() if revision is None else self._pinned(revision)
        current = pinned.commit_sha
        seen: set[str] = set()
        for _ in range(100):
            if current in seen:
                raise GitHubStorageError("github_response_invalid", "GitHub commit history contains a cycle")
            seen.add(current)
            value = _mapping(self._request("GET", f"{self._repo_path}/git/commits/{current}", expected=(200,)), "commit")
            if _git_object(value.get("sha"), "commit SHA") != current:
                raise GitHubStorageError("github_response_invalid", "GitHub commit identity changed")
            message = value.get("message")
            parents = value.get("parents")
            if not isinstance(message, str) or not isinstance(parents, list):
                raise GitHubStorageError("github_response_invalid", "GitHub commit history metadata is invalid")
            parent_ids = [_git_object(_mapping(p, "commit parent").get("sha"), "parent SHA") for p in parents]
            lines = [line for line in message.splitlines() if line.startswith("Brain-Core-Batch-Record:")]
            batch: dict[str, str] = {}
            for line in lines:
                match = re.fullmatch(r"Brain-Core-Batch-Record: (brn_[0-9]{8}_[0-9]{6}_[0-9a-f]{4,16}) ([0-9a-f]{64})", line)
                if match is None or match.group(1) in batch:
                    raise GitHubStorageError("github_response_invalid", "GitHub batch trailers are invalid")
                batch[match.group(1)] = match.group(2)
            if expected.keys() & batch.keys():
                if batch != expected or len(parent_ids) != 1:
                    raise GitHubConflict("github_record_identity_conflict", "Accepted batch IDs, hashes or parent differ")
                accepted = self._revision_at(current)
                before = self._revision_at(parent_ids[0])
                paths = tuple(_record_path(r) for r in records)
                observed = self._read_revision(accepted, paths, missing_ok=False)
                for record in records:
                    content = observed.file(_record_path(record)).content
                    if hashlib.sha256(content).hexdigest() != expected[record.id] or self._parse(observed.file(_record_path(record)), accepted) != record:
                        raise GitHubConflict("github_record_identity_conflict", "Accepted batch bytes differ from its trailers")
                if any(self.preflight_record_by_id(rid, revision=before, include_archive=True).match is not None
                       for rid in expected):
                    raise GitHubConflict("github_record_identity_conflict", "Batch records already existed before acceptance")
                return GitHubBatchHistory(accepted, before)
            if len(parent_ids) != 1:
                break
            current = parent_ids[0]
        raise GitHubStorageError("github_work_history_unavailable", "Exact accepted batch is absent from bounded single-parent history")

    def _batch_result(
        self, status: str, records: Sequence[tuple[Record, str, str]], revision: GitHubRevision
    ) -> GitHubBatchPublicationResult:
        return GitHubBatchPublicationResult(
            status,
            self.repository,
            self.branch,
            revision.commit_sha,
            revision.tree_sha,
            tuple(GitHubBatchRecord(record.id, record.kind, path, digest) for record, path, digest in records),
        )

    def _batch_landed(
        self, revision: GitHubRevision, contents: Mapping[str, bytes], blobs: Mapping[str, str] | None = None,
        archive_sources: Mapping[str, str] | None = None,
    ) -> bool | None:
        """True when every record sits at its live or archived path with its exact bytes, False when none does, and
        None when only some do or one differs - never a state this commit can leave."""

        tree = self._tree_blobs(revision) if blobs is None else blobs
        located = {
            path: path if path in tree else _ARCHIVE_PREFIX + path
            for path in contents if path in tree or _ARCHIVE_PREFIX + path in tree
        }
        if not located:
            return False
        if len(located) != len(contents):
            return None
        if any(path in tree and _ARCHIVE_PREFIX + path in tree and tree[path] != tree[_ARCHIVE_PREFIX + path]
               for path in contents):
            raise GitHubConflict("github_archive_conflict", "Batch record has different live and archived blobs")
        if archive_sources and not self._archived(tuple(archive_sources), tree, archive_sources):
            return None
        read = self._read_blobs(revision, tuple(located.values()), tree, missing_ok=False)
        return True if all(read.file(located[path]).content == content for path, content in contents.items()) else None

    def publish_records(
        self,
        *,
        records: Sequence[bytes],
        base_revision: GitHubRevision | None = None,
    ) -> GitHubBatchPublicationResult:
        """Publish several validated standalone records in one commit: all of them or none (PRD
        §4.10; Accepted - Nic, 28 Sep 2026: "Nothing saved; retry").

        Each record is checked as :meth:`publish_record` checks one. The commit adds every file on
        top of ``base_revision`` (or the branch head), and the branch update is fast-forward only, so
        if the branch moved since, nothing lands and the refusal is ``github_revision_conflict``. A
        replay after the commit landed returns ``duplicate/no-op``. A branch holding only some of
        these records, or one with different content, is a conflict, never patched up. A lost reply
        is reconciled by reading the branch: landed is ``reconciled``; not landed is
        ``github_publication_uncertain``, and the caller sends the same records again.
        """

        if isinstance(records, (bytes, str)) or not isinstance(records, Sequence):
            raise TypeError("records must be a sequence of record bytes")
        validated = [self._validate_standalone_record(record_bytes) for record_bytes in records]
        contents = {record_path: record_bytes for (_record, record_path, _digest), record_bytes in zip(validated, records)}
        if not 1 <= len(validated) <= MAX_BATCH_RECORDS or len(contents) != len(validated):
            raise GitHubStorageError(
                "github_batch_invalid",
                f"A batch needs 1 to {MAX_BATCH_RECORDS} records at distinct paths",
                details={"count": len(validated)},
            )
        base = self._pinned(base_revision) if base_revision is not None else self.revision()
        base_blobs = self._tree_blobs(base)
        landed = self._batch_landed(base, contents, base_blobs)
        if landed:
            return self._batch_result("duplicate/no-op", validated, base)
        if landed is None:
            raise GitHubConflict(
                "github_batch_conflict",
                "Some of these records already exist, or exist with different content",
                details={"record_ids": [record.id for record, _path, _digest in validated]},
            )
        if self.archive_superseded and any(old in {r.id for r, _, _ in validated} for r, _, _ in validated for old in r.supersedes):
            raise GitHubConflict("github_batch_conflict", "A batch cannot replace another record in the same batch")
        archive_sources = self._superseded_sources([r for r, _, _ in validated], base, base_blobs)
        entries = self._move_entries(archive_sources, base_blobs)
        moved_count = len(entries) // 2
        created: dict[str, str] = {}
        for (record, record_path, _digest) in validated:
            content = contents[record_path]
            blob_sha = self._create_blob(content)
            if _git_blob_sha(content, len(blob_sha)) != blob_sha:
                raise GitHubStorageError(
                    "github_blob_hash_mismatch",
                    "GitHub stored a record under an ID that does not match its content",
                    details={"record_id": record.id},
                )
            created[record_path] = blob_sha
            entries.append({"path": record_path, "mode": "100644", "type": "blob", "sha": blob_sha})
        tree = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/trees",
                expected=(201,),
                payload={"base_tree": base.tree_sha, "tree": entries},
            ),
            "created tree",
        )
        tree_sha = _git_object(tree.get("sha"), "created tree SHA")
        commit = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/commits",
                expected=(201,),
                payload={"message": self._batch_message(validated) + (f"Brain-Core-Archived: {moved_count}\n" if self.archive_superseded else ""), "tree": tree_sha, "parents": [base.commit_sha]},
            ),
            "created commit",
        )
        candidate_sha = _git_object(commit.get("sha"), "created commit SHA")
        try:
            reply = self._patch_ref(candidate_sha)
        except GitHubConflict as refused:
            latest = self.revision()
            if latest.commit_sha == base.commit_sha:
                raise
            raise GitHubConflict(
                "github_revision_conflict",
                "Configured branch advanced beyond the batch's base revision",
                details={"expected": base.commit_sha, "actual": latest.commit_sha},
            ) from refused
        except GitHubTransportError as first_error:
            latest = self.revision()
            if self._batch_landed(latest, contents, archive_sources=archive_sources):
                return self._batch_result("reconciled", validated, latest)
            if latest.commit_sha != base.commit_sha:
                raise GitHubConflict(
                    "github_revision_conflict",
                    "Branch changed while reconciling an uncertain batch publication",
                    details={"expected": base.commit_sha, "actual": latest.commit_sha},
                ) from first_error
            raise GitHubPublicationUncertain(
                "github_publication_uncertain",
                "GitHub reply was lost and the branch does not show the batch; nothing was saved",
                details={"record_ids": [record.id for record, _path, _digest in validated]},
            ) from first_error
        head = self._updated_head(reply)
        if head is None:
            head = self.revision().commit_sha
        if head != candidate_sha:
            raise GitHubConflict(
                "github_revision_conflict",
                "GitHub did not expose the batch commit at the configured branch head",
                details={"candidate": candidate_sha, "actual": head},
            )
        cache = self._object_cache
        if cache is not None:
            for record_path, blob_sha in created.items():
                cache.remember_blob(blob_sha, contents[record_path])
            cache.remember_commit(candidate_sha, tree_sha)
            folders = {record_path[:index] for record_path in created for index, character in enumerate(record_path) if character == "/"}
            if folders.isdisjoint(base_blobs):
                cache.remember_tree(tree_sha, {**self._moved_tree(base_blobs, archive_sources), **created})
        return self._batch_result(
            "created", validated, GitHubRevision(self.repository, self.branch, candidate_sha, tree_sha)
        )

    @staticmethod
    def _archive_paths(paths: Sequence[str], kinds: frozenset[str] = frozenset({"sitrep"})) -> tuple[str, ...]:
        if not isinstance(kinds, (set, frozenset)) or not kinds or not kinds <= SCAN_KINDS | {"entity"}:
            raise GitHubStorageError("github_archive_kind_refused", "Archive kinds must be an explicit nonempty set of record kinds")
        if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
            raise TypeError("paths must be a sequence of record paths")
        for path in paths:
            match = _ARCHIVABLE_RECORD_PATH.fullmatch(path) if isinstance(path, str) else None
            if match is None or match.group("kind") not in kinds:
                raise GitHubStorageError(
                    "github_archive_kind_refused",
                    "Only record paths of the explicitly allowed kinds can be moved to the archive",
                    details={"path": path if isinstance(path, str) else None},
                )
        requested = tuple(sorted(dict.fromkeys(paths)))
        if not requested or len(requested) != len(paths) or len(requested) > MAX_ARCHIVE_PATHS:
            raise GitHubStorageError(
                "github_archive_invalid",
                f"Archive needs 1 to {MAX_ARCHIVE_PATHS} distinct record paths",
                details={"count": len(paths)},
            )
        return requested

    @staticmethod
    def _archived(requested: tuple[str, ...], blobs: Mapping[str, str], sources: Mapping[str, str]) -> bool:
        return all(
            path not in blobs and blobs.get(_ARCHIVE_PREFIX + path) == sources[path]
            for path in requested
        )

    def _archive_result(
        self, status: str, requested: tuple[str, ...], revision: GitHubRevision
    ) -> GitHubArchiveResult:
        return GitHubArchiveResult(
            status,
            self.repository,
            self.branch,
            revision.commit_sha,
            revision.tree_sha,
            tuple((path, _ARCHIVE_PREFIX + path) for path in requested),
        )

    def archive_records(
        self,
        *,
        paths: Sequence[str],
        expected_revision: str | None = None,
        kinds: frozenset[str] = frozenset({"sitrep"}),
    ) -> GitHubArchiveResult:
        """Move whole record files to ``archive/<path>``, byte for byte, in one commit.

        Step C's archive path (its section 7): each file keeps its Git blob, so the move is
        exact and can be undone by moving it back or reverting the one commit. Only
        explicitly selected kinds (sitrep by default) can be moved here, and each is read and checked first. The
        branch update is fast-forward only. A replay after the move landed returns
        ``duplicate/no-op``; a lost reply is reconciled by reading the branch, never retried
        blindly.
        """

        requested = self._archive_paths(paths, kinds)
        base = self.revision()
        if expected_revision is not None and _git_object(expected_revision, "expected revision") != base.commit_sha:
            raise GitHubConflict(
                "github_revision_conflict",
                "Configured branch advanced beyond the expected revision",
                details={"expected": expected_revision, "actual": base.commit_sha},
            )
        blobs = self._tree_blobs(base)
        if all(path not in blobs and _ARCHIVE_PREFIX + path in blobs for path in requested):
            return self._archive_result("duplicate/no-op", requested, base)
        for path in requested:
            if path not in blobs:
                raise GitHubStorageError("github_path_missing", "Record to archive is absent", details={"path": path})
            if _ARCHIVE_PREFIX + path in blobs:
                raise GitHubConflict(
                    "github_archive_conflict",
                    "The archive path is already taken",
                    details={"path": _ARCHIVE_PREFIX + path},
                )
        for item in self._read_blobs(base, requested, blobs, missing_ok=False).files:
            record = self._parse(item, base)
            if _record_path(record) != item.path or record.kind not in kinds:
                raise GitHubStorageError(
                    "github_record_invalid",
                    "Record path or kind differs from its envelope",
                    details={"path": item.path, "record_id": record.id},
                )
        sources = {path: blobs[path] for path in requested}
        entries: list[dict[str, Any]] = []
        for path in requested:
            entries.append({"path": _ARCHIVE_PREFIX + path, "mode": "100644", "type": "blob", "sha": sources[path]})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        tree = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/trees",
                expected=(201,),
                payload={"base_tree": base.tree_sha, "tree": entries},
            ),
            "created tree",
        )
        tree_sha = _git_object(tree.get("sha"), "created tree SHA")
        digest = hashlib.sha256(canonical_json(list(requested)).encode("utf-8")).hexdigest()
        message = (
            f"Archive {len(requested)} Brain Core {'sitrep' if kinds == {'sitrep'} else 'selected'} records\n\n"
            f"Brain-Core-Archive-Count: {len(requested)}\n"
            f"Brain-Core-Archive-SHA256: {digest}\n"
        )
        commit = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/commits",
                expected=(201,),
                payload={"message": message, "tree": tree_sha, "parents": [base.commit_sha]},
            ),
            "created commit",
        )
        candidate_sha = _git_object(commit.get("sha"), "created commit SHA")
        try:
            reply = self._patch_ref(candidate_sha)
        except GitHubTransportError as first_error:
            latest = self.revision()
            if self._archived(requested, self._tree_blobs(latest), sources):
                return self._archive_result("reconciled", requested, latest)
            raise GitHubPublicationUncertain(
                "github_archive_uncertain",
                "GitHub reply was lost and the branch does not show the archive move",
                details={"count": len(requested), "paths_sha256": digest},
            ) from first_error
        head = self._updated_head(reply)
        if head is None:
            head = self.revision().commit_sha
        if head != candidate_sha:
            raise GitHubConflict(
                "github_revision_conflict",
                "GitHub did not expose the archive commit at the configured branch head",
                details={"candidate": candidate_sha, "actual": head},
            )
        cache = self._object_cache
        if cache is not None:
            moved = {path: blob for path, blob in blobs.items() if path not in sources}
            moved.update({_ARCHIVE_PREFIX + path: blob for path, blob in sources.items()})
            cache.remember_commit(candidate_sha, tree_sha)
            cache.remember_tree(tree_sha, moved)
        return self._archive_result(
            "archived", requested, GitHubRevision(self.repository, self.branch, candidate_sha, tree_sha)
        )

    def archive_superseded_records(
        self, *, paths: Sequence[str], expected_revision: str
    ) -> GitHubArchiveResult:
        """Move only versions named by a current record of the same kind; refuse routines.

        Validate every source and its current successor at one pinned head before creating
        any object. The update is fast-forward only; a replay checks the archived blobs.
        """
        return self._move_checked_records(paths, expected_revision, restore=False)

    def restore_archived_records(
        self, *, paths: Sequence[str], expected_revision: str
    ) -> GitHubArchiveResult:
        """Reverse an archive move using canonical ``records/...`` paths and identical blobs.

        An existing live path with different bytes is a conflict, never overwritten.
        Disable automatic archiving before a maintenance restore.
        """
        return self._move_checked_records(paths, expected_revision, restore=True)

    def _move_checked_records(
        self, paths: Sequence[str], expected_revision: str, *, restore: bool
    ) -> GitHubArchiveResult:
        requested = self._archive_paths(paths, SCAN_KINDS | {"entity"})
        base = self.revision()
        if _git_object(expected_revision, "expected revision") != base.commit_sha:
            raise GitHubConflict(
                "github_revision_conflict", "Configured branch advanced beyond the expected revision",
                details={"expected": expected_revision, "actual": base.commit_sha},
            )
        blobs = self._tree_blobs(base)
        sources: dict[str, str] = {}
        records: dict[str, Record] = {}
        for path in requested:
            archived = _ARCHIVE_PREFIX + path
            source, target = (archived, path) if restore else (path, archived)
            if source not in blobs:
                if target not in blobs:
                    raise GitHubStorageError("github_path_missing", "Record to move is absent", details={"path": path})
                source = target  # replay: still validate its envelope
            if path in blobs and archived in blobs and blobs[path] != blobs[archived]:
                raise GitHubConflict("github_archive_conflict", "Destination contains a different blob", details={"path": target})
            record = self._parse(self._read_blobs(base, (source,), blobs, missing_ok=False).file(source), base)
            if _record_path(record) != path:
                raise GitHubStorageError("github_record_invalid", "Record differs from its path", details={"path": path})
            if not restore and is_routine(record):
                raise GitHubStorageError("github_archive_routine_refused", "Routine history must stay live", details={"path": path})
            records[path] = record
            sources[path] = blobs[source]
        if not restore:
            kinds = {record.kind for record in records.values()}
            live_paths = tuple(path for path in blobs if (match := _ARCHIVABLE_RECORD_PATH.fullmatch(path)) and match.group("kind") in kinds)
            live: list[Record] = []
            for item in self._read_blobs(base, live_paths, blobs, missing_ok=False).files:
                record = self._parse(item, base)
                if _record_path(record) != item.path:
                    raise GitHubStorageError("github_record_invalid", "Current record differs from its path", details={"path": item.path})
                live.append(record)
            replaced = {(record.kind, old) for record in live for old in record.supersedes}
            named = {(record.kind, old) for record in live if (record.kind, record.id) not in replaced for old in record.supersedes}
            for path, record in records.items():
                if (record.kind, record.id) not in named:
                    raise GitHubStorageError(
                        "github_archive_not_superseded", "No current record of this kind names the version",
                        details={"path": path, "record_id": record.id},
                    )
        entries = self._move_entries(sources, blobs, restore=restore)
        moved_pairs = tuple((_ARCHIVE_PREFIX + p, p) if restore else (p, _ARCHIVE_PREFIX + p) for p in requested)

        def result(status: str, revision: GitHubRevision) -> GitHubArchiveResult:
            return GitHubArchiveResult(status, self.repository, self.branch, revision.commit_sha, revision.tree_sha, moved_pairs)

        if not entries:
            return result("duplicate/no-op", base)
        tree = _mapping(self._request(
            "POST", f"{self._repo_path}/git/trees", expected=(201,),
            payload={"base_tree": base.tree_sha, "tree": entries},
        ), "created tree")
        tree_sha = _git_object(tree.get("sha"), "created tree SHA")
        action = "Restore" if restore else "Archive superseded"
        digest = hashlib.sha256(canonical_json(list(requested)).encode("utf-8")).hexdigest()
        commit = _mapping(self._request(
            "POST", f"{self._repo_path}/git/commits", expected=(201,),
            payload={
                "message": f"{action} {len(entries) // 2} Brain Core records\n\nBrain-Core-Archive-Count: {len(entries) // 2}\nBrain-Core-Archive-SHA256: {digest}\n",
                "tree": tree_sha, "parents": [base.commit_sha],
            },
        ), "created commit")
        candidate = _git_object(commit.get("sha"), "created commit SHA")
        try:
            reply = self._patch_ref(candidate)
        except GitHubConflict as refused:
            latest = self.revision()
            if latest.commit_sha == base.commit_sha:
                raise
            raise GitHubConflict("github_revision_conflict", "Branch advanced during the move",
                                 details={"expected": base.commit_sha, "actual": latest.commit_sha}) from refused
        except GitHubTransportError as lost:
            latest = self.revision()
            actual = self._tree_blobs(latest)
            if all(source not in actual and actual.get(target) == sources[path]
                   for path, (source, target) in zip(requested, moved_pairs)):
                return result("reconciled", latest)
            raise GitHubPublicationUncertain("github_archive_uncertain", "Lost reply does not confirm the exact move",
                                             details={"count": len(requested), "paths_sha256": digest}) from lost
        head = self._updated_head(reply)
        if head is None:
            head = self.revision().commit_sha
        if head != candidate:
            raise GitHubConflict("github_revision_conflict", "GitHub did not expose the move at the branch head",
                                 details={"candidate": candidate, "actual": head})
        if self._object_cache is not None:
            self._object_cache.remember_commit(candidate, tree_sha)
            self._object_cache.remember_tree(tree_sha, self._moved_tree(blobs, sources, restore=restore))
        return result("restored" if restore else "archived", GitHubRevision(self.repository, self.branch, candidate, tree_sha))

    def publish(
        self,
        *,
        record_bytes: bytes,
        receipt_bytes: bytes,
        expected_revision: str | None = None,
        retry_after_lost_reply: bool = True,
    ) -> GitHubPublicationResult:
        """Atomically publish an existing writer record/receipt in one commit.

        Every call first reconciles the stable origin. A lost ref-update reply is
        reconciled again before the exact same candidate commit may be retried.
        """

        publication = self._validate_publication(record_bytes, receipt_bytes)
        base: GitHubRevision | None = None
        if expected_revision is not None:
            _git_object(expected_revision, "expected revision")
            base = self.revision()
            if expected_revision != base.commit_sha:
                raise GitHubConflict(
                    "github_revision_conflict",
                    "Configured branch advanced beyond the expected revision",
                    details={"expected": expected_revision, "actual": base.commit_sha},
                )
        prior = self._lookup(publication, status="duplicate/no-op")
        if expected_revision is not None:
            after_reconciliation = self.revision()
            if after_reconciliation.commit_sha != expected_revision:
                raise GitHubConflict(
                    "github_revision_conflict",
                    "Configured branch changed during conditional publication reconciliation",
                    details={"expected": expected_revision, "actual": after_reconciliation.commit_sha},
                )
            base = after_reconciliation
        if prior is not None:
            return prior

        if base is None:
            base = self.revision()
        current = self._read_revision(
            base,
            (publication.record_path, publication.receipt_path),
            missing_ok=True,
        )
        existing = {item.path: item for item in current.files}
        record_existing = existing.get(publication.record_path)
        receipt_existing = existing.get(publication.receipt_path)
        if record_existing is not None and record_existing.content != publication.record_bytes:
            raise GitHubConflict("github_record_conflict", "Remote record path contains different content")
        if receipt_existing is not None and receipt_existing.content != publication.receipt_bytes:
            raise GitHubConflict("github_receipt_conflict", "Remote receipt path contains different content")
        if record_existing is not None and receipt_existing is not None:
            return self._result("duplicate/no-op", publication, base)

        record_blob = record_existing.blob_sha if record_existing is not None else self._create_blob(record_bytes)
        receipt_blob = receipt_existing.blob_sha if receipt_existing is not None else self._create_blob(receipt_bytes)
        tree = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/trees",
                expected=(201,),
                payload={
                    "base_tree": base.tree_sha,
                    "tree": [
                        {"path": publication.record_path, "mode": "100644", "type": "blob", "sha": record_blob},
                        {"path": publication.receipt_path, "mode": "100644", "type": "blob", "sha": receipt_blob},
                    ],
                },
            ),
            "created tree",
        )
        tree_sha = _git_object(tree.get("sha"), "created tree SHA")
        commit = _mapping(
            self._request(
                "POST",
                f"{self._repo_path}/git/commits",
                expected=(201,),
                payload={"message": self._message(publication), "tree": tree_sha, "parents": [base.commit_sha]},
            ),
            "created commit",
        )
        candidate_sha = _git_object(commit.get("sha"), "created commit SHA")

        try:
            self._patch_ref(candidate_sha)
        except GitHubTransportError as first_error:
            accepted = self._lookup(publication, status="reconciled")
            if accepted is not None:
                return accepted
            latest = self.revision()
            if latest.commit_sha == candidate_sha:
                return self._observe_exact(publication, candidate_sha, status="reconciled")
            if latest.commit_sha != base.commit_sha:
                raise GitHubConflict(
                    "github_revision_conflict",
                    "Branch changed while reconciling an uncertain publication",
                    details={"expected": base.commit_sha, "actual": latest.commit_sha},
                ) from first_error
            if not retry_after_lost_reply:
                raise GitHubPublicationUncertain(
                    "github_publication_uncertain",
                    "GitHub reply was lost and reconciliation found no accepted commit",
                    details={"origin_id": publication.origin_id, "input_sha256": publication.input_sha256},
                ) from first_error
            try:
                self._patch_ref(candidate_sha)
            except GitHubTransportError as second_error:
                accepted = self._lookup(publication, status="reconciled")
                if accepted is not None:
                    return accepted
                raise GitHubPublicationUncertain(
                    "github_publication_uncertain",
                    "GitHub reply was lost again and reconciliation found no accepted commit",
                    details={"origin_id": publication.origin_id, "input_sha256": publication.input_sha256},
                ) from second_error

        head = self.revision()
        if head.commit_sha != candidate_sha:
            raise GitHubConflict(
                "github_revision_conflict",
                "GitHub did not expose the accepted candidate at the configured branch head",
                details={"candidate": candidate_sha, "actual": head.commit_sha},
            )
        return self._observe_exact(publication, candidate_sha, status="created")


__all__ = [
    "GitHubArchiveResult",
    "GitHubBrainHomeStorage",
    "GitHubConflict",
    "GitHubDecisionMatch",
    "GitHubDecisionPreflight",
    "GitHubFile",
    "GitHubMemoryOperationPreflight",
    "GitHubPublicationResult",
    "GitHubPublicationUncertain",
    "GitHubRead",
    "GitHubRecordFile",
    "GitHubRecordMatch",
    "GitHubRecordPreflight",
    "GitHubRecordPublicationResult",
    "GitHubRecordRead",
    "GitHubRevision",
    "GitHubStorageError",
    "GitHubTransportError",
    "HTTPResponse",
    "HTTPTransport",
    "MAX_ARCHIVE_PATHS",
    "MAX_DECISION_CANDIDATES",
    "MAX_MEMORY_OPERATION_CANDIDATES",
    "MAX_RECORD_CANDIDATES",
    "MAX_RESPONSE_BYTES",
    "UrllibHTTPTransport",
    "memory_operation_dedupe_key",
    "memory_operation_record_suffix",
]
