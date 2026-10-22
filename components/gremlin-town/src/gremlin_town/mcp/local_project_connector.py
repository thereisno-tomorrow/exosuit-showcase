"""Scoped network connector for current PC-local Brain Core project context.

The server side is intentionally separate from the shared MCP process.  It is
configured by the PC host with enrolled Brain Core roots, grants, write paths,
and hashed bearer tokens.  Network callers can name only an enrolled project
and repository-relative path; they cannot supply roots, grants, or policy.

The hosted MCP can use :class:`LocalProjectConnectorClient` without installing
Brain Core.  Brain Core is imported lazily only when the PC-side server starts.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import select
import socket
import stat
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from gremlin_town.mcp.brain_runtime import (
    BrainOperation,
    BrainRuntimeError,
    ContextQuery,
    ContextResult,
    ContinuityAppendRequest,
    ContinuityCloseRequest,
    ContinuityListRequest,
    ContinuityListResult,
    ContinuityRecoverRequest,
    ContinuityRecovery,
    ContinuityStartRequest,
    ContinuitySummary,
    ContinuityWriteResult,
    OwnerAuthorization,
    RetrievalExclusion,
    SourceEvidence,
    WriteReceipt,
)


CONFIG_SCHEMA = "gt.brain-local-connector/v1"
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 5_000_000
MAX_WRITE_BYTES = 1_000_000
_REPARSE_POINT = 0x0400
# ROA-34: bounded poll used only to drain a body an already-rejected
# caller genuinely sent (see _ConnectorHandler._drain_pending_body).
_DRAIN_POLL_SECONDS = 0.05
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

_ROUTES = {
    "/v1/context/read": "context:read",
    "/v1/context/retrieve": "context:read",
    "/v1/files/write": "project:write",
    "/v1/continuity/start": "continuity:write",
    "/v1/continuity/append": "continuity:write",
    "/v1/continuity/recover": "continuity:read",
    "/v1/continuity/list": "continuity:read",
    "/v1/continuity/close": "continuity:write",
    # Sitreps (docs/product/steps/b-project-sitreps.md): only the Focus section of the Compass
    # of the vault that holds a project. A Compass is a vault's declared export, so the
    # ordinary context scope covers it.
    "/v1/compass/focus": "context:read",
}
# ROA-72 (Nic, 28 Sep 2026: "ops/sessions/ in each vault"): where a registry vault's notes go,
# beside its `last-active.md`.
NOTES_FOLDER = PurePosixPath("ops", "sessions")
# How far above an enrolled project root the connector looks for the Vault OS vault that
# holds it (the root itself, then up to three parent folders), and the read limits.
_VAULT_SEARCH_LEVELS = 3
MAX_MANIFEST_BYTES = 65_536
MAX_COMPASS_BYTES = 262_144
MAX_FOCUS_CHARS = 4_000


class ConnectorError(RuntimeError):
    """Stable client/server failure without protected body content."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            value["details"] = self.details
        return value


class ConnectorUnavailable(ConnectorError):
    """The PC connector could not be reached; no cached result is substituted."""

    def __init__(self, message: str = "PC-local connector is unavailable") -> None:
        super().__init__("PC_UNAVAILABLE", message, status=503)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        del req, fp, code, msg, headers, newurl
        return None


def _read_bounded_response(response: Any) -> bytes:
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            if int(raw_length) > MAX_RESPONSE_BYTES:
                raise ConnectorError("RESPONSE_TOO_LARGE", "connector response is too large", status=502)
        except ValueError as exc:
            raise ConnectorError("INVALID_RESPONSE", "connector response length is invalid", status=502) from exc
    value = response.read(MAX_RESPONSE_BYTES + 1)
    if len(value) > MAX_RESPONSE_BYTES:
        raise ConnectorError("RESPONSE_TOO_LARGE", "connector response is too large", status=502)
    return value


@dataclass(frozen=True, slots=True)
class _TokenPolicy:
    token_sha256: str
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class _RegistryEnrollment:
    """A vault enrolled from MetaVault's registry (ROA-72): what it declares readable, where that
    came from, any declared path left out as unsafe, and the scan limits."""

    exports: tuple[str, ...]
    exports_source: str
    ignored: tuple[str, ...]
    limits: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _Project:
    project_id: str
    manifest: Any
    retrieval_catalog: Path
    grants: tuple[Any, ...]
    write_paths: frozenset[str]
    continuity_root: Path
    folder_catalog: dict[str, Any] | None = None
    # Where repeated-save bookkeeping lives: beside the notes for a hand-set project (as before),
    # in the connector's own state folder for a registry vault, so only notes land in vaults.
    operations_root: Path | None = None
    registry: _RegistryEnrollment | None = None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _exact_object(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConnectorError("INVALID_REQUEST", "request body must be a JSON object", status=422)
    optional = optional or set()
    actual = set(value)
    missing = required - actual
    unknown = actual - required - optional
    if missing or unknown:
        raise ConnectorError(
            "INVALID_REQUEST",
            "request fields do not match the operation contract",
            status=422,
            details={"missing": sorted(missing), "unknown": sorted(unknown)},
        )
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be a non-empty string", status=422)
    return value


def _safe_bind_address(
    value: Any,
    encrypted_overlay_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> str:
    address = _text(value, "bind_host")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ConnectorError("CONFIGURATION_INVALID", "bind_host must be an IP literal") from exc
    if parsed.is_unspecified:
        raise ConnectorError("CONFIGURATION_INVALID", "connector refuses a wildcard bind address")
    overlay_address = parsed.is_private or (
        isinstance(parsed, ipaddress.IPv4Address) and parsed in _CGNAT
    )
    if not (
        parsed.is_loopback
        or (
            overlay_address
            and any(parsed in network for network in encrypted_overlay_networks)
        )
    ):
        raise ConnectorError(
            "CONFIGURATION_INVALID",
            "non-loopback bind address is not in the configured encrypted overlay",
        )
    return address


def _is_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ConnectorError("PATH_MISSING", "configured path is unavailable", status=404) from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    return stat.S_ISLNK(value.st_mode) or bool(attributes & _REPARSE_POINT)


def _assert_no_reparse(root: Path, relative: str, normalize_relative: Any) -> tuple[Path, str]:
    normalized = normalize_relative(relative)
    if _is_reparse(root):
        raise ConnectorError("REPARSE_REFUSED", "enrolled root is a reparse point", status=403)
    candidate = root
    for part in PurePosixPath(normalized).parts:
        candidate = candidate / part
        if _is_reparse(candidate):
            raise ConnectorError(
                "REPARSE_REFUSED",
                "symlink or reparse-point path refused",
                status=403,
                details={"relative_path": normalized},
            )
    return candidate, normalized


def _load_brain(brain_src: str | Path | None = None) -> SimpleNamespace:
    if brain_src is not None:
        source = str(Path(brain_src).resolve(strict=True))
        if source not in sys.path:
            sys.path.insert(0, source)
    try:
        from brain_core.continuity import (
            append_event,
            close_continuity,
            list_continuity,
            recover_continuity,
            start_continuity,
        )
        from brain_core.contracts import grant_from_mapping, parse_yaml
        from brain_core.discovery import discover_catalog
        from brain_core.errors import BrainError, ErrorCode
        from brain_core.local_retrieval import parse_catalog, retrieve
        from brain_core.paths import normalize_relative, read_local_text, read_snapshot, resolve_repository_path
        from brain_core.policy import check_read_access
        from brain_core.records import Evidence, RecordStore
    except ImportError as exc:
        raise ConnectorError(
            "BRAIN_CORE_UNAVAILABLE",
            "Brain Core is unavailable in the PC connector runtime",
            status=503,
        ) from exc
    # ROA-72: enrollment from MetaVault's registry needs a Brain Core from release ciel-03 on; an
    # older one still serves a config without a registry block.
    try:
        from brain_core.discovery import read_registry, registry_manifest, vault_exports
    except ImportError:
        read_registry = registry_manifest = vault_exports = None  # type: ignore[assignment]
    return SimpleNamespace(
        read_registry=read_registry,
        registry_manifest=registry_manifest,
        vault_exports=vault_exports,
        append_event=append_event,
        close_continuity=close_continuity,
        list_continuity=list_continuity,
        recover_continuity=recover_continuity,
        start_continuity=start_continuity,
        grant_from_mapping=grant_from_mapping,
        parse_yaml=parse_yaml,
        discover_catalog=discover_catalog,
        BrainError=BrainError,
        ErrorCode=ErrorCode,
        parse_catalog=parse_catalog,
        retrieve=retrieve,
        normalize_relative=normalize_relative,
        read_local_text=read_local_text,
        read_snapshot=read_snapshot,
        resolve_repository_path=resolve_repository_path,
        check_read_access=check_read_access,
        Evidence=Evidence,
        RecordStore=RecordStore,
    )


def _brain_error(error: Exception, brain: SimpleNamespace) -> ConnectorError:
    if not isinstance(error, brain.BrainError):
        return ConnectorError("CONNECTOR_FAILURE", "PC connector failed safely", status=500)
    denied = {
        brain.ErrorCode.GRANT_MISSING,
        brain.ErrorCode.GRANT_EXPIRED,
        brain.ErrorCode.GRANT_WRONG_PATH,
        brain.ErrorCode.POLICY_EXCLUDED,
    }
    conflict = {brain.ErrorCode.SOURCE_CHANGED, brain.ErrorCode.RECORD_EXISTS}
    missing = {brain.ErrorCode.PATH_MISSING, brain.ErrorCode.NOT_FOUND, brain.ErrorCode.CONTINUITY_EMPTY}
    status = 403 if error.code in denied else 409 if error.code in conflict else 404 if error.code in missing else 400
    return ConnectorError(error.code.value, error.message, status=status)


@contextlib.contextmanager
def _exclusive_file_lock(path: Path):
    """Cross-process lock for one project's durable operation reconciliation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class LocalProjectConnectorRuntime:
    """Host-constructed PC-side runtime; callers cannot alter its trust inputs."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        brain_src: str | Path | None = None,
        operation_fault: Any | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve(strict=True)
        self.brain = _load_brain(brain_src)
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("CONFIGURATION_INVALID", "connector config is unreadable or invalid") from exc
        config = _exact_object(
            raw,
            {
                "schema",
                "bind_host",
                "port",
                "encrypted_overlay_networks",
                "trusted_client_networks",
                "auth",
                "node_map",
                "projects",
            },
            {"registry"},
        )
        if config["schema"] != CONFIG_SCHEMA:
            raise ConnectorError("CONFIGURATION_INVALID", "connector config schema is unsupported")
        self.encrypted_overlay_networks = self._networks(
            config["encrypted_overlay_networks"], field="encrypted_overlay_networks", allow_empty=True
        )
        self.bind_host = _safe_bind_address(config["bind_host"], self.encrypted_overlay_networks)
        port = config["port"]
        if type(port) is not int or not 0 <= port <= 65535:
            raise ConnectorError("CONFIGURATION_INVALID", "port must be an integer from 0 through 65535")
        self.port = port
        self.trusted_client_networks = self._networks(
            config["trusted_client_networks"], field="trusted_client_networks"
        )
        self.tokens = self._tokens(config["auth"])

        node_map_path = Path(_text(config["node_map"], "node_map")).resolve(strict=True)
        try:
            node_map, manifests = self.brain.discover_catalog(node_map_path)
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc
        raw_roots = {entry.id: Path(entry.root) for entry in node_map.vaults}
        for root in raw_roots.values():
            if _is_reparse(root):
                raise ConnectorError("CONFIGURATION_INVALID", "enrolled root may not be a reparse point")
        manifest_by_id = {manifest.id: manifest for manifest in manifests}
        self.node = node_map.node
        self.projects = self._projects(
            config["projects"], manifest_by_id, allow_empty="registry" in config
        )
        # ROA-72: every vault in MetaVault's registry whose folder is on this PC, as vault_<name>.
        self.registry_skipped: tuple[dict[str, str], ...] = ()
        if "registry" in config:
            enrolled, self.registry_skipped = self._registry_projects(config["registry"])
            both = sorted(set(enrolled) & set(self.projects))
            if both:
                raise ConnectorError(
                    "CONFIGURATION_INVALID",
                    "project IDs set by hand are also in the registry: " + ", ".join(both),
                )
            self.projects.update(enrolled)

        self.http_body_reads = 0
        self.source_body_reads = 0
        self._write_lock = threading.Lock()
        self._operation_fault = operation_fault

    @staticmethod
    def _networks(
        value: Any,
        *,
        field: str,
        allow_empty: bool = False,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        if not isinstance(value, list) or (not value and not allow_empty):
            raise ConnectorError("CONFIGURATION_INVALID", f"{field} must be a {'possibly empty' if allow_empty else 'non-empty'} list")
        try:
            networks = tuple(ipaddress.ip_network(item, strict=True) for item in value)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("CONFIGURATION_INVALID", f"{field} contains an invalid network") from exc
        return networks

    @staticmethod
    def _tokens(value: Any) -> tuple[_TokenPolicy, ...]:
        if not isinstance(value, list) or not value:
            raise ConnectorError("CONFIGURATION_INVALID", "auth must contain hashed token policies")
        result: list[_TokenPolicy] = []
        for item in value:
            data = _exact_object(item, {"token_sha256", "scopes"})
            digest = _text(data["token_sha256"], "token_sha256")
            scopes = data["scopes"]
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ConnectorError("CONFIGURATION_INVALID", "token_sha256 must be lowercase SHA-256")
            if not isinstance(scopes, list) or not scopes or any(not isinstance(scope, str) for scope in scopes):
                raise ConnectorError("CONFIGURATION_INVALID", "token scopes must be a non-empty string list")
            result.append(_TokenPolicy(digest, frozenset(scopes)))
        return tuple(result)

    def _projects(
        self, value: Any, manifest_by_id: Mapping[str, Any], *, allow_empty: bool = False
    ) -> dict[str, _Project]:
        if not isinstance(value, list) or (not value and not allow_empty):
            raise ConnectorError("CONFIGURATION_INVALID", "projects must be a non-empty list")
        projects: dict[str, _Project] = {}
        for position, item in enumerate(value):
            data = _exact_object(
                item,
                {"id", "retrieval_catalog", "grants", "write_paths", "continuity_root"},
                {"folder_catalog"},
            )
            project_id = _text(data["id"], "projects[].id")
            if project_id in projects or project_id not in manifest_by_id:
                raise ConnectorError("CONFIGURATION_INVALID", "project ID is duplicate or not discovered")
            manifest = manifest_by_id[project_id]
            catalog_relative = self.brain.normalize_relative(_text(data["retrieval_catalog"], "retrieval_catalog"))
            catalog_path, _ = self.brain.resolve_repository_path(manifest.root, catalog_relative)
            raw_grants = data["grants"]
            if not isinstance(raw_grants, list):
                raise ConnectorError("CONFIGURATION_INVALID", "project grants must be a list")
            grants = tuple(
                self.brain.grant_from_mapping(grant, f"projects[{position}].grants[{index}]")
                for index, grant in enumerate(raw_grants)
            )
            raw_write_paths = data["write_paths"]
            if not isinstance(raw_write_paths, list):
                raise ConnectorError("CONFIGURATION_INVALID", "write_paths must be a list")
            write_paths = frozenset(self.brain.normalize_relative(path) for path in raw_write_paths)
            continuity_root = Path(_text(data["continuity_root"], "continuity_root"))
            if _is_reparse(continuity_root) or not continuity_root.is_dir():
                raise ConnectorError("CONFIGURATION_INVALID", "project continuity_root must be a real directory")
            folder_catalog = data.get("folder_catalog")
            if folder_catalog is not None and not isinstance(folder_catalog, dict):
                raise ConnectorError("CONFIGURATION_INVALID", "folder_catalog must be an object")
            projects[project_id] = _Project(
                project_id,
                manifest,
                catalog_path,
                grants,
                write_paths,
                continuity_root.resolve(strict=True),
                folder_catalog,
            )
        return projects

    def _registry_projects(self, value: Any) -> tuple[dict[str, _Project], tuple[dict[str, str], ...]]:
        """Every vault in MetaVault's registry whose folder is on this PC (ROA-72), and the entries
        left out with the reason. Nothing is written into a vault to enroll it: its notes folder,
        ``ops/sessions/``, is made by the first save, and its search catalog and repeated-save
        bookkeeping live under ``state_dir``."""

        data = _exact_object(value, {"path", "state_dir"}, {"limits"})
        if self.brain.read_registry is None:
            raise ConnectorError(
                "BRAIN_CORE_UNAVAILABLE",
                "this Brain Core cannot read MetaVault's registry",
                status=503,
            )
        try:
            registry_path = Path(_text(data["path"], "registry.path")).resolve(strict=True)
        except OSError as exc:
            raise ConnectorError("CONFIGURATION_INVALID", "registry.path is unavailable") from exc
        state_dir = Path(_text(data["state_dir"], "registry.state_dir"))
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConnectorError("CONFIGURATION_INVALID", "registry.state_dir cannot be created") from exc
        if _is_reparse(state_dir) or not state_dir.is_dir():
            raise ConnectorError("CONFIGURATION_INVALID", "registry.state_dir must be a real directory")
        state_dir = state_dir.resolve(strict=True)
        limits = data.get("limits", {})
        if not isinstance(limits, dict):
            raise ConnectorError("CONFIGURATION_INVALID", "registry.limits must be an object")
        try:
            vaults, skipped = self.brain.read_registry(registry_path)
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc
        left_out = [{"name": item.name, "root": item.root, "reason": item.reason} for item in skipped]
        projects: dict[str, _Project] = {}
        for vault in vaults:
            root = Path(vault.root)
            if _is_reparse(root):
                left_out.append({"name": vault.name, "root": vault.root, "reason": "root_is_reparse_point"})
                continue
            try:
                exports = self.brain.vault_exports(vault)
                manifest = self.brain.registry_manifest(vault, exports.paths)
            except self.brain.BrainError as exc:
                left_out.append({"name": vault.name, "root": vault.root, "reason": f"exports_invalid: {exc.code.value}"})
                continue
            projects[vault.id] = _Project(
                vault.id,
                manifest,
                state_dir / "catalogs" / f"{vault.id}.json",
                (),
                frozenset(),
                root / NOTES_FOLDER,
                operations_root=state_dir / "operations" / vault.id,
                registry=_RegistryEnrollment(exports.paths, exports.source, exports.ignored, dict(limits)),
            )
        return projects, tuple(left_out)

    def enrollment(self) -> dict[str, Any]:
        """Which projects this connector serves and which registry entries it left out, without
        roots' contents or credentials: the release generates the hub's project owners from it."""

        def entry(project: _Project) -> dict[str, Any]:
            value: dict[str, Any] = {
                "id": project.project_id,
                "source": "config" if project.registry is None else "registry",
                "root": Path(project.manifest.root).as_posix(),
                "notes": project.continuity_root.as_posix(),
            }
            if project.registry is not None:
                value.update(
                    exports=list(project.registry.exports),
                    exports_from=project.registry.exports_source,
                    ignored_exports=list(project.registry.ignored),
                )
            return value

        return {
            "node": self.node,
            "projects": [entry(project) for _, project in sorted(self.projects.items())],
            "skipped": [dict(item) for item in self.registry_skipped],
        }

    def client_allowed(self, address: str) -> bool:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(parsed in network for network in self.trusted_client_networks)

    def authorize(self, token: str, required_scope: str) -> None:
        digest = _sha256_text(token)
        policy = next((item for item in self.tokens if hmac.compare_digest(item.token_sha256, digest)), None)
        if policy is None:
            raise ConnectorError("UNAUTHENTICATED", "connector credential was rejected", status=401)
        if required_scope not in policy.scopes:
            raise ConnectorError("ACCESS_DENIED", "connector scope was rejected", status=403)

    def _project(self, project_id: Any) -> _Project:
        identifier = _text(project_id, "project_id")
        project = self.projects.get(identifier)
        if project is None:
            raise ConnectorError("PROJECT_NOT_ENROLLED", "project is not enrolled", status=404)
        return project

    def _source_access(self, project: _Project, relative: str, session_id: str) -> Any:
        try:
            decision = self.brain.check_read_access(
                project.manifest,
                relative,
                project.grants,
                datetime.now(UTC),
                session_id,
                self.node,
                "think",
            )
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc
        if not decision.allowed:
            code = decision.code.value if decision.code is not None else "brain.policy.excluded"
            raise ConnectorError(code, decision.reason or "source access denied", status=403)
        return decision

    def _snapshot(self, project: _Project, relative: str) -> Any:
        _assert_no_reparse(Path(project.manifest.root), relative, self.brain.normalize_relative)
        try:
            self.source_body_reads += 1
            return self.brain.read_snapshot(project.manifest.root, relative, max_bytes=MAX_WRITE_BYTES)
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc

    @staticmethod
    def _snapshot_result(project_id: str, snapshot: Any, access: Any, *, text: str | None = None) -> dict[str, Any]:
        result = {
            "project_id": project_id,
            "relative_path": snapshot.relative_path,
            "content_sha256": snapshot.content_sha256,
            "size": snapshot.size,
            "mtime_ns": snapshot.mtime_ns,
            "freshness": "current_pc",
            "observed_at": datetime.now(UTC).isoformat(),
            "access_basis": access.grant_sha256 or access.access,
        }
        if text is not None:
            result["text"] = text
        return result

    def read_context(self, body: Any) -> dict[str, Any]:
        data = _exact_object(body, {"project_id", "relative_path", "session_id"})
        project = self._refresh_registry_catalog(self._project(data["project_id"]))
        relative = _text(data["relative_path"], "relative_path")
        session_id = _text(data["session_id"], "session_id")
        access = self._source_access(project, relative, session_id)
        snapshot = self._snapshot(project, relative)
        return self._snapshot_result(project.project_id, snapshot, access, text=snapshot.text)


    def _maybe_refresh_folder_catalog(self, project: _Project) -> _Project:
        """Host-configured folder scan only; callers cannot supply PC roots."""
        config = project.folder_catalog
        if not config or not config.get("enabled"):
            return project
        from gremlin_town.mcp.local_context_catalog import (
            FolderCatalogLimits,
            refresh_folder_catalog,
        )

        scan_root = config.get("scan_root", ".")
        if not isinstance(scan_root, str) or not scan_root:
            raise ConnectorError("CONFIGURATION_INVALID", "folder_catalog.scan_root must be a non-empty string")
        extensions = config.get("extensions", [".md"])
        if not isinstance(extensions, list) or not all(isinstance(item, str) for item in extensions):
            raise ConnectorError("CONFIGURATION_INVALID", "folder_catalog.extensions must be a string list")
        try:
            limits = FolderCatalogLimits.from_mapping(config.get("limits"))
            scanned = refresh_folder_catalog(
                vault_root=Path(project.manifest.root),
                catalog_path=project.retrieval_catalog,
                owner_vault=project.project_id,
                scan_root=scan_root,
                limits=limits,
                extensions=extensions,
            )
            export_scanned = config.get("export_scanned_files", False)
            if type(export_scanned) is not bool:
                raise ConnectorError("CONFIGURATION_INVALID", "export_scanned_files must be boolean")
            if export_scanned:
                # Explicit host enrollment of this bounded folder, not caller
                # authority. Only this scan's checked Markdown paths are visible.
                from brain_core.contracts import ExportSpec, SearchSpec
                paths = tuple(path for path, _ in scanned.path_hashes)
                manifest = replace(project.manifest,
                    exports=(ExportSpec("host-enrolled-folder", paths, limits.max_file_bytes),),
                    search=SearchSpec(paths))
                return replace(project, manifest=manifest)
            return project
        except ConnectorError:
            raise
        except ValueError as exc:
            raise ConnectorError("CONFIGURATION_INVALID", str(exc)) from exc
        except OSError as exc:
            raise ConnectorError("PATH_MISSING", "folder catalog scan failed", status=404) from exc

    def _refresh_registry_catalog(self, project: _Project) -> _Project:
        """ROA-72 ("Exports + notes"): a registry vault serves the Markdown files its declared
        exports name, listed afresh on each read into a catalog in the connector's state folder,
        never in the vault. The files found become the vault's exact exports for this request."""

        enrollment = project.registry
        if enrollment is None:
            return project
        from gremlin_town.mcp.local_context_catalog import FolderCatalogLimits, refresh_exports_catalog

        try:
            limits = FolderCatalogLimits.from_mapping(enrollment.limits)
            scanned = refresh_exports_catalog(
                vault_root=Path(project.manifest.root),
                catalog_path=project.retrieval_catalog,
                owner_vault=project.project_id,
                exports=enrollment.exports,
                limits=limits,
            )
        except (TypeError, ValueError) as exc:
            raise ConnectorError("CONFIGURATION_INVALID", str(exc)) from exc
        except OSError as exc:
            raise ConnectorError("PATH_MISSING", "exports catalog scan failed", status=404) from exc
        paths = tuple(path for path, _ in scanned.path_hashes)
        if not paths:
            return project
        from brain_core.contracts import ExportSpec, SearchSpec

        manifest = replace(
            project.manifest,
            exports=(ExportSpec("declared-exports", paths, limits.max_file_bytes),),
            search=SearchSpec(paths),
        )
        return replace(project, manifest=manifest)

    def retrieve_context(self, body: Any) -> dict[str, Any]:
        data = _exact_object(
            body,
            {"project_id", "intent", "session_id"},
            {"max_sources", "max_source_tokens"},
        )
        project = self._project(data["project_id"])
        intent = _text(data["intent"], "intent")
        session_id = _text(data["session_id"], "session_id")
        max_sources = data.get("max_sources", 12)
        max_source_tokens = data.get("max_source_tokens", 12_000)
        if type(max_sources) is not int or not 1 <= max_sources <= 100:
            raise ConnectorError("INVALID_REQUEST", "max_sources must be an integer from 1 through 100", status=422)
        if type(max_source_tokens) is not int or max_source_tokens < 1:
            raise ConnectorError("INVALID_REQUEST", "max_source_tokens must be a positive integer", status=422)
        per_source_tokens = min(1_500, max(1, max_source_tokens // max_sources))
        try:
            project = self._refresh_registry_catalog(self._maybe_refresh_folder_catalog(project))
            catalog = self.brain.parse_catalog(
                self.brain.read_local_text(project.retrieval_catalog, max_bytes=5_000_000),
                str(project.retrieval_catalog),
            )
            for record in catalog.evidence:
                _assert_no_reparse(
                    Path(project.manifest.root),
                    record.relative_path,
                    self.brain.normalize_relative,
                )
            result = self.brain.retrieve(
                project.retrieval_catalog,
                (project.manifest,),
                intent,
                project.grants,
                now=datetime.now(UTC),
                session_id=session_id,
                node=self.node,
                mode="think",
                result_limit=max_sources,
                per_source_tokens=per_source_tokens,
            )
        except ConnectorError:
            raise
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc
        return {
            "project_id": project.project_id,
            "repository_version": catalog.repository_version,
            "sources": [asdict(item) for item in result.sources],
            "relations": [asdict(item) for item in result.relations],
            "ingested_ids": list(result.ingested_ids),
            "exclusions": [asdict(item) for item in result.exclusions],
            "result_sha256": result.result_sha256,
            "freshness": "current_pc",
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def write_file(self, body: Any) -> dict[str, Any]:
        data = _exact_object(
            body,
            {"project_id", "relative_path", "session_id", "expected_sha256", "text"},
        )
        project = self._project(data["project_id"])
        relative = self.brain.normalize_relative(_text(data["relative_path"], "relative_path"))
        if relative not in project.write_paths:
            raise ConnectorError("WRITE_NOT_ENROLLED", "path is not enrolled for write", status=403)
        expected = _text(data["expected_sha256"], "expected_sha256")
        if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
            raise ConnectorError("INVALID_REQUEST", "expected_sha256 must be lowercase SHA-256", status=422)
        text = data["text"]
        if not isinstance(text, str):
            raise ConnectorError("INVALID_REQUEST", "text must be a string", status=422)
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES or b"\x00" in encoded:
            raise ConnectorError("INVALID_REQUEST", "text is oversized or binary", status=422)
        access = self._source_access(project, relative, _text(data["session_id"], "session_id"))
        destination, _ = _assert_no_reparse(
            Path(project.manifest.root), relative, self.brain.normalize_relative
        )
        with self._write_lock:
            before = self._snapshot(project, relative)
            if before.content_sha256 != expected:
                raise ConnectorError(
                    "VERSION_CONFLICT",
                    "file changed since the caller observed it",
                    status=409,
                    details={"current_sha256": before.content_sha256},
                )
            temporary: Path | None = None
            try:
                descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Re-observe immediately before replacement.  The connector
                # serializes its own writers; an external-editor race remains
                # a deployment concern and is never reported as a remote save.
                current = self._snapshot(project, relative)
                if current.content_sha256 != expected:
                    raise ConnectorError(
                        "VERSION_CONFLICT",
                        "file changed during compare-before-write",
                        status=409,
                        details={"current_sha256": current.content_sha256},
                    )
                os.replace(temporary, destination)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            after = self._snapshot(project, relative)
        return {
            "project_id": project.project_id,
            "relative_path": after.relative_path,
            "before_sha256": before.content_sha256,
            "content_sha256": after.content_sha256,
            "size": after.size,
            "mtime_ns": after.mtime_ns,
            "freshness": "current_pc",
            "observed_at": datetime.now(UTC).isoformat(),
            "access_basis": access.grant_sha256 or access.access,
            "changed": before.content_sha256 != after.content_sha256,
        }

    def _notes_root(self, project: _Project, *, create: bool) -> Path | None:
        """Where a project's notes are. A registry vault's is its ``ops/sessions/``, made by the
        first save (``create``); before that a read finds ``None``. A link in its place is refused."""

        if project.registry is None:
            return project.continuity_root
        candidate = Path(project.manifest.root)
        if _is_reparse(candidate):
            raise ConnectorError("REPARSE_REFUSED", "enrolled root is a reparse point", status=403)
        for part in NOTES_FOLDER.parts:
            candidate = candidate / part
            if not candidate.exists() and not candidate.is_symlink():
                if not create:
                    return None
                try:
                    candidate.mkdir(exist_ok=True)
                except OSError as exc:
                    raise ConnectorError("PATH_MISSING", "the vault's notes folder cannot be made", status=404) from exc
            if _is_reparse(candidate) or not candidate.is_dir():
                raise ConnectorError(
                    "REPARSE_REFUSED",
                    "a vault's notes folder may not be a link or a file",
                    status=403,
                    details={"relative_path": NOTES_FOLDER.as_posix()},
                )
        return candidate

    @staticmethod
    def _continuity_evidence(project: _Project, session_id: str, command: Mapping[str, Any], brain: SimpleNamespace) -> Any:
        return brain.Evidence(
            source_type="mcp_request",
            locator=f"connector://{project.project_id}/continuity/{session_id}",
            content_sha256=_sha256_bytes(_canonical_json(command)),
            session_id=session_id,
            actor="brain-local-connector",
        )

    @staticmethod
    def _continuity_write_result(project: _Project, operation_id: str, current: Any) -> dict[str, Any]:
        receipt = current.receipt
        receipt_basis = (
            asdict(receipt)
            if receipt is not None
            else {
                "record_id": current.record.id,
                "record_sha256": current.record.sha256,
                "updated_at": current.record.updated_at.isoformat(),
            }
        )
        return {
            "record_id": current.record.id,
            "session_id": str(current.record.data["session_id"]),
            "state": current.record.state,
            "changed": bool(current.changed),
            "updated_at": current.record.updated_at.isoformat(),
            "receipt": {
                "operation_id": operation_id,
                "status": "accepted",
                "owner_locator": f"vault://{project.project_id}",
                "record_id": current.record.id,
                "version": receipt.relative_path if receipt is not None else current.record.updated_at.isoformat(),
                "content_sha256": receipt.content_sha256 if receipt is not None else current.record.sha256,
                "receipt_sha256": _sha256_bytes(_canonical_json(receipt_basis)),
                "remote_observed": False,
                "retryable": False,
                "refusal_code": None,
            },
        }

    @staticmethod
    def _write_operation_document(path: Path, document: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(_canonical_json(document))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            with path.open("r+b") as accepted:
                os.fsync(accepted.fileno())
            if os.name != "nt":
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _canonical_continuity_effect(
        self,
        project: _Project,
        operation_id: str,
        operation_kind: str,
        command: Mapping[str, Any],
        command_sha256: str,
    ) -> dict[str, Any] | None:
        session_id = command.get("session_id")
        if not isinstance(session_id, str):
            return None
        notes = self._notes_root(project, create=False)
        if notes is None:
            return None
        try:
            records = self.brain.RecordStore(notes).current(kind="continuity")
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc
        matches = [
            record
            for record in records
            if record.data.get("session_id") == session_id
            and any(item.content_sha256 == command_sha256 for item in record.evidence)
        ]
        if not matches:
            return None
        record = max(matches, key=lambda item: (item.updated_at, item.id))
        if operation_kind == "continuity.start" and record.data.get("intent") != command.get("intent"):
            return None
        if operation_kind == "continuity.close":
            expected_state = "recovered" if command.get("recovered") is True else "closed"
            if record.state != expected_state:
                return None
        current = SimpleNamespace(record=record, receipt=None, changed=True)
        result = self._continuity_write_result(project, operation_id, current)
        result["reconciled_from_pending"] = True
        return result

    def _reconcile_continuity_operation(
        self,
        project: _Project,
        operation_id: str,
        operation_kind: str,
        command: Mapping[str, Any],
        effect: Any,
    ) -> dict[str, Any]:
        operation_id = _text(operation_id, "operation_id")
        operation_root = project.operations_root or project.continuity_root / ".connector-operations"
        identity = _sha256_text(operation_id)
        receipt_path = operation_root / f"{identity}.json"
        lock_path = operation_root / f"{identity}.lock"
        input_sha256 = _sha256_bytes(
            _canonical_json(
                {"operation_id": operation_id, "operation_kind": operation_kind, "command": dict(command)}
            )
        )
        command_sha256 = _sha256_bytes(_canonical_json(command))
        with _exclusive_file_lock(lock_path):
            if receipt_path.is_file():
                try:
                    prior = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ConnectorError("OPERATION_RECEIPT_CORRUPT", "operation receipt is unreadable", status=500) from exc
                if (
                    prior.get("schema") != "gt.brain-local-operation/v1"
                    or prior.get("operation_id") != operation_id
                    or prior.get("operation_kind") != operation_kind
                    or prior.get("input_sha256") != input_sha256
                ):
                    raise ConnectorError(
                        "OPERATION_INPUT_CONFLICT",
                        "operation ID was reused with different input",
                        status=409,
                    )
                state = prior.get("state")
                if state == "accepted":
                    result = prior.get("result")
                    if not isinstance(result, dict):
                        raise ConnectorError("OPERATION_RECEIPT_CORRUPT", "operation receipt result is invalid", status=500)
                    return result
                if state == "failed":
                    error = prior.get("error")
                    if not isinstance(error, dict):
                        raise ConnectorError("OPERATION_RECEIPT_CORRUPT", "operation error receipt is invalid", status=500)
                    raise ConnectorError(
                        str(error.get("code", "OPERATION_FAILED")),
                        str(error.get("message", "continuity operation failed")),
                        status=int(error.get("status", 409)),
                    )
                if state != "pending" or prior.get("command_sha256") != command_sha256:
                    raise ConnectorError("OPERATION_RECEIPT_CORRUPT", "operation receipt state is invalid", status=500)
                reconciled = self._canonical_continuity_effect(
                    project,
                    operation_id,
                    operation_kind,
                    command,
                    command_sha256,
                )
                if reconciled is None:
                    raise ConnectorError(
                        "OPERATION_OUTCOME_UNCERTAIN",
                        "pending operation has no canonical acceptance evidence",
                        status=409,
                        details={"operation_id": operation_id},
                    )
                accepted = dict(prior)
                accepted.update({"state": "accepted", "result": reconciled})
                self._write_operation_document(receipt_path, accepted)
                return reconciled
            pending = {
                "schema": "gt.brain-local-operation/v1",
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "input_sha256": input_sha256,
                "command_sha256": command_sha256,
                "state": "pending",
                "result": None,
            }
            self._write_operation_document(receipt_path, pending)
            if self._operation_fault is not None:
                self._operation_fault("after_pending", operation_id)
            try:
                result = effect()
            except ConnectorError as exc:
                failed = dict(pending)
                failed.update(
                    {
                        "state": "failed",
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            "status": exc.status,
                        },
                    }
                )
                self._write_operation_document(receipt_path, failed)
                raise
            except Exception as exc:
                converted = _brain_error(exc, self.brain)
                failed = dict(pending)
                failed.update(
                    {
                        "state": "failed",
                        "error": {
                            "code": converted.code,
                            "message": str(converted),
                            "status": converted.status,
                        },
                    }
                )
                self._write_operation_document(receipt_path, failed)
                raise converted from exc
            if self._operation_fault is not None:
                self._operation_fault("after_effect", operation_id)
            accepted = dict(pending)
            accepted.update({"state": "accepted", "result": result})
            self._write_operation_document(receipt_path, accepted)
            return result

    def start_continuity(self, body: Any) -> dict[str, Any]:
        data = _exact_object(
            body,
            {"project_id", "operation_id", "session_id", "intent", "source_references", "next_step"},
            {"task_id"},
        )
        project = self._project(data["project_id"])
        session_id = _text(data["session_id"], "session_id")
        sources = data["source_references"]
        if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
            raise ConnectorError("INVALID_REQUEST", "source_references must be a string list", status=422)
        # N12b: a project note may name its task; sent only when it does, so an older hub's
        # start and this connector's own record shape stay exactly as they were.
        linked = {} if "task_id" not in data else {"task_id": _text(data["task_id"], "task_id")}

        def effect() -> dict[str, Any]:
            current = self.brain.start_continuity(
                self.brain.RecordStore(self._notes_root(project, create=True)),
                session_id=session_id,
                intent=_text(data["intent"], "intent"),
                sources=sources,
                evidence=self._continuity_evidence(project, session_id, data, self.brain),
                now=datetime.now(UTC),
                next_step=_text(data["next_step"], "next_step"),
                **linked,
            )
            return self._continuity_write_result(project, str(data["operation_id"]), current)

        return self._reconcile_continuity_operation(
            project, data["operation_id"], "continuity.start", data, effect
        )

    def append_continuity(self, body: Any) -> dict[str, Any]:
        data = _exact_object(
            body,
            {"project_id", "operation_id", "session_id", "event", "value"},
        )
        project = self._project(data["project_id"])
        session_id = _text(data["session_id"], "session_id")

        def effect() -> dict[str, Any]:
            current = self.brain.append_event(
                self.brain.RecordStore(self._notes_root(project, create=True)),
                session_id=session_id,
                event=_text(data["event"], "event"),
                value=_text(data["value"], "value"),
                evidence=self._continuity_evidence(project, session_id, data, self.brain),
                now=datetime.now(UTC),
            )
            return self._continuity_write_result(project, str(data["operation_id"]), current)

        return self._reconcile_continuity_operation(
            project, data["operation_id"], "continuity.append", data, effect
        )

    def recover_continuity(self, body: Any) -> dict[str, Any]:
        data = _exact_object(body, {"project_id"}, {"max_tokens", "session_id"})
        project = self._project(data["project_id"])
        max_tokens = data.get("max_tokens", 2_500)
        if type(max_tokens) is not int:
            raise ConnectorError("INVALID_REQUEST", "max_tokens must be an integer", status=422)
        session_id = data.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ConnectorError("INVALID_REQUEST", "session_id must be a string", status=422)
        notes = self._notes_root(project, create=False)
        try:
            if notes is None:
                raise self.brain.BrainError(
                    self.brain.ErrorCode.CONTINUITY_EMPTY, "no note has been saved for this project yet"
                )
            return self.brain.recover_continuity(
                self.brain.RecordStore(notes),
                session_id=session_id, max_tokens=max_tokens,
            )
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc

    def list_continuity(self, body: Any) -> dict[str, Any]:
        data = _exact_object(body, {"project_id"}, {"limit"})
        project = self._project(data["project_id"])
        limit = data.get("limit", 20)
        if type(limit) is not int:
            raise ConnectorError("INVALID_REQUEST", "limit must be an integer", status=422)
        folder = self._notes_root(project, create=False)
        if folder is None:
            return {"items": []}  # a registry vault with no notes yet: nothing is created to say so
        try:
            notes = self.brain.list_continuity(
                self.brain.RecordStore(folder), limit=limit
            )
        except Exception as exc:
            raise _brain_error(exc, self.brain) from exc
        return {
            "items": [
                {
                    "record_id": note["record_id"],
                    "session_id": note["session_id"],
                    "intent": note["intent"],
                    "next_step": note["next_step"],
                    "state": note["state"],
                    "updated_at": note["updated_at"].isoformat(),
                    "task_id": note.get("task_id"),
                }
                for note in notes
            ]
        }

    def close_continuity(self, body: Any) -> dict[str, Any]:
        data = _exact_object(
            body,
            {"project_id", "operation_id", "session_id", "recovered"},
        )
        project = self._project(data["project_id"])
        session_id = _text(data["session_id"], "session_id")
        if type(data["recovered"]) is not bool:
            raise ConnectorError("INVALID_REQUEST", "recovered must be a boolean", status=422)

        def effect() -> dict[str, Any]:
            current = self.brain.close_continuity(
                self.brain.RecordStore(self._notes_root(project, create=True)),
                session_id=session_id,
                evidence=self._continuity_evidence(project, session_id, data, self.brain),
                now=datetime.now(UTC),
                recovered=data["recovered"],
            )
            return self._continuity_write_result(project, str(data["operation_id"]), current)

        return self._reconcile_continuity_operation(
            project, data["operation_id"], "continuity.close", data, effect
        )

    def _compass_from_manifest(self, vault_root: Path) -> tuple[str | None, str | None]:
        """The Compass text a Vault OS manifest exports, or ``None`` and why not."""

        brain = self.brain
        try:
            _assert_no_reparse(vault_root, "ops/vault-manifest.md", brain.normalize_relative)
            manifest = brain.read_snapshot(vault_root, "ops/vault-manifest.md", max_bytes=MAX_MANIFEST_BYTES)
        except (ConnectorError, brain.BrainError, OSError):
            return None, "manifest_unreadable"
        lines = manifest.text.splitlines()
        end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
        if not lines or lines[0].strip() != "---" or end is None:
            return None, "manifest_invalid"
        try:
            frontmatter = brain.parse_yaml("\n".join(lines[1:end]) + "\n", "ops/vault-manifest.md")
        except brain.BrainError:
            return None, "manifest_invalid"
        # The same gate Vault OS uses: the manifest must describe this folder.
        root_path = frontmatter.get("root-path")
        if not isinstance(root_path, str) or os.path.normcase(str(Path(root_path).resolve())) != os.path.normcase(
            str(vault_root.resolve())
        ):
            return None, "root_path_mismatch"
        exports = frontmatter.get("exports")
        compass = exports.get("compass") if isinstance(exports, Mapping) else None
        if not isinstance(compass, str) or not compass.strip():
            return None, "no_compass_export"
        try:
            relative = brain.normalize_relative(compass.strip())
            _assert_no_reparse(vault_root, relative, brain.normalize_relative)
            snapshot = brain.read_snapshot(vault_root, relative, max_bytes=MAX_COMPASS_BYTES)
        except (ConnectorError, brain.BrainError, OSError):
            return None, "compass_unreadable"
        return snapshot.text, None

    def read_compass_focus(self, body: Any) -> dict[str, Any]:
        """Only the Focus section of the Compass of the vault that holds an enrolled project.

        That vault is the nearest folder, from the project's enrolled root up to three parent
        folders, with a Vault OS manifest (``ops/vault-manifest.md``) whose ``root-path`` is
        that folder. Its ``exports.compass`` names the Compass. Nothing else from the vault is
        read or returned. A vault with no Compass answers ``compass: false`` with the reason,
        so the hub leaves the Focus line out (b-project-sitreps.md).
        """

        data = _exact_object(body, {"project_id"})
        project = self._project(data["project_id"])
        try:
            from brain_core.sitreps import focus_section
        except ImportError as exc:
            raise ConnectorError(
                "BRAIN_CORE_UNAVAILABLE",
                "Brain Core in the PC connector runtime cannot read a Compass Focus",
                status=503,
            ) from exc
        text: str | None = None
        reason: str | None = "no_vault_manifest"
        candidate = Path(project.manifest.root)
        for _ in range(_VAULT_SEARCH_LEVELS + 1):
            if (candidate / "ops" / "vault-manifest.md").is_file():
                text, reason = self._compass_from_manifest(candidate)
                break
            if candidate.parent == candidate:
                break
            candidate = candidate.parent
        section = None if text is None else focus_section(text)
        if section is not None:
            section = section[:MAX_FOCUS_CHARS]
        result: dict[str, Any] = {
            "project_id": project.project_id,
            "compass": text is not None,
            "focus": section,
            "focus_sha256": None if section is None else _sha256_text(section),
            "freshness": "current_pc",
            "observed_at": datetime.now(UTC).isoformat(),
        }
        if reason is not None:
            result["reason"] = reason
        return result

    def dispatch(self, route: str, body: Any) -> dict[str, Any]:
        operations = {
            "/v1/context/read": self.read_context,
            "/v1/context/retrieve": self.retrieve_context,
            "/v1/files/write": self.write_file,
            "/v1/continuity/start": self.start_continuity,
            "/v1/continuity/append": self.append_continuity,
            "/v1/continuity/recover": self.recover_continuity,
            "/v1/continuity/list": self.list_continuity,
            "/v1/continuity/close": self.close_continuity,
            "/v1/compass/focus": self.read_compass_focus,
        }
        return operations[route](body)


class _ConnectorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, runtime: LocalProjectConnectorRuntime) -> None:
        self.runtime = runtime
        super().__init__((runtime.bind_host, runtime.port), _ConnectorHandler)


class _ConnectorHandler(BaseHTTPRequestHandler):
    server: _ConnectorHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        del format, args

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = _canonical_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _reject(self, error: ConnectorError) -> None:
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
        encoded = _canonical_json({"ok": False, "error": error.as_dict()})
        self.send_response(error.status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _drain_pending_body(self) -> None:
        """Best-effort, bounded discard of a body we rejected before reading.

        ROA-34: authentication and scope are checked before the body is
        read (see ``test_authentication_and_scope_are_checked_before_http_body_read``),
        so a caller that fails that check may still have a real request
        body sitting unread on the wire. ``Connection: close`` plus an
        immediate socket close while the OS receive buffer still holds
        bytes the caller already sent makes Windows (and other stacks)
        send a reset instead of a clean close, which can destroy the
        rejection response we are about to send in the same instant -
        observed as the caller's client misreporting ACCESS_DENIED or
        UNAUTHENTICATED as PC_UNAVAILABLE about 7% of the time.

        Draining is capped by a short poll rather than the declared
        Content-Length, so a caller that only claims a body via
        Content-Length but never sends one is rejected promptly instead
        of blocked - it never turns this rejection path into the body
        read it is deliberately trying to avoid.
        """
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return
        try:
            claimed = int(raw_length)
        except ValueError:
            return
        if claimed <= 0:
            return
        try:
            remaining = min(claimed, MAX_REQUEST_BYTES)
            while remaining > 0:
                ready, _, _ = select.select([self.connection], [], [], _DRAIN_POLL_SECONDS)
                if not ready:
                    return
                chunk = self.connection.recv(min(65536, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            return

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        required_scope = _ROUTES.get(route)
        if required_scope is None:
            self._reject(ConnectorError("NOT_FOUND", "connector route not found", status=404))
            return
        body_read = False
        try:
            if not self.server.runtime.client_allowed(self.client_address[0]):
                raise ConnectorError("CLIENT_NETWORK_DENIED", "client network was rejected", status=403)
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                raise ConnectorError("UNAUTHENTICATED", "connector credential is required", status=401)
            self.server.runtime.authorize(authorization[7:], required_scope)
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ConnectorError("LENGTH_REQUIRED", "Content-Length is required", status=411)
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ConnectorError("INVALID_REQUEST", "Content-Length is invalid", status=400) from exc
            if not 0 <= length <= MAX_REQUEST_BYTES:
                raise ConnectorError("REQUEST_TOO_LARGE", "request body is too large", status=413)
            raw_body = self.rfile.read(length)
            body_read = True
            self.server.runtime.http_body_reads += 1
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ConnectorError("INVALID_JSON", "request body is not valid UTF-8 JSON", status=400) from exc
            result = self.server.runtime.dispatch(route, body)
            self._send(200, {"ok": True, "result": result})
        except ConnectorError as exc:
            if not body_read:
                self._drain_pending_body()
            self._reject(exc)
        except Exception as exc:
            if not body_read:
                self._drain_pending_body()
            self._reject(_brain_error(exc, self.server.runtime.brain))


class RunningLocalProjectConnector:
    """In-process lifecycle helper; calls still cross a real TCP/HTTP boundary."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        brain_src: str | Path | None = None,
        operation_fault: Any | None = None,
    ) -> None:
        self.runtime = LocalProjectConnectorRuntime(
            config_path,
            brain_src=brain_src,
            operation_fault=operation_fault,
        )
        self.server = _ConnectorHTTPServer(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, name="brain-local-connector", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        rendered = f"[{host}]" if ":" in host else host
        return f"http://{rendered}:{port}"

    def start(self) -> "RunningLocalProjectConnector":
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()

    def __enter__(self) -> "RunningLocalProjectConnector":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.stop()


class LocalProjectConnectorClient:
    """Hosted-side HTTP client with explicit PC-off behavior and no cache."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 3.0) -> None:
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise ValueError("base_url must be a valid HTTP(S) origin") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("base_url must be an HTTP(S) origin without credentials or path")
        if parsed.scheme == "http":
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError as exc:
                raise ValueError("plaintext HTTP is permitted only for an IP-literal loopback test origin") from exc
            if not address.is_loopback:
                raise ValueError("plaintext HTTP is permitted only for loopback tests")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be non-empty")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        netloc = parsed.hostname
        if ":" in netloc:
            netloc = f"[{netloc}]"
        if port is not None:
            netloc = f"{netloc}:{port}"
        self.base_url = urlunsplit((parsed.scheme, netloc, "", "", ""))
        self.token = token
        self.timeout = float(timeout)
        self._opener = build_opener(_NoRedirect())

    def call(self, route: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        encoded = _canonical_json(dict(payload))
        request = Request(
            f"{self.base_url}{route}",
            data=encoded,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = int(response.status)
                raw = _read_bounded_response(response)
        except HTTPError as exc:
            status = int(exc.code)
            if 300 <= status < 400:
                raise ConnectorError("REDIRECT_REFUSED", "connector redirect was refused", status=502) from exc
            if 500 <= status < 600:
                raise ConnectorUnavailable(
                    "PC-local connector gateway or server is unavailable"
                ) from exc
            raw = _read_bounded_response(exc)
        except (URLError, TimeoutError, OSError, socket.timeout) as exc:
            raise ConnectorUnavailable() from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("INVALID_RESPONSE", "connector returned invalid JSON", status=502) from exc
        if not 200 <= status < 300 or not isinstance(data, dict) or data.get("ok") is not True:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            raise ConnectorError(
                str(error.get("code", "CONNECTOR_REJECTED")),
                str(error.get("message", "connector rejected the request")),
                status=status,
                details=error.get("details") if isinstance(error.get("details"), dict) else None,
            )
        result = data.get("result")
        if not isinstance(result, dict):
            raise ConnectorError("INVALID_RESPONSE", "connector result is not an object", status=502)
        return result

    def read_context(self, project_id: str, relative_path: str, session_id: str) -> dict[str, Any]:
        return self.call(
            "/v1/context/read",
            {"project_id": project_id, "relative_path": relative_path, "session_id": session_id},
        )

    def retrieve_context(
        self,
        project_id: str,
        intent: str,
        session_id: str,
        *,
        max_sources: int = 12,
        max_source_tokens: int = 12_000,
    ) -> dict[str, Any]:
        return self.call(
            "/v1/context/retrieve",
            {
                "project_id": project_id,
                "intent": intent,
                "session_id": session_id,
                "max_sources": max_sources,
                "max_source_tokens": max_source_tokens,
            },
        )

    def write_file(
        self,
        project_id: str,
        relative_path: str,
        session_id: str,
        expected_sha256: str,
        text: str,
    ) -> dict[str, Any]:
        return self.call(
            "/v1/files/write",
            {
                "project_id": project_id,
                "relative_path": relative_path,
                "session_id": session_id,
                "expected_sha256": expected_sha256,
                "text": text,
            },
        )

    def start_continuity(self, project_id: str, request: ContinuityStartRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "project_id": project_id,
            "operation_id": request.operation_id,
            "session_id": request.session_id,
            "intent": request.intent,
            "source_references": list(request.source_references),
            "next_step": request.next_step,
        }
        if request.task_id is not None:
            # Only when set: a PC connector older than this release refuses the unknown field,
            # so a task-linked project note needs the connector release too (N12b).
            body["task_id"] = request.task_id
        return self.call("/v1/continuity/start", body)

    def append_continuity(self, project_id: str, request: ContinuityAppendRequest) -> dict[str, Any]:
        return self.call(
            "/v1/continuity/append",
            {
                "project_id": project_id,
                "operation_id": request.operation_id,
                "session_id": request.session_id,
                "event": request.event,
                "value": request.value,
            },
        )

    def recover_continuity(
        self, project_id: str, *, session_id: str | None = None, max_tokens: int = 2_500
    ) -> dict[str, Any]:
        return self.call(
            "/v1/continuity/recover",
            {"project_id": project_id, "session_id": session_id, "max_tokens": max_tokens},
        )

    def list_continuity(self, project_id: str, *, limit: int = 20) -> dict[str, Any]:
        return self.call(
            "/v1/continuity/list",
            {"project_id": project_id, "limit": limit},
        )

    def close_continuity(self, project_id: str, request: ContinuityCloseRequest) -> dict[str, Any]:
        return self.call(
            "/v1/continuity/close",
            {
                "project_id": project_id,
                "operation_id": request.operation_id,
                "session_id": request.session_id,
                "recovered": request.recovered,
            },
        )

    def read_compass_focus(self, project_id: str) -> dict[str, Any]:
        """The Focus section of the Compass of the vault that holds ``project_id`` (sitreps)."""

        return self.call("/v1/compass/focus", {"project_id": project_id})


@dataclass(frozen=True, slots=True)
class LocalProjectContextCapability:
    """Trusted host-issued project/session binding carried by Lane A."""

    project_id: str
    session_id: str

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.session_id.strip():
            raise ValueError("local connector capability fields must be non-empty")


class LocalConnectorContextProvider:
    """Lane A ``ContextProvider`` adapter over the scoped network connector."""

    def __init__(self, client: LocalProjectConnectorClient) -> None:
        self.client = client

    async def retrieve(
        self, authorization: OwnerAuthorization, query: ContextQuery
    ) -> ContextResult:
        capability = authorization.capability
        if (
            authorization.operation is not BrainOperation.CONTEXT_RETRIEVE
            or authorization.access != "read"
            or authorization.selector.kind != "project"
            or not isinstance(capability, LocalProjectContextCapability)
            or authorization.selector.project_id != capability.project_id
            or authorization.owner_locator != f"vault://{capability.project_id}"
        ):
            raise BrainRuntimeError(
                "brain.policy.excluded",
                "trusted local-project authorization does not match the context request",
            )
        try:
            value = await asyncio.to_thread(
                self.client.retrieve_context,
                capability.project_id,
                query.intent,
                capability.session_id,
                max_sources=query.max_sources,
                max_source_tokens=query.max_source_tokens,
            )
            sources = tuple(
                SourceEvidence(
                    evidence_id=str(item["evidence_id"]),
                    owner_locator=f"vault://{item['owner_vault']}",
                    source_locator=f"vault://{item['owner_vault']}/{item['relative_path']}",
                    source_version=str(item["repository_version"]),
                    content_sha256=str(item["content_sha256"]),
                    access_basis=str(item["access_basis"]),
                    retrieval_method=str(item["retrieval_method"]),
                    locator=str(item["locator"]),
                    source_tokens=int(item["source_tokens"]),
                    excerpt=str(item["excerpt"]),
                )
                for item in value["sources"]
            )
            exclusions = tuple(
                RetrievalExclusion(str(item["code"]), str(item["reason"]), int(item["count"]))
                for item in value["exclusions"]
            )
            return ContextResult(
                sources=sources,
                exclusions=exclusions,
                result_sha256=str(value["result_sha256"]),
                observed_at=datetime.fromisoformat(str(value["observed_at"])),
                freshness="current",
            )
        except ConnectorUnavailable as exc:
            raise BrainRuntimeError("brain.capability.unavailable", str(exc), retryable=True) from exc
        except ConnectorError as exc:
            raise BrainRuntimeError(exc.code, str(exc), retryable=exc.status >= 500) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise BrainRuntimeError("brain.contract.invalid", "local connector returned an invalid context result") from exc

    async def aclose(self) -> None:
        """The standard-library client holds no persistent connection."""


class LocalConnectorContinuityProvider:
    """Lane A ``ContinuityProvider`` over project-scoped connector stores."""

    def __init__(self, client: LocalProjectConnectorClient) -> None:
        self.client = client

    @staticmethod
    def _capability(
        authorization: OwnerAuthorization,
        operation: BrainOperation,
        access: str,
    ) -> LocalProjectContextCapability:
        capability = authorization.capability
        if (
            authorization.operation is not operation
            or authorization.access != access
            or authorization.selector.kind != "project"
            or not isinstance(capability, LocalProjectContextCapability)
            or authorization.selector.project_id != capability.project_id
            or authorization.owner_locator != f"vault://{capability.project_id}"
        ):
            raise BrainRuntimeError(
                "brain.policy.excluded",
                "trusted local-project authorization does not match the continuity request",
            )
        return capability

    @staticmethod
    def _receipt(value: Mapping[str, Any]) -> WriteReceipt:
        return WriteReceipt(
            operation_id=str(value["operation_id"]),
            status=str(value["status"]),  # type: ignore[arg-type]
            owner_locator=str(value["owner_locator"]),
            record_id=None if value.get("record_id") is None else str(value["record_id"]),
            version=None if value.get("version") is None else str(value["version"]),
            content_sha256=(
                None if value.get("content_sha256") is None else str(value["content_sha256"])
            ),
            receipt_sha256=(
                None if value.get("receipt_sha256") is None else str(value["receipt_sha256"])
            ),
            remote_observed=bool(value["remote_observed"]),
            retryable=bool(value.get("retryable", False)),
            refusal_code=(
                None if value.get("refusal_code") is None else str(value["refusal_code"])
            ),
        )

    @classmethod
    def _write_result(cls, value: Mapping[str, Any]) -> ContinuityWriteResult:
        receipt = value.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("continuity receipt is missing")
        return ContinuityWriteResult(
            record_id=str(value["record_id"]),
            session_id=str(value["session_id"]),
            state=str(value["state"]),  # type: ignore[arg-type]
            changed=bool(value["changed"]),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            receipt=cls._receipt(receipt),
        )

    @staticmethod
    def _runtime_error(error: Exception) -> BrainRuntimeError:
        if isinstance(error, ConnectorUnavailable):
            return BrainRuntimeError("brain.capability.unavailable", str(error), retryable=True)
        if isinstance(error, ConnectorError):
            return BrainRuntimeError(error.code, str(error), retryable=error.status >= 500)
        return BrainRuntimeError("brain.contract.invalid", "local connector returned invalid continuity data")

    async def start(
        self, authorization: OwnerAuthorization, request: ContinuityStartRequest
    ) -> ContinuityWriteResult:
        capability = self._capability(authorization, BrainOperation.CONTINUITY_START, "write")
        try:
            value = await asyncio.to_thread(self.client.start_continuity, capability.project_id, request)
            return self._write_result(value)
        except (ConnectorError, KeyError, TypeError, ValueError) as exc:
            raise self._runtime_error(exc) from exc

    async def append(
        self, authorization: OwnerAuthorization, request: ContinuityAppendRequest
    ) -> ContinuityWriteResult:
        capability = self._capability(authorization, BrainOperation.CONTINUITY_APPEND, "write")
        try:
            value = await asyncio.to_thread(self.client.append_continuity, capability.project_id, request)
            return self._write_result(value)
        except (ConnectorError, KeyError, TypeError, ValueError) as exc:
            raise self._runtime_error(exc) from exc

    async def recover(
        self, authorization: OwnerAuthorization, request: ContinuityRecoverRequest
    ) -> ContinuityRecovery:
        capability = self._capability(authorization, BrainOperation.CONTINUITY_RECOVER, "read")
        try:
            value = await asyncio.to_thread(
                self.client.recover_continuity,
                capability.project_id,
                session_id=request.session_id,
                max_tokens=request.max_tokens,
            )
            return ContinuityRecovery(
                record_id=str(value["record_id"]),
                session_id=str(value["session_id"]),
                state=str(value["state"]),  # type: ignore[arg-type]
                intent=str(value["intent"]),
                next_step=str(value["next_step"]),
                updated_at=datetime.fromisoformat(str(value["updated_at"])),
                tokenizer=str(value["tokenizer"]),
                tokens=int(value["tokens"]),
                sources=tuple(map(str, value.get("sources", ()))),
                effects=tuple(map(str, value.get("effects", ()))),
                decisions=tuple(map(str, value.get("decisions", ()))),
                commitments=tuple(map(str, value.get("commitments", ()))),
                candidates=tuple(map(str, value.get("candidates", ()))),
                blockers=tuple(map(str, value.get("blockers", ()))),
                dropped={str(key): int(count) for key, count in value.get("dropped", {}).items()},
                task_id=None if value.get("task_id") is None else str(value["task_id"]),
            )
        except (ConnectorError, KeyError, TypeError, ValueError) as exc:
            raise self._runtime_error(exc) from exc

    async def list_open(
        self, authorization: OwnerAuthorization, request: ContinuityListRequest
    ) -> ContinuityListResult:
        capability = self._capability(authorization, BrainOperation.CONTINUITY_LIST, "read")
        try:
            value = await asyncio.to_thread(
                self.client.list_continuity, capability.project_id, limit=request.limit
            )
            items = value["items"]
            if not isinstance(items, list):
                raise ValueError("continuity list result items must be a list")
            return ContinuityListResult(
                items=tuple(
                    ContinuitySummary(
                        record_id=str(item["record_id"]),
                        session_id=str(item["session_id"]),
                        intent=str(item["intent"]),
                        next_step=str(item["next_step"]),
                        state=str(item["state"]),  # type: ignore[arg-type]
                        updated_at=datetime.fromisoformat(str(item["updated_at"])),
                        task_id=None if item.get("task_id") is None else str(item["task_id"]),
                    )
                    for item in items
                )
            )
        except (ConnectorError, KeyError, TypeError, ValueError) as exc:
            raise self._runtime_error(exc) from exc

    async def close(
        self, authorization: OwnerAuthorization, request: ContinuityCloseRequest
    ) -> ContinuityWriteResult:
        capability = self._capability(authorization, BrainOperation.CONTINUITY_CLOSE, "write")
        try:
            value = await asyncio.to_thread(self.client.close_continuity, capability.project_id, request)
            return self._write_result(value)
        except (ConnectorError, KeyError, TypeError, ValueError) as exc:
            raise self._runtime_error(exc) from exc

    async def aclose(self) -> None:
        """The standard-library client holds no persistent connection."""



def _serve(config: Path, ready_file: Path, brain_src: Path | None) -> int:
    connector = RunningLocalProjectConnector(config, brain_src=brain_src)
    connector.start()
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.write_text(
        json.dumps({"base_url": connector.base_url, "pid": os.getpid()}, sort_keys=True),
        encoding="utf-8",
    )
    try:
        connector.thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        connector.stop()
        ready_file.unlink(missing_ok=True)
    return 0


def _list_projects(config: Path, brain_src: Path | None) -> int:
    """Print the projects a config enrolls, and the registry entries it leaves out, as JSON,
    without starting a server (ROA-72: the release builds the hub's project owners from it)."""

    runtime = LocalProjectConnectorRuntime(config, brain_src=brain_src)
    print(json.dumps(runtime.enrollment(), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the scoped PC-local Brain Core connector")
    parser.add_argument("command", choices=["serve", "list-projects"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--brain-src", type=Path)
    args = parser.parse_args(argv)
    if args.command == "list-projects":
        return _list_projects(args.config, args.brain_src)
    if args.ready_file is None:
        parser.error("serve needs --ready-file")
    return _serve(args.config, args.ready_file, args.brain_src)


if __name__ == "__main__":
    raise SystemExit(main())
