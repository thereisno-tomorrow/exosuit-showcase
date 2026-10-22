"""Shared fixtures for the adapter tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def hermes_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Every adapter test gets its own $HERMES_HOME, so Diablo's plugin writes its write journal
    (speed-06) into a throwaway folder, never into the repository."""

    home = tmp_path_factory.mktemp("hermes-home")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
