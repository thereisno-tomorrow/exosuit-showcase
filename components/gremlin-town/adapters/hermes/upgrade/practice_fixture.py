"""Private, persistent HTTP fixture for Hermes rehearsal, never a Brain writer.

This deliberately simulates the remote service contract. It proves the native
Hermes/plugin/HTTPS path and independent fixture readback, not Brain Core's
production owner policy or GitHub publication. Unsupported operations fail.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
import ssl
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FixtureError(ValueError):
    pass


class FixtureStore:
    def __init__(self, root: Path):
        if root.is_symlink():
            raise FixtureError("fixture root symlink refused")
        root = root.resolve()
        marker = root / "fixture-only.json"
        if root.is_symlink() or not marker.is_file() or marker.is_symlink():
            raise FixtureError("explicit fixture-only marker required")
        if json.loads(marker.read_text()) != {"fixture_only": True}:
            raise FixtureError("invalid fixture marker")
        self.path = root / "fixture.sqlite3"
        if self.path.is_symlink():
            raise FixtureError("fixture database symlink refused")
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls(seq INTEGER PRIMARY KEY, at TEXT NOT NULL, operation TEXT NOT NULL, outcome TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS review_answers(seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS continuity_markers(id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, body TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def call(self, operation: str, args: dict):
        # Never accept owner/project selection or publication locations here.
        if args.get("scope", "personal") != "personal" or "project_id" in args:
            raise FixtureError("only personal fixture scope is available")
        with self.connect() as db:
            if operation == "memory.save":
                words = " ".join(str(args.get(k, "")) for k in ("title", "body", "words"))
                if not any(w in words.casefold() for w in ("practice", "fixture")):
                    raise FixtureError("save must explicitly name practice or fixture data")
                key = args.get("operation_id")
                if not isinstance(key, str) or not key:
                    raise FixtureError("operation_id required")
                payload_hash = hashlib.sha256(encoded(args).encode()).hexdigest()
                old = db.execute("SELECT payload_hash,receipt FROM operations WHERE id=?", (key,)).fetchone()
                if old:
                    if old[0] != payload_hash:
                        raise FixtureError("operation replay conflict")
                    result = {**json.loads(old[1]), "status": "duplicate"}
                else:
                    moment = datetime.now(UTC)
                    ident = "brn_" + moment.strftime("%Y%m%d_%H%M%S_") + payload_hash[:8]
                    row = {"record_id": ident, "kind": args.get("kind", "item"),
                           "title": args["title"], "body": args["body"],
                           "state": "open", "level": args.get("level", "accepted"),
                           "word": "task", "confirmed": False, "fixture_only": True,
                           "updated_at": moment.isoformat(), "expected_sha256": payload_hash}
                    db.execute("INSERT INTO records VALUES (?,?)", (ident, encoded(row)))
                    result = {"operation_id": key, "status": "accepted", "record_id": ident,
                              "remote_observed": True, "fixture_only": True,
                              "owner_locator": "fixture://brain-home", "observed_version": "fixture-" + payload_hash,
                              "record_sha256": payload_hash}
                    db.execute("INSERT INTO operations VALUES (?,?,?)", (key, payload_hash, encoded(result)))
            elif operation in ("memory.list", "memory.search", "memory.read", "weekly"):
                rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM records ORDER BY id")]
                if operation == "memory.search":
                    terms = str(args.get("query", "")).casefold().split()
                    rows = [r for r in rows if any(t in (r["title"] + " " + r["body"]).casefold() for t in terms)]
                elif operation == "memory.read":
                    rows = [r for r in rows if r["record_id"] == args.get("record_id")]
                    if not rows:
                        raise FixtureError("fixture record not found")
                elif operation == "memory.list" and args.get("kind") == "preference":
                    rows = [r for r in rows if r["kind"] == "preference"]
                rows = rows[:min(int(args.get("limit", 20)), 50)]
                # Explicit fixture projection for the installed reader contract.
                # This is synthetic service output, not production work inference.
                rows = [{**r, "now": {"is": "task", "status": "open", "next": r["title"],
                                      "progress": "Synthetic fixture item", "dates": {}}} for r in rows]
                result = {"fixture_only": True, "version": "fixture-only", "records": rows,
                          "items": rows, "matches": rows}
                if operation == "weekly":
                    result.update(period="practice week", open_items=rows)
                if operation == "memory.read":
                    result.update(rows[0])
            elif operation == "continuity.start":
                if (args.get("session_id") != "practice-provider-note"
                        or not str(args.get("operation_id", "")).startswith("practice-")
                        or "practice" not in str(args.get("intent", "")).casefold()):
                    raise FixtureError("only the fixed practice continuity note may be started")
                key = args["operation_id"]
                payload_hash = hashlib.sha256(encoded(args).encode()).hexdigest()
                old = db.execute("SELECT payload_hash,receipt FROM operations WHERE id=?", (key,)).fetchone()
                if old and old[0] != payload_hash:
                    raise FixtureError("operation replay conflict")
                result = {"session_id": "practice-provider-note", "operation_id": key,
                          "started": True, "status": "duplicate" if old else "accepted", "fixture_only": True}
                if not old:
                    db.execute("INSERT INTO operations VALUES (?,?,?)", (key, payload_hash, encoded(result)))
            elif operation == "continuity.list":
                notes = [{"session_id": "practice-provider-note", "state": "open",
                          "intent": "Practice memory-provider rehearsal",
                          "next_step": "Check synthetic compression and idle markers"}]
                result = {"items": notes, "notes": notes, "fixture_only": True, "version": "fixture-only"}
            elif operation == "continuity.append":
                if (args.get("session_id") != "practice-provider-note" or args.get("event") != "effect"
                        or not str(args.get("operation_id", "")).startswith(("hermes-compressed-", "hermes-idle-"))
                        or not str(args.get("value", "")).startswith(("compressed:", "idle:"))):
                    raise FixtureError("only provider markers on the fixed practice note are available")
                key = args["operation_id"]
                payload_hash = hashlib.sha256(encoded(args).encode()).hexdigest()
                old = db.execute("SELECT payload_hash FROM continuity_markers WHERE id=?", (key,)).fetchone()
                if old and old[0] != payload_hash:
                    raise FixtureError("operation replay conflict")
                if not old:
                    db.execute("INSERT INTO continuity_markers VALUES (?,?,?)", (key, payload_hash, encoded(args)))
                result = {"changed": True, "status": "duplicate" if old else "accepted",
                          "operation_id": key, "session_id": "practice-provider-note", "fixture_only": True}
            elif operation == "review.respond":
                refs = args.get("item_refs", [])
                if not refs or args.get("response") not in ("picked_today", "accepted", "not_now", "parked", "dropped", "corrected", "no_answer"):
                    raise FixtureError("explicit fixture references and answer required")
                known = {r[0] for r in db.execute("SELECT id FROM records")}
                if not set(refs) <= known:
                    raise FixtureError("review may only name stored fixture records")
                db.execute("INSERT INTO review_answers(body) VALUES (?)", (encoded(args),))
                result = {"recorded": True, "item_refs": refs, "response": args["response"], "fixture_only": True}
            else:
                raise FixtureError("operation inactive in practice fixture")
            db.execute("INSERT INTO calls(at,operation,outcome) VALUES (?,?,?)",
                       (datetime.now(UTC).isoformat(), operation, "accepted"))
        # Read a save independently after the committing connection closed.
        if operation == "memory.save":
            with self.connect() as reader:
                if not reader.execute("SELECT 1 FROM records WHERE id=?", (result["record_id"],)).fetchone():
                    raise FixtureError("independent fixture readback failed")
        return result


def serve(root: Path, port: int):
    from jsonschema import Draft202012Validator

    store = FixtureStore(root)
    schemas = json.loads((root / "schemas.json").read_text())
    token = (root / "access.token").read_text().strip()
    if not token:
        raise FixtureError("practice-only access token missing")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log requests, bodies or authorization values.

        def reply(self, status, obj):
            body = encoded(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self.reply(401, {"error": {"code": "UNAUTHORIZED", "message": "test identity required"}})
            operation = self.path.removeprefix("/v1/brain/") if self.path.startswith("/v1/brain/") else {
                "/v1/review/responses": "review.respond"}.get(self.path)
            if operation not in schemas:
                return self.reply(404, {"error": {"code": "INACTIVE_PRACTICE", "message": "fixture operation inactive"}})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 100_000:
                    raise FixtureError("bounded body required")
                args = json.loads(self.rfile.read(size))
                Draft202012Validator(schemas[operation]).validate(args)
                result = store.call(operation, args)
            except Exception:
                return self.reply(422, {"error": {"code": "FIXTURE_REFUSED", "message": "invalid or inactive fixture call"}})
            self.reply(200, result)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "server.crt", root / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9443)
    args = parser.parse_args()
    serve(args.root, args.port)
