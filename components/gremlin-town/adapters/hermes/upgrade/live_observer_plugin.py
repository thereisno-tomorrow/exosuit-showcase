"""Opt-in live upgrade observer: bounded schema metrics and tool names.

No prompts, arguments, results, credentials or tool schema bodies are logged.
It never changes a request, retries a call or performs any live repair.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace


def append(row):
    home = Path(os.environ["HERMES_HOME"])
    marker_path = home / "upgrade-observation.json"
    if marker_path.is_symlink():
        raise RuntimeError("upgrade observer refuses a symlink binding")
    marker = json.loads(marker_path.read_text())
    if marker.get("container") != "hermes-wa" or not marker.get("started_at") or marker.get("enabled") is not True:
        raise RuntimeError("upgrade observer requires explicit live start binding")
    row.update(at=datetime.now(UTC).isoformat(), container_name="hermes-wa", container_started_at=marker["started_at"])
    path = home / "upgrade-observer.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise RuntimeError("upgrade observer log has a foreign owner")
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)


def tool_metrics(tools):
    wire = json.dumps(tools, ensure_ascii=False)
    serialized = [json.dumps(t, ensure_ascii=False) for t in tools]
    names = [(t.get("function") or t).get("name", "<unnamed>") for t in tools]
    return {"tool_count": len(tools), "tool_names": names, "total_chars": len(wire),
            "largest_chars": max(map(len, serialized), default=0),
            "schemas_sha256": hashlib.sha256(wire.encode()).hexdigest(),
            "within_budget": len(wire) <= 63_000 and max(map(len, serialized), default=0) <= 12_000}


def execution(**kwargs):
    from agent.codex_runtime import _sanitize_consumer_codex_request
    payload = kwargs["request"]
    # Execution sees the effective request after all request-shaping middleware.
    # Project the same subscription sanitizer without changing the payload.
    wire = _sanitize_consumer_codex_request(SimpleNamespace(_is_codex_backend=lambda: True), dict(payload))
    try:
        append({"event": "llm_execution", "provider": kwargs.get("provider"),
                "platform": kwargs.get("platform"), "model": payload.get("model"), **tool_metrics(wire.get("tools") or [])})
    except Exception:
        import logging
        logging.getLogger(__name__).warning("upgrade request metrics unavailable; request continues unchanged")
    return kwargs["next_call"](payload)


def tool_request(**kwargs):
    row = {"event": "tool_request", "tool_name": kwargs.get("tool_name")}
    if kwargs.get("tool_name") == "gremlin_town_more_tools":
        args = kwargs.get("args") or {}
        row["front_action"] = args.get("action") if args.get("action") in ("list", "describe", "call") else "<invalid>"
        row["front_target"] = args.get("name") if args.get("name") in ("bc_weekly", "cronjob_manage") else "<other>"
    try:
        append(row)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('upgrade tool metrics unavailable; tool request continues unchanged')
    return None


def register(ctx):
    ctx.register_middleware("llm_execution", execution)
    ctx.register_middleware("tool_request", tool_request)
