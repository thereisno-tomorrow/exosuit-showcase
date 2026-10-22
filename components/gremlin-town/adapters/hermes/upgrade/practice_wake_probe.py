"""Fixture-only signed wake wire, invalid/stale rejection and duplicate check.

Runs inside the private practice gateway. Acceptance is not completion; the hub
observer must independently read its native session and remote reply receipts.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def probe():
    home = Path(os.environ["HERMES_HOME"])
    if home != Path("/opt/data") or json.loads((home / "practice-only.json").read_text()).get("practice_only") is not True:
        raise ValueError("explicit private practice home required")
    name = os.environ["PRACTICE_CONTAINER_NAME"]
    start = json.loads((home / "practice-starts.json").read_text())[name]
    path = home / "practice-signed-wake.json"
    if path.exists() or path.is_symlink():
        raise ValueError("refusing duplicate wake rehearsal")
    key = home / "practice-secrets/wake-signing.key"
    if key.is_symlink() or key.stat().st_uid != os.geteuid() or key.stat().st_mode & 0o777 != 0o600:
        raise ValueError("private practice key custody required")
    secret = key.read_bytes().strip()
    delivery = "practice-wake-" + hashlib.sha256(start.encode()).hexdigest()[:16]
    body = json.dumps({"request_id": delivery, "delivery_id": delivery}, sort_keys=True, separators=(",", ":")).encode()
    def post(timestamp, signature):
        headers = {"Content-Type": "application/json", "X-Request-ID": delivery,
                   "Idempotency-Key": delivery, "X-GitHub-Event": "gt.request.ready",
                   "X-Webhook-Timestamp": str(timestamp), "X-Webhook-Signature-V2": signature}
        request = urllib.request.Request("http://127.0.0.1:8644/webhooks/gremlin-town-wake", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read(8192))
        except urllib.error.HTTPError as error:
            return error.code, {}
    now = int(time.time())
    def sign(stamp):
        return hmac.new(secret, str(stamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    bad, _ = post(now, "0" * 64)
    stale, _ = post(now - 3600, sign(now - 3600))
    accepted, result = post(now, sign(now))
    duplicate, repeated = post(now, sign(now))
    if not (bad == 401 and stale == 401 and accepted == 202 and result.get("status") == "accepted"
            and duplicate == 200 and repeated.get("status") == "duplicate"):
        raise RuntimeError("practice signed wake contract refused; private diagnostics suppressed")
    receipt = {"fixture_only": True, "container": name, "started_at": start, "at": datetime.now(UTC).isoformat(),
               "delivery_id": delivery, "bad_signature_rejected": True, "stale_signature_rejected": True,
               "native_accepted": True, "duplicate_suppressed": True, "agent_completion_pending": True}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        raw = json.dumps(receipt, indent=2).encode()
        if os.write(descriptor, raw) != len(raw):
            raise OSError("incomplete wake receipt")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return receipt


if __name__ == "__main__":
    print(json.dumps(probe()))
