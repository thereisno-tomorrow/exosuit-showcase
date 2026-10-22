"""W15 stalled work asks Nic; no invented obstacle, action or cause."""
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "adapters/hermes/skills"

def _text(name):
    return " ".join((SKILLS / name / "SKILL.md").read_text(encoding="utf-8").split())


def test_stalled_asks_nic_for_the_obstacle_and_the_next_physical_thing():
    text = _text("gremlin-town-review")
    assert '"What\'s in the way? What\'s the very next physical thing?"' in text
    assert "Nic supplies the answer" in text
    assert "Save his named next action, or his park/drop decision" in text
    assert "don't invent a cause or a tiny step" in text


def test_stuck_mode_does_not_manufacture_an_if_then_or_infer_avoidance_from_age():
    text = _text("next-action")
    assert 'ask "What\'s in the way?" Nic supplies the answer' in text
    assert "never suggest a tiny first step or turn his answer into an if-then you wrote" in text
    assert "Age alone doesn't prove avoidance" in text
    assert "don't invent dates, people or causes to unblock him" in text
