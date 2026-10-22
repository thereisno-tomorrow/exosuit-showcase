"""Hermes standalone directory-plugin entrypoint."""

from typing import Any

from .plugin import _words_shadow_from_environment, register as _register


def register(ctx: Any) -> None:
    """Entry point called by Hermes's directory-plugin loader."""

    _register(ctx, words_shadow=_words_shadow_from_environment())


__all__ = ["register"]
