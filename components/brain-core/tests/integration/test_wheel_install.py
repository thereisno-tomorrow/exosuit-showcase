from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path

import brain_core


def test_distribution_metadata_and_console_entrypoint_are_packaged() -> None:
    distribution = importlib.metadata.distribution("brain-core")
    assert distribution.version == brain_core.__version__ == "0.4.0"
    scripts = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    assert scripts["brain"] == "brain_core.cli:main"


def test_clean_wheel_job_imports_from_its_isolated_environment() -> None:
    if os.environ.get("BRAIN_WHEEL_TEST") != "1":
        return
    loaded = Path(brain_core.__file__).resolve()
    assert loaded.is_relative_to(Path(sys.prefix).resolve())
    assert "site-packages" in loaded.parts
