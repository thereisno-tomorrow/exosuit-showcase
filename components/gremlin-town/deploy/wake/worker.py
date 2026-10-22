"""Optional GT wake sidecar. Reads hub metadata; never mutates hub work/state.

Run with the installed GT release's Python. Separate persistent state prevents
re-sending an accepted notification after restart. HTTP acceptance is NOT pickup.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import sqlite3
import time
import tomllib
from datetime import datetime, UTC
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from gremlin_town.domain.errors import GremlinTownError
from gremlin_town.runtime import _load_participants

LOG = logging.getLogger("gt-wake")
SQL = """
SELECT r.request_id,r.project,r.requester_id,r.recipient_id,r.sensitivity,
       r.created_at,r.revision,o.operation_id
FROM requests r JOIN operations o ON o.request_id=r.request_id
JOIN message_links m ON m.operation_id=o.operation_id
WHERE r.mode='action_requested' AND r.work_state='requested'
AND r.recovery_hold=0 AND o.recovery_hold=0 AND o.recovery_required=0
AND o.message_kind='request' AND o.operation_state='accepted'
AND m.exact_link_verified=1 AND m.trust_label='authenticated_operation'
ORDER BY r.created_at,r.request_id
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_endpoint(target):
    url = target["url"]
    p = urlsplit(url)
    if p.username or p.password or p.fragment or p.query or not p.hostname:
        raise ValueError("wake URL must not contain credentials, query or fragment")
    if target["kind"] == "hermes":
        # Explicit numeric private bridge/loopback address; no DNS or redirects.
        ip = ipaddress.ip_address(p.hostname)
        if p.scheme != "http" or not (ip.is_private or ip.is_loopback) or ip.is_link_local or ip.is_unspecified:
            raise ValueError("Hermes webhook must use a private numeric address")
        if p.path != "/webhooks/gremlin-town-wake":
            raise ValueError("unexpected Hermes route")
    elif target["kind"] == "grok":
        if p.scheme != "https" or p.hostname != target["allowed_host"] or p.port not in (None,443):
            raise ValueError("Grok webhook must match the reviewed HTTPS origin")
    else:
        raise ValueError("unknown wake target kind")
    secret = Path(target["secret_file"])
    if not secret.is_absolute():
        raise ValueError("secret_file must be absolute")


def send(target, payload, delivery_id, now):
    validate_endpoint(target)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    secret = Path(target["secret_file"]).read_text().strip()
    if len(secret) < 24:
        raise ValueError("wake secret is missing or too short")
    headers = {"Content-Type": "application/json", "X-Request-ID": delivery_id,
               "Idempotency-Key": delivery_id, "X-GitHub-Event": "gt.request.ready"}
    if target["kind"] == "hermes":
        timestamp = str(int(now))
        headers["X-Webhook-Timestamp"] = timestamp
        headers["X-Webhook-Signature-V2"] = hmac.new(
            secret.encode(), timestamp.encode()+b"."+body, hashlib.sha256).hexdigest()
    else:
        # Installed only after confirming the header in Grok's routine panel.
        header = target["auth_header"]
        if header.lower() not in ("authorization", "x-api-key", "x-webhook-key"):
            raise ValueError("unreviewed Grok auth header")
        headers[header] = target.get("auth_prefix", "") + secret
    request = Request(target["url"], data=body, headers=headers, method="POST")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            status = response.status
            raw = response.read(8193)
        if len(raw) > 8192:
            return "failed_response_size"
        if target["kind"] == "hermes":
            result = json.loads(raw)
            if status not in (200,202) or result.get("status") not in ("accepted","duplicate"):
                return "failed_response"
        elif status not in (200,201,202,204):
            return "failed_response"
        return "notified"
    except HTTPError as exc:
        # Never log the URL, response body, request headers or exception text.
        return "retry" if exc.code in (408,429) or exc.code >= 500 else f"failed_http_{exc.code}"
    except (URLError, TimeoutError, OSError):
        return "retry"
    except (ValueError, UnicodeError):
        return "failed_response"


class Worker:
    def __init__(self, config_path, *, sender=send, clock=time.time):
        self.config_path = Path(config_path)
        self.sender, self.clock = sender, clock
        self.config = tomllib.loads(self.config_path.read_text())
        if not self.config.get("enabled", False):
            raise ValueError("wake worker is disabled")
        self.cutoff = datetime.fromisoformat(self.config["activate_after"])
        if self.cutoff.tzinfo is None:
            raise ValueError("activation cutoff must include timezone")
        self.targets = {t["participant_id"]: t for t in self.config["target"] if t.get("enabled", False)}
        for target in self.targets.values():
            validate_endpoint(target)
        self.state = sqlite3.connect(self.config["state_database"], isolation_level=None)
        self.state.row_factory = sqlite3.Row
        self.state.execute("PRAGMA journal_mode=WAL")
        self.state.execute("""CREATE TABLE IF NOT EXISTS wakes(
            request_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, attempts INTEGER NOT NULL, next_at REAL NOT NULL,
            updated_at REAL NOT NULL)""")

    def eligible(self):
        runtime = tomllib.loads(Path(self.config["hub_config"]).read_text())
        recovery = runtime.get("recovery", {})
        if runtime["server"].get("operator_only", False) or any(
            recovery.get(k, True) is not True for k in ("normal_clients_allowed","outbound_dispatch_allowed")
        ):
            return []
        policy, _ = _load_participants(Path(runtime["participants"]["file"]))
        path = Path(runtime["server"]["database"]).resolve()
        db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (2, 3):
                raise RuntimeError("unsupported GT database schema; wake stopped")
            query = SQL
            if version == 3:
                query = SQL.replace("ORDER BY", """AND o.backend_binding=r.backend_binding
                AND m.request_id=r.request_id AND m.provider_message_id=o.provider_message_id
                AND m.provider_thread_id=o.provider_thread_id AND m.provider_thread_id=r.thread_id
                AND m.content_sha256=o.payload_sha256 AND m.actor_id=o.actor_id
                ORDER BY""")
            rows = db.execute(query).fetchall()
        finally:
            db.close()
        accepted = []
        for row in rows:
            if row["recipient_id"] not in self.targets:
                continue
            if datetime.fromisoformat(row["created_at"]) < self.cutoff and row["request_id"] not in self.config.get("include_existing", []):
                continue
            try:
                policy.require_route(row["project"],row["requester_id"],row["recipient_id"],row["sensitivity"])
            except GremlinTownError:
                continue
            accepted.append(dict(row))
        return accepted

    def tick(self):
        delivered = 0
        for row in self.eligible():
            now = self.clock()
            request_id = row["request_id"]
            delivery = "gt-wake-" + hashlib.sha256(request_id.encode()).hexdigest()[:32]
            self.state.execute("INSERT OR IGNORE INTO wakes VALUES(?,?, 'pending',0,0,?)",(request_id,delivery,now))
            # Durable claim before network I/O; competing processes cannot claim twice.
            cur = self.state.execute("""UPDATE wakes SET status='sending', attempts=attempts+1,
                next_at=?,updated_at=? WHERE request_id=? AND status IN ('pending','retry','sending')
                AND next_at<=? AND attempts<5""", (now+300,now,request_id,now))
            if cur.rowcount != 1:
                continue
            # Fresh state/policy check immediately before delivery. Recipient checks again.
            if not any(r["request_id"]==request_id for r in self.eligible()):
                self.state.execute("UPDATE wakes SET status='suppressed',updated_at=? WHERE request_id=?",(now,request_id))
                continue
            payload = {"request_id":request_id,"delivery_id":delivery}
            outcome = self.sender(self.targets[row["recipient_id"]],payload,delivery,now)
            attempt = self.state.execute("SELECT attempts FROM wakes WHERE request_id=?",(request_id,)).fetchone()[0]
            if outcome == "retry" and attempt >= 5:
                outcome = "failed_attempts"
            self.state.execute("UPDATE wakes SET status=?,next_at=?,updated_at=? WHERE request_id=?",(outcome,now+min(300,15*2**attempt),now,request_id))
            LOG.info("request=%s recipient=%s wake=%s attempt=%d",request_id,row["recipient_id"],outcome,attempt)
            delivered += 1
            if delivered >= 8:
                break
        return delivered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--inspect", action="store_true", help="read eligible request metadata only; no sends")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    worker = Worker(args.config)
    try:
        if args.inspect:
            print(json.dumps([{"request_id":r["request_id"],"recipient_id":r["recipient_id"]} for r in worker.eligible()]))
            return
        while True:
            worker.tick()
            if args.once:
                return
            time.sleep(10)
    finally:
        worker.state.close()


if __name__ == "__main__":
    main()
