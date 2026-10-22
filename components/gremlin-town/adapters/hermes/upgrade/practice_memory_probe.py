"""Installed provider rehearsal using only the isolated HTTPS fixture.

Run inside the candidate practice container. No model, live records or secrets
are emitted. Idle uses the unchanged thirty-minute timer, never a fake clock.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path


def probe(mode: str):
    home = Path(os.environ["HERMES_HOME"])
    if home != Path("/opt/data") or json.loads((home / "practice-only.json").read_text()).get("practice_only") is not True:
        raise ValueError("explicit container practice home required")
    folder = home / "plugins/brain"
    container = os.environ["PRACTICE_CONTAINER_NAME"]
    start = json.loads((home / "practice-starts.json").read_text())[container]
    spec = importlib.util.spec_from_file_location("practice_brain", folder / "__init__.py", submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    providers = []
    class Collector:
        def register_memory_provider(self, provider):
            providers.append(provider)
    module.register(Collector())
    provider, = providers
    from agent.memory_provider import MemoryProvider
    assert isinstance(provider, MemoryProvider)
    provider.initialize("practice-provider-" + hashlib.sha256(start.encode()).hexdigest()[:16], hermes_home=str(home))
    messages = [{"role": "assistant", "tool_calls": [{"function": {
        "name": "bc_continuity_start", "arguments": json.dumps({"session_id": "practice-provider-note"})}}]}]
    began = datetime.now(UTC).isoformat()
    outbox = home / "brain-outbox.jsonl"
    if mode == "queue":
        assert not outbox.exists() or not outbox.read_text().strip(), "unrelated outbox entries refused"
        assert provider.on_pre_compress(messages) == ""
        limit = time.monotonic() + 15
        while time.monotonic() < limit and not (outbox.exists() and outbox.read_text().strip()):
            time.sleep(.1)
        rows = [json.loads(line) for line in outbox.read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["note"] == "practice-provider-note"
        result = {"queued": True, "operation_id": rows[0]["operation_id"]}
    else:
        assert outbox.exists() and outbox.read_text().strip(), "queued marker required"
        queued = json.loads(outbox.read_text().splitlines()[0])["operation_id"]
        block = provider.prefetch("Practice provider rehearsal")
        assert "not reachable" not in block.casefold()
        limit = time.monotonic() + 15
        while time.monotonic() < limit and outbox.exists() and outbox.read_text().strip():
            time.sleep(.1)
        assert not outbox.exists() or not outbox.read_text().strip()
        provider.sync_turn("Practice provider rehearsal", "Fixture only", messages=messages)
        initial = {"fixture_only": True, "at": began, "replayed_operation_id": queued,
                   "native_provider_contract": True, "natural_idle_started": True,
                   "container": container, "started_at": start}
        write(home / "practice-memory-replay.json", initial)
        # The real provider watcher checks once a minute after 1800 seconds.
        time.sleep(1870)
        assert provider._idle_marked is True
        result = {"natural_idle_elapsed_seconds": 1870, "idle_marked": True,
                  "replayed_operation_id": queued}
    provider.shutdown()
    result.update(fixture_only=True, at=datetime.now(UTC).isoformat(), began_at=began,
                  native_provider_contract=True, mode=mode, container=container, started_at=start)
    write(home / ("practice-memory-" + mode + ".json"), result)
    return result


def write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        raw = json.dumps(value, indent=2).encode()
        assert os.write(descriptor, raw) == len(raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("queue", "watch-idle"))
    print(json.dumps(probe(parser.parse_args().mode)))
