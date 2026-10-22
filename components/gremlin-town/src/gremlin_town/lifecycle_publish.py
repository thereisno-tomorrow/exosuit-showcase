"""Fail-closed publication of host-bound lifecycle state into existing policy files.

No registry access, credential issuing, native installation, or sync-state writer.
Publication holds dispatch_lock; policy.lock is never held over DB work. Hash checks
protect cooperative edits; a hostile writer ignoring those locks is not supported.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib
from uuid import UUID

from gremlin_town.lifecycle import LifecycleError

BASELINE = "fleet-boss-grok"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TABLE = re.compile(r"(?m)^\[\[(participant|route)\]\][ \t]*(?:#[^\n]*)?\r?$" )


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value) or value == "0" * 64:
        raise LifecycleError("invalid_token_hash")
    return value


def _managed(actor):
    if not isinstance(actor, str) or not actor.startswith("grok-"):
        return False
    try:
        return str(UUID(actor[5:])) == actor[5:]
    except ValueError:
        return False


def _read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise LifecycleError("policy_path_not_regular")
    return path.read_bytes(), info


def _metadata(path, info):
    if os.name == "posix":
        os.chown(path, info.st_uid, info.st_gid)
    os.chmod(path, stat.S_IMODE(info.st_mode))


def _replace(path, temporary):
    if os.name == "nt":
        # ReplaceFile preserves the target's Windows ACL and identity metadata.
        replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
        replace.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        replace.restype = ctypes.c_int
        if not replace(str(path), str(temporary), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(temporary, path)
    if os.name == "posix":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic(path, expected, content):
    """Preserve exact preimage; reject concurrent change before atomic replacement."""
    raw, info = _read(path)
    if _digest(raw) != expected:
        raise LifecycleError("policy_preimage_conflict")
    if raw == content:
        return expected
    backup = path.with_name(path.name + ".lifecycle-preimage-" + expected)
    try:
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        prior, _ = _read(backup)
        if prior != raw:
            raise LifecycleError("policy_backup_conflict")
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _metadata(backup, info)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".lifecycle-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _metadata(temporary, info)
        current, current_info = _read(path)
        if (_digest(current) != expected or current_info.st_ino != info.st_ino
                or stat.S_IMODE(current_info.st_mode) != stat.S_IMODE(info.st_mode)
                or (current_info.st_uid, current_info.st_gid) != (info.st_uid, info.st_gid)):
            raise LifecycleError("policy_preimage_conflict")
        _replace(path, temporary)
        post, post_info = _read(path)
        if post != content or stat.S_IMODE(post_info.st_mode) != stat.S_IMODE(info.st_mode):
            raise LifecycleError("policy_postimage_conflict")
        if os.name == "posix" and (post_info.st_uid, post_info.st_gid) != (info.st_uid, info.st_gid):
            raise LifecycleError("policy_ownership_changed")
    finally:
        if temporary.exists():
            temporary.unlink()
    return _digest(content)


def _table(kind, row):
    # Participant/route schema permits only scalars and scalar arrays.
    return "[[" + kind + "]]\n" + "".join(
        json.dumps(key) + " = " + json.dumps(value, ensure_ascii=True) + "\n"
        for key, value in row.items()) + "\n"


def _render(raw, before, after):
    """Keep unrelated tables/comments byte-for-byte, append only new route rows."""
    text = raw.decode("utf-8")
    matches = list(_TABLE.finditer(text))
    if not matches:
        raise LifecycleError("unsupported_participant_toml_layout")
    counts = {"participant": 0, "route": 0}
    output = [text[:matches[0].start()]]
    for i, match in enumerate(matches):
        kind = match.group(1)
        old = before.get(kind, [])[counts[kind]]
        counts[kind] += 1
        new = (next(item for item in after[kind] if item["id"] == old["id"])
               if kind == "participant" else old)
        chunk = text[match.start():matches[i+1].start() if i+1 < len(matches) else len(text)]
        output.append(chunk if new == old else _table(kind, new))
    if any(counts[kind] != len(before.get(kind, [])) for kind in counts):
        raise LifecycleError("unsupported_participant_toml_layout")
    for kind in counts:
        for row in after.get(kind, []):
            if (kind == "participant" and any(r["id"] == row["id"] for r in before[kind])) or (kind == "route" and row in before.get(kind, [])):
                continue
            output.append("\n" + _table(kind, row))
    encoded = "".join(output).encode("utf-8")
    if tomllib.loads(encoded.decode()) != after:
        raise LifecycleError("participant_render_mismatch")
    return encoded


def _validate_files(participant_path, participant_bytes, brain_path, brain_bytes):
    from gremlin_town.runtime import _load_participants
    from gremlin_town.mcp.brain_assembly import load_owner_policy
    for path, raw, validate in ((participant_path, participant_bytes, _load_participants),
                                (brain_path, brain_bytes, load_owner_policy)):
        descriptor, name = tempfile.mkstemp(prefix=".lifecycle-validate-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
            validate(temporary)
        finally:
            temporary.unlink()


class FilePolicyPublisher:
    def __init__(self, runtime, participant_path):
        self.runtime = runtime
        self.participant_path = Path(participant_path)

    def _candidate(self, bots, participants, brain):
        if not isinstance(bots, dict):
            raise LifecycleError("invalid_bot_mapping")
        rows = {row["id"]: row for row in participants["participant"]}
        template = rows.get(BASELINE)
        if (not template or template.get("role", "participant") != "participant"
                or template.get("auth_mode", "bearer") != "bearer" or not template.get("enabled", True)):
            raise LifecycleError("invalid_nonoperator_baseline")
        scopes = brain.get("subject_scopes", {}).get(BASELINE)
        if not isinstance(scopes, list) or "bc:read" not in scopes:
            raise LifecycleError("baseline_brain_scopes_missing")
        target = deepcopy(participants)
        target_rows = {row["id"]: row for row in target["participant"]}
        desired = {}
        revoked = set()
        for uid, bot in bots.items():
            if not isinstance(uid, str) or not isinstance(bot, dict) or not _managed("grok-" + uid) or bot.get("actor_id") != "grok-" + uid:
                raise LifecycleError("invalid_lifecycle_identity")
            if bot.get("state") not in {"ready", "pending", "inactive"}:
                raise LifecycleError("invalid_lifecycle_state")
            if not isinstance(bot.get("name"), str) or not bot["name"].strip() or len(bot["name"]) > 256:
                raise LifecycleError("invalid_lifecycle_name")
            histories = bot.get("revoked_hashes", [])
            if not isinstance(histories, list):
                raise LifecycleError("invalid_revoked_hashes")
            revoked.update(_hash(value) for value in histories)
            if "token_sha256" in bot:
                _hash(bot["token_sha256"])
            if bot["state"] == "ready" and not bot.get("token_sha256"):
                raise LifecycleError("ready_token_missing")
            desired[bot["actor_id"]] = bot
        # Whole manager state is authoritative here; never inspect native profiles.
        for actor in rows:
            if _managed(actor) and actor not in desired:
                desired[actor] = {"state":"inactive", "name":rows[actor].get("display_name",actor)}
        ready_hashes = set()
        ready_addresses = set()
        blocked_routes = set()
        for actor, bot in desired.items():
            old = rows.get(actor)
            if old and old.get("role", "participant") != "participant":
                raise LifecycleError("operator_identity_collision")
            ready = bot["state"] == "ready"
            expected_routes = []
            for route in participants.get("route", []):
                if BASELINE in (route["source"], route["destination"]):
                    expected_routes.append({**route,
                        "source": actor if route["source"] == BASELINE else route["source"],
                        "destination": actor if route["destination"] == BASELINE else route["destination"]})
            if ready and any(r not in expected_routes for r in participants.get("route", [])
                             if actor in (r["source"], r["destination"])):
                # Retain historical routing metadata but never keep a revoked
                # baseline route live. An operator must review this discrepancy.
                blocked_routes.add(actor)
                bot = {**bot, "state":"inactive"}
                desired[actor] = bot
                ready = False
            digest = bot.get("token_sha256") if ready else None
            if ready:
                address = bot.get("delivery_address")
                if (not isinstance(address, str) or len(address) > 254
                        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", address)):
                    raise LifecycleError("invalid_native_delivery_address")
                if (address.casefold() in ready_addresses or any(
                        other != actor and row["address"].casefold() == address.casefold()
                        for other, row in rows.items()) or any(
                        other != actor and isinstance(item.get("delivery_address"), str)
                        and item["delivery_address"].casefold() == address.casefold()
                        for other, item in desired.items())):
                    raise LifecycleError("native_delivery_address_collision")
                if old and old["address"] != address:
                    raise LifecycleError("stable_identity_address_changed")
                ready_addresses.add(address.casefold())
                if (digest in revoked or digest in ready_hashes or any(
                        other != actor and item.get("token_sha256") == digest
                        for other, item in desired.items())):
                    raise LifecycleError("reused_or_revoked_hash")
                for other, row in rows.items():
                    if row.get("token_sha256") == digest and other != actor:
                        raise LifecycleError("credential_collision_or_resurrection")
                ready_hashes.add(digest)
            if old:
                row = target_rows[actor]
                # Inactive rows retain scopes and routes for historical reads.
                row["display_name"] = bot["name"]
                row["enabled"] = ready
                if ready:
                    if row.get("projects", []) != template.get("projects", []) or row.get("sensitivities", []) != template.get("sensitivities", []):
                        raise LifecycleError("existing_actor_scope_differs_from_baseline")
                    row["auth_mode"] = "bearer"
                    row["token_sha256"] = digest
            else:
                # No placeholder routes or authority before a protected receipt.
                if not ready:
                    continue
                row = {"id":actor, "display_name":bot["name"],
                       "address":address,
                       "role":"participant", "enabled":True, "auth_mode":"bearer",
                       "projects":deepcopy(template.get("projects", [])),
                       "sensitivities":deepcopy(template.get("sensitivities", [])), "aliases":[],
                       "token_sha256":digest}
                target["participant"].append(row)
                target_rows[actor] = row
            if ready:
                for route in participants.get("route", []):
                    if BASELINE not in (route["source"], route["destination"]):
                        continue
                    clone = dict(route)
                    clone["source"] = actor if clone["source"] == BASELINE else clone["source"]
                    clone["destination"] = actor if clone["destination"] == BASELINE else clone["destination"]
                    if clone not in target.setdefault("route", []):
                        target["route"].append(clone)
        new_brain = deepcopy(brain)
        new_brain.setdefault("subject_scopes", {})
        for actor, bot in desired.items():
            new_brain["subject_scopes"].pop(actor, None)
            if bot["state"] == "ready":
                new_brain["subject_scopes"][actor] = list(scopes)
            for old_owner, owner in zip(brain["owners"], new_brain["owners"], strict=True):
                for field in ("readers", "writers"):
                    if bot["state"] == "ready" and BASELINE in old_owner[field]:
                        if actor not in owner[field]:
                            owner[field].append(actor)
                    else:
                        owner[field] = [value for value in owner[field] if value != actor]
        return target, new_brain, desired, blocked_routes

    def _holds(self, inactive):
        store = self.runtime.store
        with store.transaction() as db:
            for actor in sorted(inactive):
                requests = db.execute("""SELECT request_id FROM requests WHERE
                    (requester_id=? OR recipient_id=?) AND
                    (work_state IN ('requested','acknowledged','in_progress','blocked') OR
                     EXISTS(SELECT 1 FROM operations o WHERE o.request_id=requests.request_id
                       AND o.operation_state IN ('queued','sending','uncertain')))""", (actor,actor)).fetchall()
                operations = db.execute("""SELECT operation_id,request_id FROM operations WHERE
                    operation_state IN ('queued','sending','uncertain') AND
                    (actor_id=? OR route_source_id=? OR route_destination_id=? OR
                     request_id IN (SELECT request_id FROM requests WHERE requester_id=? OR recipient_id=?))""",
                    (actor,actor,actor,actor,actor)).fetchall()
                ids = {row["request_id"] for row in requests} | {row["request_id"] for row in operations}
                for identifier in sorted(ids):
                    changed = db.execute("UPDATE requests SET recovery_hold=1 WHERE request_id=? AND recovery_hold=0",(identifier,)).rowcount
                    if changed:
                        store.append_event(db, request_id=identifier, operation_id=None, actor_id="system:grok-lifecycle",
                            event_type="grok_lifecycle_hold_applied", detail={"participant_id":actor,"reason":"participant_inactive","disposition":"operator_required"})
                for row in operations:
                    changed = db.execute("UPDATE operations SET recovery_hold=1 WHERE operation_id=? AND recovery_hold=0",(row["operation_id"],)).rowcount
                    if changed:
                        store.append_event(db, request_id=row["request_id"], operation_id=row["operation_id"], actor_id="system:grok-lifecycle",
                            event_type="grok_lifecycle_hold_applied", detail={"participant_id":actor,"reason":"participant_inactive","disposition":"operator_required"})

    def publish(self, bots):
        runtime = self.runtime
        brain_path = Path(runtime.brain.policy_file)
        path = self.participant_path
        if getattr(runtime, "participants_file", path) != path:
            raise LifecycleError("participant_refresh_path_mismatch")
        with runtime.policy.dispatch_lock:
            raw, _ = _read(path)
            brain_raw, _ = _read(brain_path)
            before = tomllib.loads(raw.decode("utf-8"))
            brain = json.loads(brain_raw)
            _validate_files(path, raw, brain_path, brain_raw)
            target, new_brain, desired, blocked_routes = self._candidate(bots, before, brain)
            # A previously missed publication can leave inactive disk identities
            # enabled in memory. Revoke before any DB wait or failing refresh.
            with runtime.policy.lock:
                for actor, bot in desired.items():
                    if bot["state"] != "ready" and actor in runtime.policy._participants:
                        runtime.policy.set_enabled(actor, False)
            changed = set()
            old_rows = {row["id"]: row for row in before["participant"]}
            target_rows = {row["id"]: row for row in target["participant"]}
            for actor in desired:
                row = target_rows.get(actor)
                def grants(policy):
                    return (policy.get("subject_scopes", {}).get(actor),
                            [(actor in o["readers"], actor in o["writers"]) for o in policy["owners"]])
                def routes(doc):
                    return [r for r in doc.get("route", []) if actor in (r["source"],r["destination"])]
                if row != old_rows.get(actor) or grants(brain) != grants(new_brain) or routes(before) != routes(target):
                    changed.add(actor)
            if not changed:
                self._holds({a for a,b in desired.items() if b["state"] != "ready"})
                # Equal files may follow a missed runtime publication. Reload
                # without disabling unchanged identities or rewriting files.
                runtime.refresh_participants()
                if blocked_routes:
                    raise LifecycleError("obsolete_managed_routes_operator_review_required")
                return {"status":"unchanged", "changed":[]}
            # Preserve revision when semantics are unchanged; deterministic content
            # revision avoids accumulating suffixes on idempotent reconciliation.
            if new_brain != brain:
                material = {k:v for k,v in new_brain.items() if k != "revision"}
                new_brain["revision"] = "grok-lifecycle-" + _digest(json.dumps(material,sort_keys=True).encode())[:24]
            final_raw = _render(raw, before, target)
            brain_final = (json.dumps(new_brain,indent=2,ensure_ascii=True)+"\n").encode() if new_brain != brain else brain_raw
            stage = deepcopy(target)
            for row in stage["participant"]:
                if row["id"] in changed:
                    row["enabled"] = False
            stage_raw = _render(raw, before, stage)
            _validate_files(path, final_raw, brain_path, brain_final)
            _validate_files(path, stage_raw, brain_path, brain_raw)
            participant_hash, brain_hash = _digest(raw), _digest(brain_raw)
            if _digest(_read(brain_path)[0]) != brain_hash:
                raise LifecycleError("policy_preimage_conflict")
            staged = False
            try:
                staged = True
                participant_hash = _atomic(path, participant_hash, stage_raw)
                self._holds({a for a,b in desired.items() if b["state"] != "ready"})
                runtime.refresh_participants()
                brain_hash = _atomic(brain_path, brain_hash, brain_final)
                participant_hash = _atomic(path, participant_hash, final_raw)
                runtime.refresh_participants()
                if blocked_routes:
                    raise LifecycleError("obsolete_managed_routes_operator_review_required")
                return {"status":"published", "changed":sorted(changed),
                        "participants_sha256":participant_hash,"brain_policy_sha256":brain_hash}
            except BaseException:
                if staged:
                    # Do not restore enabled preimages or clear holds after failure.
                    with runtime.policy.lock:
                        for actor in changed:
                            if actor in runtime.policy._participants:
                                runtime.policy.set_enabled(actor, False)
                    current, _ = _read(path)
                    if current == final_raw and current != stage_raw:
                        _atomic(path, _digest(final_raw), stage_raw)
                    elif current != stage_raw:
                        raise LifecycleError("publication_failed_with_external_conflict") from None
                raise
