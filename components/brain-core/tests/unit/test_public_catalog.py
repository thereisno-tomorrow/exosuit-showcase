"""The showcase's synthetic policy cannot discover or write personal roots."""
import hashlib
from pathlib import Path

from brain_core.durability import CATALOG_BYTES, CATALOG_PATH, CATALOG_SHA256, SharedDurabilityWriter
from test_durability import request


def test_bundled_synthetic_catalog_matches_its_pinned_identity():
    raw = Path(CATALOG_PATH).read_bytes()
    assert len(raw) == CATALOG_BYTES
    assert hashlib.sha256(raw).hexdigest().upper() == CATALOG_SHA256


def test_owner_roots_require_explicit_binding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    receipt = SharedDurabilityWriter().write(request())
    assert receipt.status == "failure"
    assert receipt.refusal_code == "owner_root_unavailable"
    assert not list(tmp_path.iterdir())
