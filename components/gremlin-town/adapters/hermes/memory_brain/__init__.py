"""Hermes memory provider ``brain``: Brain Home's read side for Diablo.

Hermes lists this folder as a memory provider because this file calls
``register_memory_provider``; ``memory.provider: brain`` in config.yaml turns it on.
The behaviour is in ``provider.py``. Spec: docs/product/steps/g-damien-hooks.md.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any, Callable

from .provider import BrainProvider

__all__ = ["register"]


def register(ctx: Any) -> None:
    """Called by Hermes's memory-provider loader with its provider collector."""

    # Present only inside Hermes. Imported here, not at the top, so the package also
    # loads in the repository's tests and install checks.
    from agent.memory_provider import MemoryProvider

    try:
        from agent.memory_provider import spawn_context_thread
    except ImportError:  # Hermes before the helper existed, such as 0.20.5 on hermes-wa (26 Sep 2026)
        spawn_context_thread = None

    class BrainMemoryProvider(BrainProvider, MemoryProvider):
        """Brain Home's read side, as the Hermes memory provider ``brain``."""

    def spawn(target: Callable[[], None], *, name: str) -> None:
        # Hermes's helper keeps the caller's context (profile, session) in the new thread;
        # without it, the same context is carried over by hand.
        if spawn_context_thread is not None:
            spawn_context_thread(target, name=name).start()
        else:
            threading.Thread(target=contextvars.copy_context().run, args=(target,), name=name, daemon=True).start()

    ctx.register_memory_provider(BrainMemoryProvider(spawn=spawn))
