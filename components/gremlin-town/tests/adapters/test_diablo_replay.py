"""Offline replay sequencing, separate grading and cleanup gates."""
import importlib.util
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location(
    "diablo_replay", Path(__file__).resolve().parents[2] / "scripts/diablo_replay.py")
replay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = replay
spec.loader.exec_module(replay)


class FakeDriver:
    def __init__(self):
        self.chats = []
        self.cleaned = []

    def fresh_chat(self, **options):
        self.chats.append(options)
        return f"fixture-{len(self.chats)}"

    def run(self, session, prompt):
        return {"record_ids": [session], "agent_log_counts": {"skill_loads": 1, "model_calls": 2},
                "actual": prompt.id}

    def cleanup(self, ids):
        self.cleaned.append(ids)


def test_replay_has_23_synthetic_prompts_and_three_fresh_chats_per_condition():
    assert [p.id for p in replay.PROMPTS] == [*(f"R{i:02d}" for i in range(1, 16)),
                                           *(f"S{i:02d}" for i in range(1, 9))]
    assert next(p for p in replay.PROMPTS if p.id == "S03").text == "The workshop served excellent sandwiches today."
    driver = FakeDriver()
    results = replay.run_replay(driver, model="fixture-sol", day="2026-10-01",
                                judge=lambda p, row: row["actual"] == p.id)
    assert len(results) == len(driver.chats) == len(driver.cleaned) == 138
    assert all(row["correct"] and row["model_calls"] == 2 for row in results)
    assert {row["model"] for row in driver.chats} == {"fixture-sol"}
    assert {row["day"] for row in driver.chats} == {"2026-10-01"}
    assert {row["core_rules"] for row in driver.chats} == {True, False}


def test_failed_measurement_cleans_only_journal_ids_and_never_starts_next_chat():
    driver = FakeDriver()
    driver.run = lambda session, prompt: {"record_ids": ["fixture-written-id"]}
    with pytest.raises(ValueError, match="agent_log_counts"):
        replay.run_replay(driver, model="fixture", day="2026-10-01", judge=lambda p, row: True)
    assert driver.cleaned == [("fixture-written-id",)] and len(driver.chats) == 1


def test_cleanup_failure_stops_before_next_condition_or_prompt():
    driver = FakeDriver()
    def fail(ids):
        raise RuntimeError("fixture owner refused cleanup")
    driver.cleanup = fail
    with pytest.raises(RuntimeError, match="refused cleanup"):
        replay.run_replay(driver, model="fixture", day="2026-10-01", judge=lambda p, row: False)
    assert len(driver.chats) == 1


def test_cli_reads_fixture_only_and_missing_counters_fail_loudly(tmp_path, capsys):
    fixture = tmp_path / "fixture.json"
    fixture.write_text('[{"agent_log_counts":{"model_calls":2,"skill_loads":1}}]')
    assert replay.main(["--fixture", str(fixture)]) == 0
    assert '"model_calls": 2' in capsys.readouterr().out
    with pytest.raises(ValueError, match="model_calls"):
        replay.measurements({"agent_log_counts": {"skill_loads": 1}})
