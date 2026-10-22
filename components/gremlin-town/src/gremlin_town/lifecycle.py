"""Reconcile a trusted native registry through existing sync/event storage.

This is applied lifecycle state, not a second authoritative bot registry. A native
source and protected provisioner must be bound by the host; HTTP callers cannot
supply snapshots, credentials, callbacks, or arbitrary permission templates.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from uuid import UUID


class LifecycleError(ValueError):
    pass


def validate_snapshot(raw, source):
    if not isinstance(raw, dict) or set(raw) != {"source", "revision", "kind", "complete", "next_cursor", "bots"}:
        raise LifecycleError("malformed_snapshot")
    if raw["source"] != source or type(raw["revision"]) is not int or raw["revision"] < 0:
        raise LifecycleError("untrusted_or_unordered_snapshot")
    if not isinstance(raw["kind"], str) or raw["kind"] not in {"snapshot", "events"}:
        raise LifecycleError("malformed_snapshot")
    if raw["complete"] is not True or raw["next_cursor"] is not None:
        raise LifecycleError("incomplete_snapshot")
    if not isinstance(raw["bots"], list) or len(raw["bots"]) > 10000:
        raise LifecycleError("malformed_snapshot")
    seen = set()
    for bot in raw["bots"]:
        if not isinstance(bot, dict) or set(bot) != {"uuid", "name", "revision", "status", "eligible"}:
            raise LifecycleError("malformed_bot")
        try:
            canonical = str(UUID(bot["uuid"]))
        except (ValueError, TypeError, AttributeError):
            raise LifecycleError("invalid_uuid") from None
        if canonical != bot["uuid"] or canonical in seen:
            raise LifecycleError("duplicate_or_noncanonical_uuid")
        seen.add(canonical)
        if (not isinstance(bot["name"], str) or not bot["name"].strip() or len(bot["name"]) > 256
                or type(bot["revision"]) is not int or not 0 <= bot["revision"] <= raw["revision"]
                or not isinstance(bot["status"], str) or bot["status"] not in {"enabled", "disabled", "deleted"}
                or type(bot["eligible"]) is not bool):
            raise LifecycleError("malformed_bot")
    return raw


class GrokLifecycle:
    """Host-bound source.fetch(), provisioner.ensure(), publisher.publish().

    Source revisions must be native authoritative ordering, never timestamps
    invented from a directory scan. ensure(uuid, actor_id, generation, name)
    must idempotently install a distinct protected credential and native shared
    instructions. Its receipt contains token_sha256, delivery_address,
    protected=True and instructions_installed=True. A missing piece keeps
    the bot disabled. Addresses must be distinct native delivery mailboxes.
    Publisher receives the whole applied-state candidate and must fail closed.
    """

    def __init__(self, store, *, source_id="grok", source=None, provisioner=None, publisher=None):
        self.store, self.source_id = store, source_id
        self.source, self.provisioner, self.publisher = source, provisioner, publisher
        self._lock = threading.RLock()
        self._key = "grok-lifecycle:" + source_id

    def _load(self):
        row = self.store.fetchone("SELECT cursor FROM sync_state WHERE name=?", (self._key,))
        return json.loads(row["cursor"]) if row and row["cursor"] else {
            "revision": -1, "digest": None, "bots": {}, "last_success_at": None,
            "status": "unconfigured", "failures": [],
        }

    def _save(self, state):
        with self.store.transaction() as db:
            db.execute("INSERT INTO sync_state(name,cursor,last_success_at) VALUES(?,?,?) "
                       "ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,last_success_at=excluded.last_success_at",
                       (self._key, json.dumps(state, sort_keys=True), state["last_success_at"]))
            self.store.append_event(db, request_id=None, operation_id=None, actor_id="system:grok-lifecycle",
                                    event_type="grok_lifecycle_reconciliation", detail={
                                        "status": state["status"], "revision": state["revision"],
                                        "failures": state["failures"], "bot_count": len(state["bots"]),
                                    })

    def status(self):
        with self._lock:
            state = self._load()
            return {"source": self.source_id, "status": state["status"],
                    "revision": state["revision"], "last_success_at": state["last_success_at"],
                    "failures": state["failures"], "integration_configured": all(
                        value is not None for value in (self.source, self.provisioner, self.publisher)),
                    "bots": [{k: v for k, v in bot.items() if k not in {"token_sha256", "revoked_hashes"}}
                             for bot in state["bots"].values()]}

    def reconcile(self):
        with self._lock:
            state = self._load()
            if self.source is None:
                return self._failure(state, "registry_adapter_unavailable")
            try:
                raw = self.source.fetch()
            except Exception:
                # Never log exceptions that can contain credentials/URLs.
                return self._failure(state, "registry_unavailable")
            try:
                snapshot = validate_snapshot(raw, self.source_id)
            except LifecycleError as exc:
                return self._failure(state, str(exc))
            digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            revision = snapshot["revision"]
            if revision < state["revision"]:
                return self._failure(state, "stale_snapshot")
            if revision == state["revision"] and digest != state["digest"]:
                return self._failure(state, "revision_conflict")
            # Even equal snapshots rerun idempotent publication, repairing missed
            # file/runtime changes without recreating a credential or native install.
            candidate = json.loads(json.dumps(state))
            candidate.update(revision=revision, digest=digest, status="pending", failures=[])
            seen = set()
            for item in snapshot["bots"]:
                uid = item["uuid"]
                seen.add(uid)
                prior = candidate["bots"].get(uid)
                if prior and item["revision"] < prior["revision"]:
                    continue
                if prior and prior["registry_status"] == "deleted":
                    continue  # UUID tombstones never inherit a replacement's name.
                if prior and item["revision"] == prior["revision"]:
                    if any(item[k] != prior[k] for k in ("name", "eligible")) or item["status"] != prior["registry_status"]:
                        return self._failure(state, "bot_revision_conflict")
                    continue
                bot = dict(prior or {"actor_id": "grok-" + uid, "generation": 0, "revoked_hashes": []})
                bot.update(uuid=uid, name=item["name"], revision=item["revision"],
                           eligible=item["eligible"], registry_status=item["status"])
                desired = item["status"] == "enabled" and item["eligible"]
                if not desired:
                    if bot.get("token_sha256"):
                        bot["revoked_hashes"] = sorted(set(bot["revoked_hashes"] + [bot.pop("token_sha256")]))
                    bot["state"] = "inactive"
                elif not prior or prior["state"] != "ready":
                    if not prior or prior["state"] == "inactive":
                        bot["generation"] += 1
                    bot["state"] = "pending"
                candidate["bots"][uid] = bot
            if snapshot["kind"] == "snapshot":
                for uid, bot in candidate["bots"].items():
                    if uid not in seen and bot["registry_status"] != "deleted":
                        bot.update(state="inactive", registry_status="deleted", revision=revision)
                        if bot.get("token_sha256"):
                            bot["revoked_hashes"] = sorted(set(bot["revoked_hashes"] + [bot.pop("token_sha256")]))
            # Journal desired inactive/pending state before external publication.
            self._save(candidate)
            if self.publisher is None:
                return self._failure(candidate, "runtime_publisher_unavailable")
            try:
                self.publisher.publish(candidate["bots"])
            except Exception:
                return self._failure(candidate, "policy_publication_failed")
            for bot in candidate["bots"].values():
                if bot["state"] != "pending":
                    continue
                if self.provisioner is None:
                    candidate["failures"].append({"uuid": bot["uuid"], "code": "native_provisioner_unavailable"})
                    continue
                try:
                    receipt = self.provisioner.ensure(bot["uuid"], bot["actor_id"], bot["generation"], bot["name"])
                    token_hash = receipt["token_sha256"]
                    address = receipt.get("delivery_address")
                    addresses = {b.get("delivery_address", "").casefold()
                                 for b in candidate["bots"].values() if b["uuid"] != bot["uuid"]}
                    used = {b.get("token_sha256") for b in candidate["bots"].values()}
                    used.update(h for b in candidate["bots"].values() for h in b["revoked_hashes"])
                    if (receipt.get("protected") is not True or receipt.get("instructions_installed") is not True
                            or not isinstance(token_hash, str) or len(token_hash) != 64
                            or any(c not in "0123456789abcdef" for c in token_hash)
                            or token_hash in used or token_hash == "0" * 64
                            or not isinstance(address, str) or len(address) > 254
                            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", address)
                            or address.casefold() in addresses):
                        raise LifecycleError("invalid_provisioning_receipt")
                    bot.update(token_sha256=token_hash, delivery_address=address,
                               state="ready", instructions_installed=True)
                except Exception:
                    candidate["failures"].append({"uuid": bot["uuid"], "code": "native_provisioning_failed"})
            try:
                self.publisher.publish(candidate["bots"])
            except Exception:
                # Publisher contract keeps the failed candidate disabled. Persist
                # installed credentials so the next attempt need not mint others.
                candidate["status"] = "publication_failed"
                candidate["failures"].append({"code": "policy_publication_failed"})
                self._save(candidate)
                return self.status()
            candidate["status"] = "pending" if candidate["failures"] else "synchronized"
            if not candidate["failures"]:
                candidate["last_success_at"] = self.store.now().isoformat()
            self._save(candidate)
            return self.status()

    def _failure(self, state, code):
        state["status"] = "stale" if state["last_success_at"] else "unavailable"
        state["failures"] = [{"code": code}]
        self._save(state)
        return self.status()
