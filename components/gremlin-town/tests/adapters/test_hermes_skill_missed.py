"""W15 missed deadlines still require Nic's decision, date and promise safety."""
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "adapters/hermes/skills"

def _text(name):
    return " ".join((SKILLS / name / "SKILL.md").read_text(encoding="utf-8").split())


def test_missed_returns_until_nics_answer_changes_the_task():
    text = _text("gremlin-town-review")
    assert "A Missed line (it says how long) returns every morning until Nic's answer changes the task" in text
    assert 'recommit - ask "by when?" and set his `date`' in text
    assert "defer - park it or move the date" in text
    assert "drop - `end` with `ending: dropped` and his reason" in text
    assert "Anything else leaves it for tomorrow" in text
    assert 'ask "have they been told?"' in text


def test_did_it_happen_offers_done_move_drop_without_automatic_tick():
    text = _text("gremlin-town-review")
    assert "An appointment passed: done - `tick`, checked by his answer" in text
    assert 'else ask "when?" and send his date words' in text
    assert "Don't tick a passed time automatically" in text


def test_carried_over_is_once_and_never_missed():
    text = _text("gremlin-town-review")
    assert '"Today, another day, or drop?"' in text
    assert "A passed plan is said once, never missed" in text
    assert "Three carry-overs" in text
