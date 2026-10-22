"""Brain Home continuity through the existing append-only Brain Core record format.

Every operation reads one GitHub revision, stages existing BC operations locally,
and conditionally publishes one immutable revision. No cache is canonical.
"""
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile

import anyio
from brain_core.continuity import (
    append_event, close_continuity, list_continuity, recover_continuity, start_continuity,
)
from brain_core.github_storage import GitHubConflict
from brain_core.records import Evidence, RecordStore, render_record

from .brain_runtime import (
    BrainOperation, BrainRuntimeError, ContinuitySummary, ContinuityWriteResult,
    ContinuityListResult, ContinuityRecovery, WriteReceipt,
)
from .brain_home_copy import SCAN_MAX_BYTES, SCAN_MAX_RECORDS
from .github_brain_provider import GitHubBrainProvider, _record_path
from .host_identity import attribute_evidence, current_write_attribution


class RoutedContinuityProvider:
    def __init__(self, project, shared):
        self.project, self.shared = project, shared

    def _provider(self, authorization):
        return self.project if authorization.selector.kind == 'project' else self.shared

    async def start(self, authorization, request):
        return await self._provider(authorization).start(authorization, request)

    async def append(self, authorization, request):
        return await self._provider(authorization).append(authorization, request)

    async def recover(self, authorization, request):
        return await self._provider(authorization).recover(authorization, request)

    async def list_open(self, authorization, request):
        return await self._provider(authorization).list_open(authorization, request)

    async def close(self, authorization, request):
        return await self._provider(authorization).close(authorization, request)


class GitHubContinuityProvider:
    def __init__(self, storage, operation_time):
        self.storage, self.operation_time = storage, operation_time

    @staticmethod
    def _authorize(authorization, operation, access):
        GitHubBrainProvider._authorize(authorization, operation, access)
        if authorization.selector.kind not in {'personal', 'cross-project'}:
            raise BrainRuntimeError('brain.policy.excluded', 'General continuity requires a Brain Home owner')

    def _snapshot(self, authorization):
        snapshot = self.storage.snapshot_records(
            kinds=frozenset({'continuity'}), max_records=SCAN_MAX_RECORDS, max_bytes=SCAN_MAX_BYTES)
        return snapshot, tuple(item for item in snapshot.records
            if authorization.owner_locator in item.record.vaults
            and authorization.owner_id in item.record.owners)

    @staticmethod
    def _stage(root, items):
        for item in items:
            target = root / _record_path(item.record)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.content)
        return RecordStore(root)

    @staticmethod
    def _receipt(request, record, version, content, duplicate=False):
        receipt = WriteReceipt(
            request.operation_id, 'duplicate' if duplicate else 'accepted', 'vault://brain-home',
            record.id, version, hashlib.sha256(content).hexdigest(), None, True,
        )
        return ContinuityWriteResult(record.id, request.session_id, record.state, not duplicate, record.updated_at, receipt)

    def _write(self, authorization, request, operation):
        # The durable key binds semantic operation, owner, and exact input. Retrying
        # after restart or a lost reply finds the original immutable record.
        owner = authorization.owner_locator + ':' + authorization.owner_id
        op_key = hashlib.sha256((owner + ':' + request.operation_id).encode()).hexdigest()
        bound = asdict(request)
        if bound.get('task_id', 0) is None:
            # N12b: a start that names no task binds exactly the input it bound before the
            # field existed, so a retry across that release still replays instead of conflicting.
            del bound['task_id']
        payload = {'operation': operation.value, 'scope':authorization.selector.kind, 'request':bound}
        attribution = current_write_attribution()
        if attribution is not None:
            payload['attribution'] = attribution
        input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',',':')).encode()).hexdigest()
        prefix = 'gt-continuity:' + op_key + ':'
        dedupe_key = prefix + input_hash
        try:
            for _ in range(4):
                snapshot, items = self._snapshot(authorization)
                prior = [item for item in items if item.record.dedupe_key.startswith(prefix)]
                if prior:
                    if len(prior) != 1 or prior[0].record.dedupe_key != dedupe_key:
                        raise BrainRuntimeError('brain.record.exists', 'Continuity operation is bound to different input')
                    item = prior[0]
                    return self._receipt(request, item.record, snapshot.revision.commit_sha, item.content, True)
                now = self.operation_time('continuity-' + op_key)
                evidence = Evidence(
                    source_type='authenticated_request', locator='gt-mcp://continuity/'+op_key,
                    content_sha256=input_hash, excerpt='Authenticated MCP continuity request; input content hash, not independent verification of external effects.',
                    actor='authenticated-mcp-principal',
                )
                evidence = attribute_evidence(evidence)
                with tempfile.TemporaryDirectory(prefix='gt-bc-continuity-') as directory:
                    root = Path(directory)
                    store = self._stage(root, items)
                    if operation == BrainOperation.CONTINUITY_START:
                        result = start_continuity(store, session_id=request.session_id, intent=request.intent,
                            sources=request.source_references, evidence=evidence, now=now,
                            next_step=request.next_step, entropy=op_key[:16], task_id=request.task_id)
                    elif operation == BrainOperation.CONTINUITY_APPEND:
                        result = append_event(store, session_id=request.session_id, event=request.event,
                            value=request.value, evidence=evidence, now=now, entropy=op_key[:16])
                    else:
                        result = close_continuity(store, session_id=request.session_id, recovered=request.recovered,
                            evidence=evidence, now=now, entropy=op_key[:16])
                    # Persist an operation receipt even for a semantic no-op, so
                    # replay with different arguments cannot execute later.
                    if not result.changed:
                        revised, _ = store.revise(result.record.id, now=now, evidence=(evidence,), entropy=op_key[:16])
                        record = revised
                    else:
                        record = result.record
                    record = replace(record, owners=(authorization.owner_id,), vaults=(authorization.owner_locator,), dedupe_key=dedupe_key)
                    content = render_record(record).encode('utf-8')
                try:
                    published = self.storage.publish_record(record_bytes=content, expected_revision=snapshot.revision.commit_sha)
                except GitHubConflict as exc:
                    if exc.code == 'github_revision_conflict':
                        continue
                    raise
                return self._receipt(request, record, published.commit_sha, content, published.status=='duplicate/no-op')
            raise BrainRuntimeError('brain.record.write_failed', 'Brain Home changed repeatedly; retry the same operation', retryable=True)
        except BrainRuntimeError:
            raise
        except Exception as exc:
            # Native BC transition/refusal codes remain meaningful, without raw data.
            code = getattr(getattr(exc, 'code', None), 'value', getattr(exc, 'code', None))
            if isinstance(code, str) and code.startswith('brain.'):
                raise BrainRuntimeError(code, 'Brain Core refused the continuity operation') from exc
            raise GitHubBrainProvider._map_storage_error(exc) from exc

    async def start(self, authorization, request):
        self._authorize(authorization, BrainOperation.CONTINUITY_START, 'write')
        return await anyio.to_thread.run_sync(self._write, authorization, request, BrainOperation.CONTINUITY_START)

    async def append(self, authorization, request):
        self._authorize(authorization, BrainOperation.CONTINUITY_APPEND, 'write')
        return await anyio.to_thread.run_sync(self._write, authorization, request, BrainOperation.CONTINUITY_APPEND)

    async def close(self, authorization, request):
        self._authorize(authorization, BrainOperation.CONTINUITY_CLOSE, 'write')
        return await anyio.to_thread.run_sync(self._write, authorization, request, BrainOperation.CONTINUITY_CLOSE)

    def _recover(self, authorization, request):
        try:
            _, items = self._snapshot(authorization)
            with tempfile.TemporaryDirectory(prefix='gt-bc-recover-') as directory:
                store = self._stage(Path(directory), items)
                candidates = [r for r in store.current(kind='continuity') if r.state in {'open','recovered'}]
                if request.session_id is not None:
                    candidates = [r for r in candidates if r.data['session_id'] == request.session_id]
                if not candidates:
                    raise BrainRuntimeError('brain.continuity.empty', 'No recoverable Brain Home continuity')
                latest = max(candidates, key=lambda r:(r.updated_at,r.id))
                brief = recover_continuity(store, session_id=request.session_id, max_tokens=request.max_tokens)
                return ContinuityRecovery(
                    record_id=brief['record_id'], session_id=brief.get('session_id', latest.data['session_id']),
                    state=brief.get('state',latest.state), intent=brief['intent'], next_step=brief['next_step'],
                    updated_at=latest.updated_at, tokenizer=brief.get('tokenizer','brain.lexical/v1'), tokens=brief['tokens'],
                    **{key:tuple(brief.get(key,())) for key in ('sources','effects','decisions','commitments','candidates','blockers')},
                    dropped=brief.get('dropped',{}), task_id=latest.data.get('task_id'),
                )
        except BrainRuntimeError:
            raise
        except Exception as exc:
            raise GitHubBrainProvider._map_storage_error(exc, reading=True) from exc

    async def recover(self, authorization, request):
        self._authorize(authorization, BrainOperation.CONTINUITY_RECOVER, 'read')
        return await anyio.to_thread.run_sync(self._recover, authorization, request)

    def _list(self, authorization, request):
        try:
            _, items = self._snapshot(authorization)
            with tempfile.TemporaryDirectory(prefix='gt-bc-list-') as directory:
                store = self._stage(Path(directory), items)
                notes = list_continuity(store, limit=request.limit)
                return ContinuityListResult(items=tuple(
                    ContinuitySummary(
                        record_id=note['record_id'], session_id=note['session_id'], intent=note['intent'],
                        next_step=note['next_step'], state=note['state'], updated_at=note['updated_at'],
                        task_id=note.get('task_id'),
                    )
                    for note in notes
                ))
        except BrainRuntimeError:
            raise
        except Exception as exc:
            raise GitHubBrainProvider._map_storage_error(exc, reading=True) from exc

    async def list_open(self, authorization, request):
        self._authorize(authorization, BrainOperation.CONTINUITY_LIST, 'read')
        return await anyio.to_thread.run_sync(self._list, authorization, request)
