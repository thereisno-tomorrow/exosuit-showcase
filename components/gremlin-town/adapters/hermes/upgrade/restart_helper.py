"""Reconstructed mega-02 single-restart coordinator and read-only postcheck.

No launcher is guessed. The hub-specific backend must provide observations from
the inspected launcher and current start. Real use requires Nic's upgrade word.
This module does not install files, repair failed checks, or retry a restart.
"""
from datetime import datetime, timezone
from typing import Protocol

try:
    from .restart_guard import eligible, connections
except ImportError:
    from restart_guard import eligible, connections


class Backend(Protocol):
    def observe(self) -> dict: ...
    def verify_preimages(self) -> bool: ...
    def log_offset(self) -> int: ...
    def restart(self) -> None: ...
    def started_at(self) -> datetime: ...
    def new_log_lines(self, offset: int) -> list[dict]: ...
    def verify_installed(self) -> bool: ...


def postcheck(backend: Backend, *, previous_start: datetime, offset: int,
              required_connections: dict[str, str]) -> dict:
    current_start = backend.started_at()
    if current_start.tzinfo is None or previous_start.tzinfo is None or current_start <= previous_start:
        raise RuntimeError("NEW_START_NOT_OBSERVED")
    markers = connections(backend.new_log_lines(offset), current_start, required_connections)
    installed = backend.verify_installed()
    return {"status": "pass" if installed and all(markers.values()) else "fail",
            "started_at": current_start.isoformat(), "installed": installed, "connections": markers}


def restart_once(backend: Backend, *, required_connections: dict[str, str],
                 clock=lambda: datetime.now(timezone.utc)) -> dict:
    previous_start = backend.started_at()
    offset = backend.log_offset()
    # Observe after every preparatory read, immediately before mutation.
    if not backend.verify_preimages() or not eligible(backend.observe(), clock()):
        raise RuntimeError("RESTART_PREFLIGHT_REFUSED")
    backend.restart()  # exactly one call; any exception propagates, never retries
    return postcheck(backend, previous_start=previous_start, offset=offset,
                     required_connections=required_connections)
