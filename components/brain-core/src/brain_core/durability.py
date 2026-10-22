"""One strict, owner-routed writer for durable decisions."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from brain_core.contracts import canonical_json
from brain_core.errors import BrainError, ErrorCode
from brain_core.ownership import OwnershipCatalogError, OwnershipRefusal, parse_ownership_catalog, resolve_ownership
from brain_core.records import Evidence, Record, RecordStore, WriteReceipt, evidence_from_mapping, new_record_id, parse_record, record_from_mapping

REQUEST_SCHEMA = "brain.durability-request/v1"
RECEIPT_SCHEMA = "brain.durability-receipt/v1"
WRITER_ID = "brain-core-shared-durability-writer/v1"
# Demonstration policy only: it contains no personal vault policy or host paths.
# Callers must explicitly bind owner roots before any durable write can occur.
CATALOG_PATH = str(Path(__file__).parent / "data" / "ownership-catalog.json")
CATALOG_BYTES = 5_292
CATALOG_SHA256 = "E3952E976A7562DFB71E84B2EE96A2E9298569256599FB1F04525CEFB500E60E"
CATALOG_SCHEMA = "brain.ownership-catalog/v1"
CATALOG_ID = "showcase-synthetic-ownership-v1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ORIGIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SCOPES = frozenset({"personal", "cross-project", "system", "fleet", "project"})
_REQUEST_FIELDS = frozenset(
    {
        "schema", "verb", "operation", "origin_id", "choice", "title", "because",
        "forecloses", "governing_scope", "encoded_in", "completeness_check",
        "explicit_scope", "default_scope", "source_context_sha256", "session_delta_sha256",
        "accepted_at", "ownership_catalog", "expected_owner_locator",
        "expected_writer_generation", "observed_preimage_sha256",
    }
)
_CATALOG_FIELDS = frozenset({"path", "bytes", "sha256", "schema", "catalog_id"})
_SCOPE_FIELDS = frozenset({"kind", "project_id"})
_ROOT_BINDINGS: dict[str, Path] = {}
_PROCESS_MUTEX_GUARD = threading.Lock()
_PROCESS_MUTEXES: dict[str, Any] = {}


def _contract(message: str, *, field: str, actual: object = None, refusal_code: str = "request_invalid") -> None:
    raise BrainError(
        ErrorCode.CONTRACT_INVALID,
        message,
        details={"field": field, "actual": actual, "refusal_code": refusal_code},
    )


def _exact_mapping(value: object, fields: frozenset[str], field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _contract(f"{field} must be an object", field=field, actual=type(value).__name__)
    data = dict(value)
    if frozenset(data) != fields:
        _contract(
            f"{field} must contain exactly the v1 fields",
            field=field,
            actual={"missing": sorted(fields - frozenset(data)), "unknown": sorted(frozenset(data) - fields)},
        )
    return data


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        _contract(f"{field} must be a non-empty string", field=field, actual=value)
    trimmed = value.strip()
    if not trimmed or any(character in trimmed for character in ("\r", "\n", "\0")):
        _contract(f"{field} must be one non-empty trimmed line", field=field, actual=value)
    return trimmed


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _contract(f"{field} must be 64 lowercase hexadecimal characters", field=field, actual=value)
    return value


@dataclass(frozen=True, slots=True)
class _Scope:
    kind: str
    project_id: str | None


def _scope(value: object, field: str, *, ambiguous: bool) -> _Scope:
    data = _exact_mapping(value, _SCOPE_FIELDS, field)
    kind = data["kind"]
    project_id = data["project_id"]
    allowed = _SCOPES | ({"ambiguous"} if ambiguous else frozenset())
    if not isinstance(kind, str) or kind not in allowed:
        _contract(f"{field}.kind is unknown", field=f"{field}.kind", actual=kind)
    if kind == "project":
        if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
            _contract(f"{field}.project_id is invalid", field=f"{field}.project_id", actual=project_id)
    elif project_id is not None:
        _contract(f"{field}.project_id must be null", field=f"{field}.project_id", actual=project_id)
    return _Scope(kind, project_id)


@dataclass(frozen=True, slots=True)
class DurabilityRequest:
    schema: str
    verb: str
    operation: str
    origin_id: str
    choice: str
    title: str
    because: str
    forecloses: str
    governing_scope: str
    encoded_in: str
    completeness_check: str
    explicit_scope: _Scope | None
    default_scope: _Scope
    source_context_sha256: str
    session_delta_sha256: str | None
    accepted_at: datetime
    expected_owner_locator: str
    expected_writer_generation: int
    observed_preimage_sha256: str
    source_evidence: tuple[Evidence, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DurabilityRequest":
        fields = _REQUEST_FIELDS | ({"source_evidence"} if "source_evidence" in value else set())
        data = _exact_mapping(value, fields, "durability_request")
        raw_evidence = data.get("source_evidence", [])
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 50:
            _contract("source_evidence must be a list of at most 50 evidence objects", field="source_evidence")
        source_evidence = tuple(evidence_from_mapping(item, "durability_request.source_evidence") for item in raw_evidence)
        if data["schema"] != REQUEST_SCHEMA:
            _contract("schema is unknown", field="schema", actual=data["schema"])
        if data["verb"] not in {"capture", "decide"}:
            _contract("verb is unknown", field="verb", actual=data["verb"])
        if data["operation"] != "accept":
            _contract("operation is unknown", field="operation", actual=data["operation"])
        origin_id = data["origin_id"]
        if not isinstance(origin_id, str) or _ORIGIN_ID.fullmatch(origin_id) is None:
            _contract("origin_id is invalid", field="origin_id", actual=origin_id)
        explicit = None if data["explicit_scope"] is None else _scope(data["explicit_scope"], "explicit_scope", ambiguous=False)
        default = _scope(data["default_scope"], "default_scope", ambiguous=True)
        delta = data["session_delta_sha256"]
        if data["verb"] == "capture":
            delta = _sha(delta, "session_delta_sha256")
        elif delta is not None:
            _contract("session_delta_sha256 must be null for Decide", field="session_delta_sha256", actual=delta)
        accepted_at = data["accepted_at"]
        if not isinstance(accepted_at, str):
            _contract("accepted_at must be RFC 3339 text", field="accepted_at", actual=accepted_at)
        try:
            accepted = datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
        except ValueError:
            _contract("accepted_at must be RFC 3339 text", field="accepted_at", actual=accepted_at)
        if accepted.tzinfo is None or accepted.utcoffset() is None:
            _contract("accepted_at requires a timezone", field="accepted_at", actual=accepted_at)
        binding = _exact_mapping(data["ownership_catalog"], _CATALOG_FIELDS, "ownership_catalog")
        expected_binding = {
            "path": CATALOG_PATH,
            "bytes": CATALOG_BYTES,
            "sha256": CATALOG_SHA256,
            "schema": CATALOG_SCHEMA,
            "catalog_id": CATALOG_ID,
        }
        if binding != expected_binding:
            _contract("ownership_catalog does not match the sole bound catalog", field="ownership_catalog", actual=binding, refusal_code="catalog_binding_mismatch")
        owner = data["expected_owner_locator"]
        if not isinstance(owner, str) or not owner.startswith("vault://") or "{" in owner or "}" in owner:
            _contract("expected_owner_locator must be a resolved vault locator", field="expected_owner_locator", actual=owner)
        generation = data["expected_writer_generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
            _contract("expected_writer_generation must be positive", field="expected_writer_generation", actual=generation)
        return cls(
            schema=REQUEST_SCHEMA,
            verb=data["verb"],
            operation="accept",
            origin_id=origin_id,
            choice=_text(data["choice"], "choice"),
            title=_text(data["title"], "title"),
            because=_text(data["because"], "because"),
            forecloses=_text(data["forecloses"], "forecloses"),
            governing_scope=_text(data["governing_scope"], "governing_scope"),
            encoded_in=_text(data["encoded_in"], "encoded_in"),
            completeness_check=_text(data["completeness_check"], "completeness_check"),
            explicit_scope=explicit,
            default_scope=default,
            source_context_sha256=_sha(data["source_context_sha256"], "source_context_sha256"),
            session_delta_sha256=delta,
            accepted_at=accepted,
            expected_owner_locator=owner,
            expected_writer_generation=generation,
            observed_preimage_sha256=_sha(data["observed_preimage_sha256"], "observed_preimage_sha256"),
            source_evidence=source_evidence,
        )

    @property
    def selected_scope(self) -> _Scope:
        return self.explicit_scope if self.explicit_scope is not None else self.default_scope


@dataclass(frozen=True, slots=True)
class OwnerRoute:
    owner_id: str
    owner_locator: str
    writer_locator: str
    writer_generation: int
    catalog_id: str
    catalog_sha256: str
    surface_kind: str
    root: Path
    canonical_relative_path: str
    lock_relative_path: str
    receipt_relative_root: str


@dataclass(frozen=True, slots=True)
class DurabilityReceipt:
    schema: str
    writer_id: str
    origin_id: str
    semantic_key: str
    status: str
    owner_id: str | None
    owner_locator: str
    writer_locator: str | None
    writer_generation: int
    canonical_locator: str | None
    canonical_identity: str | None
    canonical_sha256: str | None
    refusal_code: str | None
    model_calls: int = 0
    network_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mutex(path: Path) -> Any:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _PROCESS_MUTEX_GUARD:
        return _PROCESS_MUTEXES.setdefault(key, threading.RLock())


@contextmanager
def _file_lock(path: Path) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise BrainError(ErrorCode.RECORD_WRITE_FAILED, "Could not open durability lock", details={"path": str(path), "error": type(exc).__name__, "refusal_code": "lock_failed"}) from exc
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise BrainError(ErrorCode.RECORD_WRITE_FAILED, "Could not hold durability lock", details={"path": str(path), "error": type(exc).__name__, "refusal_code": "lock_failed"}) from exc
    finally:
        handle.close()


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & 0x400)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(candidate)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def _atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise BrainError(ErrorCode.RECORD_WRITE_FAILED, "Atomic durability publication failed", details={"path": str(path), "error": type(exc).__name__, "refusal_code": "publication_failed"}) from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError as exc:
                raise BrainError(ErrorCode.RECORD_WRITE_FAILED, "Durability temporary cleanup failed", details={"path": str(temporary), "error": type(exc).__name__, "refusal_code": "temporary_cleanup_failed"}) from exc


def _slug(value: str) -> str:
    rendered = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", rendered).strip("-")[:48] or "decision"


class SharedDurabilityWriter:
    """Resolve one request and publish exactly once to its canonical owner."""

    def __init__(
        self,
        *,
        root_bindings: Mapping[str, str | Path] | None = None,
        project_roots: Mapping[str, str | Path] | None = None,
        catalog_path: str | Path | None = None,
        fault: Callable[[str, Path], None] | None = None,
    ) -> None:
        supplied = _ROOT_BINDINGS if root_bindings is None else root_bindings
        self._roots = {key: Path(value) for key, value in supplied.items()}
        self._project_roots = {key: Path(value) for key, value in (project_roots or {}).items()}
        self._fault = fault
        # Physical host binding only: the wire catalog identity stays unchanged.
        self._catalog_path = Path(CATALOG_PATH if catalog_path is None else catalog_path)

    def write(self, value: Mapping[str, Any]) -> DurabilityReceipt:
        request = DurabilityRequest.from_mapping(value)
        scope = request.selected_scope
        if scope.kind == "ambiguous":
            return self._failure(request, "scope_ambiguous")
        try:
            route = self._resolve_route(request, scope)
            semantic_key = self._semantic_key(request, route)
            if route.surface_kind == "brain-records":
                return self._write_brain_record(request, route, semantic_key)
            return self._write_markdown(request, route, semantic_key)
        except BrainError as exc:
            refusal = exc.details.get("refusal_code") if isinstance(exc.details.get("refusal_code"), str) else exc.code.value
            return self._failure(request, refusal)
        except OwnershipCatalogError as exc:
            return self._failure(request, exc.code)
        except (OSError, UnicodeError):
            return self._failure(request, "canonical_io_failed")

    def _failure(self, request: DurabilityRequest, refusal_code: str) -> DurabilityReceipt:
        return DurabilityReceipt(
            RECEIPT_SCHEMA, WRITER_ID, request.origin_id, "", "failure", None,
            request.expected_owner_locator, None, request.expected_writer_generation,
            None, None, None, refusal_code,
        )

    def _load_catalog(self):
        path = self._catalog_path
        if not path.is_file() or _is_reparse(path):
            raise BrainError(ErrorCode.CONTRACT_INVALID, "Bound ownership catalog is unavailable", details={"refusal_code": "catalog_unavailable"})
        raw = path.read_bytes()
        if len(raw) != CATALOG_BYTES or hashlib.sha256(raw).hexdigest().upper() != CATALOG_SHA256:
            raise BrainError(ErrorCode.CONTRACT_INVALID, "Bound ownership catalog changed", details={"refusal_code": "catalog_binding_mismatch"})
        catalog = parse_ownership_catalog(raw, expected_sha256=CATALOG_SHA256.lower())
        if catalog.schema != CATALOG_SCHEMA or catalog.catalog_id != CATALOG_ID:
            raise BrainError(ErrorCode.CONTRACT_INVALID, "Bound ownership catalog identity changed", details={"refusal_code": "catalog_binding_mismatch"})
        return catalog

    def _resolve_route(self, request: DurabilityRequest, scope: _Scope) -> OwnerRoute:
        responsibility = {
            "personal": "personal_memory",
            "cross-project": "cross_project_memory",
            "system": "system_decisions",
            "fleet": "system_decisions",
            "project": "project_decisions",
        }[scope.kind]
        resolution = resolve_ownership(
            self._load_catalog(), responsibility, project_id=scope.project_id, at=request.accepted_at,
            expected_owner_locator=request.expected_owner_locator,
            expected_writer_generation=request.expected_writer_generation,
            observed_preimage_sha256=request.observed_preimage_sha256,
        )
        if isinstance(resolution, OwnershipRefusal):
            raise BrainError(ErrorCode.CONTRACT_INVALID, resolution.detail, details={"refusal_code": resolution.code.value})
        if resolution.owner_locator == "vault://brain-home":
            root = self._roots.get(resolution.owner_locator)
            surface, canonical, lock, receipts = "brain-records", "records", "records/.durability/writer.lock", "records/.durability/receipts"
        elif resolution.owner_locator == "vault://meta-vault":
            root = self._roots.get(resolution.owner_locator)
            surface, canonical, lock, receipts = "decisions-markdown", "ops/decisions.md", "ops/.durability/writer.lock", "ops/.durability/receipts"
        elif scope.kind == "project" and scope.project_id is not None:
            root = self._project_roots.get(scope.project_id)
            surface, canonical, lock, receipts = "decisions-markdown", "ops/decisions.md", "ops/.durability/writer.lock", "ops/.durability/receipts"
        else:
            root = None
            surface, canonical, lock, receipts = "", "", "", ""
        if root is None or not root.is_dir() or _is_reparse(root):
            raise BrainError(ErrorCode.ROOT_MISSING, "Resolved owner root is unavailable", details={"refusal_code": "owner_root_unavailable"})
        root = root.resolve(strict=True)
        if scope.kind == "project" and not self._project_exposes_decisions(root):
            raise BrainError(ErrorCode.CONTRACT_INVALID, "Project contract does not expose ops/decisions.md", details={"refusal_code": "project_decisions_unavailable"})
        canonical_path = (root / canonical).resolve(strict=False)
        if not _inside(root, canonical_path):
            raise BrainError(ErrorCode.PATH_ESCAPE, "Resolved canonical surface escaped its owner", details={"refusal_code": "owner_surface_escape"})
        if surface == "decisions-markdown" and (not canonical_path.is_file() or _is_reparse(canonical_path)):
            raise BrainError(ErrorCode.PATH_MISSING, "Canonical decisions surface is unavailable", details={"refusal_code": "owner_surface_unavailable"})
        return OwnerRoute(
            resolution.owner_id, resolution.owner_locator, resolution.writer_locator,
            resolution.writer_generation, resolution.catalog_id, resolution.catalog_sha256,
            surface, root, canonical, lock, receipts,
        )

    @staticmethod
    def _project_exposes_decisions(root: Path) -> bool:
        manifest = root / "ops" / "vault-manifest.md"
        if not manifest.is_file() or _is_reparse(manifest):
            return False
        try:
            text = manifest.read_text(encoding="utf-8")
            lines = text.splitlines()
            if not lines or lines[0].strip() != "---":
                return False
            try:
                end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
            except StopIteration:
                return False
            value = YAML(typ="safe", pure=True).load("\n".join(lines[1:end]) + "\n")
        except YAMLError as exc:
            raise BrainError(
                ErrorCode.CONTRACT_INVALID,
                "Project manifest frontmatter is invalid YAML",
                details={"refusal_code": "project_manifest_invalid"},
            ) from exc
        except (OSError, UnicodeError):
            return False
        if not isinstance(value, Mapping) or not isinstance(value.get("exports"), Mapping):
            return False
        exports = value["exports"]
        return exports.get("decisions") == "ops/decisions.md" or exports.get("decision-log") == "ops/decisions.md"

    @staticmethod
    def _semantic_key(request: DurabilityRequest, route: OwnerRoute) -> str:
        payload = {
            "choice": request.choice,
            "governing_scope": request.governing_scope,
            "operation": "accept",
            "owner_locator": route.owner_locator,
            "schema": "brain.durable-decision-key/v1",
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _record(request: DurabilityRequest, route: OwnerRoute, semantic_key: str) -> Record:
        evidence = Evidence(
            source_type="durability_request",
            locator=f"origin:{request.origin_id}",
            content_sha256=request.source_context_sha256,
            excerpt=request.choice,
            actor="nic",
        )
        return record_from_mapping(
            {
                "schema": "brain.record/v1",
                "id": new_record_id(request.accepted_at, semantic_key[:16]),
                "kind": "decision",
                "title": request.title,
                "state": "accepted",
                "created_at": request.accepted_at.isoformat(),
                "updated_at": request.accepted_at.isoformat(),
                "assertion": "explicit",
                "confidence": 1.0,
                "sensitivity": "private",
                "vaults": [route.owner_locator],
                "owners": [route.owner_id],
                "entities": [],
                "evidence": [evidence.as_dict(), *(item.as_dict() for item in request.source_evidence)],
                "supersedes": [],
                "dedupe_key": semantic_key,
                "data": {
                    "because": request.because,
                    "forecloses": request.forecloses,
                    "governs": request.governing_scope,
                    "encoded_in": request.encoded_in,
                    "completeness_check": request.completeness_check,
                    "origin_id": request.origin_id,
                },
                "body": "",
            },
            "durability.record",
        )

    def _success_receipt(
        self, request: DurabilityRequest, route: OwnerRoute, semantic_key: str, *,
        status: str, canonical_locator: str, canonical_identity: str, canonical_sha256: str,
    ) -> DurabilityReceipt:
        return DurabilityReceipt(
            RECEIPT_SCHEMA, WRITER_ID, request.origin_id, semantic_key, status,
            route.owner_id, route.owner_locator, route.writer_locator, route.writer_generation,
            canonical_locator, canonical_identity, canonical_sha256, None,
        )

    def _publish_receipt(self, route: OwnerRoute, receipt: DurabilityReceipt) -> DurabilityReceipt:
        path = route.root / route.receipt_relative_root / f"{receipt.origin_id}.json"
        resolved_parent = path.parent.resolve(strict=False)
        if not _inside(route.root, resolved_parent):
            raise BrainError(ErrorCode.PATH_ESCAPE, "Receipt path escaped its owner", details={"refusal_code": "receipt_path_escape"})
        content = (canonical_json(receipt.as_dict()) + "\n").encode("utf-8")
        if path.exists():
            if path.read_bytes() != content:
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Origin receipt conflicts with canonical truth", details={"refusal_code": "receipt_conflict"})
        else:
            _atomic_replace(path, content)
        if path.read_bytes() != content:
            raise BrainError(ErrorCode.RECORD_CORRUPT, "Origin receipt postimage changed", details={"refusal_code": "receipt_postimage_changed"})
        return receipt

    def _write_brain_record(self, request: DurabilityRequest, route: OwnerRoute, semantic_key: str) -> DurabilityReceipt:
        store = RecordStore(route.root)
        candidate = self._record(request, route, semantic_key)
        result: list[DurabilityReceipt] = []
        receipt_path = route.root / route.receipt_relative_root / f"{request.origin_id}.json"

        def before_create() -> None:
            if receipt_path.exists():
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Origin receipt has no canonical decision", details={"refusal_code": "receipt_orphaned"})

        def finalize(record: Record, write_receipt: WriteReceipt, published: bool) -> None:
            canonical_path = route.root / write_receipt.relative_path
            raw = canonical_path.read_bytes()
            observed = parse_record(raw.decode("utf-8"), str(canonical_path))
            if observed.id != record.id or observed.dedupe_key != semantic_key or hashlib.sha256(raw).hexdigest() != write_receipt.content_sha256:
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Canonical decision postimage is inconsistent", details={"refusal_code": "canonical_postimage_changed"})
            if observed.data.get("origin_id") == request.origin_id and observed.evidence != candidate.evidence:
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Stable origin is already bound to different evidence", details={"refusal_code": "origin_evidence_conflict"})
            if published and self._fault is not None:
                self._fault("after_canonical_publish_before_receipt", canonical_path)
                raw = canonical_path.read_bytes()
                observed = parse_record(raw.decode("utf-8"), str(canonical_path))
                if observed.id != record.id or observed.dedupe_key != semantic_key or hashlib.sha256(raw).hexdigest() != write_receipt.content_sha256:
                    raise BrainError(ErrorCode.RECORD_CORRUPT, "Canonical decision postimage changed before receipt", details={"refusal_code": "canonical_postimage_changed"})
            status = "created" if observed.data.get("origin_id") == request.origin_id else "duplicate/no-op"
            receipt = self._success_receipt(
                request, route, semantic_key, status=status,
                canonical_locator=f"{route.owner_locator}/{write_receipt.relative_path}",
                canonical_identity=observed.id,
                canonical_sha256=hashlib.sha256(raw).hexdigest(),
            )
            result.append(self._publish_receipt(route, receipt))
            if self._fault is not None:
                self._fault("after_receipt_publish", route.root / route.receipt_relative_root / f"{request.origin_id}.json")

        store.create_semantic(
            candidate,
            semantic_key=semantic_key,
            origin_id=request.origin_id,
            observed_preimage_sha256=request.observed_preimage_sha256,
            before_create=before_create,
            finalize=finalize,
        )
        if len(result) != 1:
            raise BrainError(ErrorCode.RECORD_WRITE_FAILED, "Canonical decision produced no receipt", details={"refusal_code": "receipt_missing"})
        return result[0]

    @staticmethod
    def _markdown_matches(raw: bytes, semantic_key: str) -> tuple[dict[str, str], ...]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BrainError(ErrorCode.RECORD_CORRUPT, "Decisions surface is not UTF-8", details={"refusal_code": "canonical_encoding_invalid"}) from exc
        if "\r" in text:
            raise BrainError(ErrorCode.RECORD_CORRUPT, "Decisions surface is not LF-only", details={"refusal_code": "canonical_encoding_invalid"})
        starts = tuple(re.finditer(r"(?m)^- \[(\d{4}-\d{2}-\d{2})\] Decision: ([^\n]+)\n", text))
        matches: list[dict[str, str]] = []
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            block = text[start.start():end].rstrip("\n") + "\n"
            if f"  Semantic-Key: {semantic_key}\n" not in block:
                continue
            origin = re.search(r"(?m)^  Origin-ID: ([a-z0-9][a-z0-9-]{0,127})$", block)
            identity = re.search(r"(?m)^  Decision-ID: ([^\n]+)$", block)
            if origin is None or identity is None:
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Semantic decision is incomplete", details={"refusal_code": "semantic_origin_corrupt"})
            matches.append({"origin_id": origin.group(1), "identity": identity.group(1), "block": block})
        return tuple(matches)

    @staticmethod
    def _decision_block(request: DurabilityRequest, semantic_key: str) -> tuple[str, bytes]:
        identity = f"{request.accepted_at:%Y%m%d}-{_slug(request.title)}-{semantic_key[:12]}"
        state = "unencoded" if request.encoded_in == "UNENCODED" else "encoded"
        block = "\n".join(
            (
                f"- [{request.accepted_at:%Y-%m-%d}] Decision: {request.title}",
                f"  Decision-ID: {identity}",
                f"  Because: {request.because}",
                f"  Forecloses: {request.forecloses}",
                f"  Governs: {request.governing_scope}",
                f"  Encoded in: {request.encoded_in}",
                f"  Completeness check: {request.completeness_check}",
                f"  Origin-ID: {request.origin_id}",
                f"  Semantic-Key: {semantic_key}",
                f"  State: {state}",
                "",
            )
        ).encode("utf-8")
        return identity, block

    def _write_markdown(self, request: DurabilityRequest, route: OwnerRoute, semantic_key: str) -> DurabilityReceipt:
        canonical_path = route.root / route.canonical_relative_path
        lock_path = route.root / route.lock_relative_path
        with _mutex(lock_path), _file_lock(lock_path):
            raw = canonical_path.read_bytes()
            matches = self._markdown_matches(raw, semantic_key)
            if len(matches) > 1:
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Decision semantic key has multiple canonical origins", details={"refusal_code": "semantic_origin_corrupt"})
            if matches:
                accepted = matches[0]
                status = "created" if accepted["origin_id"] == request.origin_id else "duplicate/no-op"
                receipt = self._success_receipt(
                    request, route, semantic_key, status=status,
                    canonical_locator=f"{route.owner_locator}/{route.canonical_relative_path}#{accepted['identity']}",
                    canonical_identity=accepted["identity"],
                    canonical_sha256=hashlib.sha256(accepted["block"].encode("utf-8")).hexdigest(),
                )
                return self._publish_receipt(route, receipt)
            if f"  Origin-ID: {request.origin_id}\n" in raw.decode("utf-8"):
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Origin ID already belongs to a different semantic decision", details={"refusal_code": "origin_id_conflict"})
            receipt_path = route.root / route.receipt_relative_root / f"{request.origin_id}.json"
            if receipt_path.exists():
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Origin receipt has no canonical decision", details={"refusal_code": "receipt_orphaned"})
            actual_preimage = hashlib.sha256(raw).hexdigest()
            if actual_preimage != request.observed_preimage_sha256:
                raise BrainError(ErrorCode.CONTRACT_INVALID, "Canonical decisions preimage changed", details={"refusal_code": "canonical_preimage_changed"})
            identity, block = self._decision_block(request, semantic_key)
            if raw and not raw.endswith(b"\n"):
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Canonical decisions surface lacks terminal LF", details={"refusal_code": "canonical_encoding_invalid"})
            separator = b"" if not raw or raw.endswith(b"\n\n") else b"\n"
            postimage = raw + separator + block
            if canonical_path.read_bytes() != raw:
                raise BrainError(ErrorCode.CONTRACT_INVALID, "Canonical decisions preimage changed inside the lock", details={"refusal_code": "canonical_preimage_changed"})
            _atomic_replace(canonical_path, postimage)
            observed = canonical_path.read_bytes()
            if observed != postimage:
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Canonical decisions postimage changed", details={"refusal_code": "canonical_postimage_changed"})
            if self._fault is not None:
                self._fault("after_canonical_publish_before_receipt", canonical_path)
                observed = canonical_path.read_bytes()
            accepted = self._markdown_matches(observed, semantic_key)
            if len(accepted) != 1 or accepted[0]["origin_id"] != request.origin_id or accepted[0]["identity"] != identity:
                raise BrainError(ErrorCode.RECORD_CORRUPT, "Canonical decision cannot reconstruct its origin", details={"refusal_code": "canonical_postimage_changed"})
            receipt = self._success_receipt(
                request, route, semantic_key, status="created",
                canonical_locator=f"{route.owner_locator}/{route.canonical_relative_path}#{identity}",
                canonical_identity=identity,
                canonical_sha256=hashlib.sha256(accepted[0]["block"].encode("utf-8")).hexdigest(),
            )
            result = self._publish_receipt(route, receipt)
            if self._fault is not None:
                self._fault("after_receipt_publish", route.root / route.receipt_relative_root / f"{request.origin_id}.json")
            return result
