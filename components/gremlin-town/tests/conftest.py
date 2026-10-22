"""Shared work-model fixture owned by Brain Core; no duplicate migration logic."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def moved_over():
    path = Path(__file__).resolve().parents[2] / "brain-core/tests/fixtures/work_model/moved_over.py"
    spec = importlib.util.spec_from_file_location("shared_work_model_moved_over", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.moved_over_records()
