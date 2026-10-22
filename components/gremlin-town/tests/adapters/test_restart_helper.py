from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util
import sys
import pytest

HERE = Path(__file__).resolve().parents[2] / "adapters/hermes/upgrade"
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("tested_restart_helper", HERE / "restart_helper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
sys.path.remove(str(HERE))
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


class Backend:
    def __init__(self):
        self.restarts = 0
        self.active = 0
        self.good = True
        self.marker = NOW + timedelta(seconds=1)
    def observe(self):
        return {"container": "hermes-wa", "mount": "/srv/hermes-example", "observed_at": NOW.isoformat(),
                "active_turns": self.active, "active_jobs": 0, "ready": True}
    def verify_preimages(self): return self.good
    def log_offset(self): return 100
    def restart(self): self.restarts += 1
    def started_at(self): return NOW + timedelta(seconds=1) if self.restarts else NOW
    def new_log_lines(self, offset):
        assert offset == 100
        return [{"timestamp": self.marker.isoformat(), "message": "Discord connected"}]
    def verify_installed(self): return True


def test_one_restart_and_postcheck():
    b = Backend()
    assert helper.restart_once(b, clock=lambda: NOW, required_connections={"discord": "Discord connected"})["status"] == "pass"
    assert b.restarts == 1


@pytest.mark.parametrize("reason", ["busy", "changed"])
def test_failed_preflight_never_restarts(reason):
    b = Backend()
    if reason == "busy": b.active = 1
    else: b.good = False
    with pytest.raises(RuntimeError, match="RESTART_PREFLIGHT_REFUSED"):
        helper.restart_once(b, clock=lambda: NOW, required_connections={"discord": "Discord connected"})
    assert b.restarts == 0


def test_stale_marker_is_red_and_never_repaired():
    b = Backend(); b.marker = NOW - timedelta(minutes=1)
    result = helper.restart_once(b, clock=lambda: NOW, required_connections={"discord": "Discord connected"})
    assert result["status"] == "fail"
    assert b.restarts == 1


def test_restart_error_propagates_without_retry():
    b = Backend()
    def fail():
        b.restarts += 1
        raise RuntimeError("RESTART_FAILED")
    b.restart = fail
    with pytest.raises(RuntimeError, match="RESTART_FAILED"):
        helper.restart_once(b, clock=lambda: NOW, required_connections={"discord": "Discord connected"})
    assert b.restarts == 1


def test_postcheck_never_restarts():
    b = Backend(); b.restarts = 1
    assert helper.postcheck(b, previous_start=NOW, offset=100, required_connections={"discord": "Discord connected"})["status"] == "pass"
    assert b.restarts == 1
