"""W15 retires agent-written first steps; Nic supplies the optional action."""
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "adapters/hermes/skills"

def _text(name):
    return " ".join((SKILLS / name / "SKILL.md").read_text(encoding="utf-8").split())


def test_skills_do_not_instruct_diablo_to_suggest_or_write_a_first_step():
    for name in ("gremlin-town", "gremlin-town-review", "next-action"):
        text = _text(name)
        assert "suggest one concrete first step" not in text
        assert "Diablo suggests:" not in text
        assert "Write the next action" not in text
    assert "Nic writes next actions; never suggest a first step for him" in _text("gremlin-town-review")
    assert "never suggest a tiny first step" in _text("next-action")


def test_nothing_next_asks_nic_and_keeps_his_named_actions_as_said():
    text = _text("gremlin-town-review")
    assert '"Done, or what\'s next?"' in text
    assert "parts or next action are saved as he said them" in text
    assert "Never fill an empty next line" in text
    assert "recorded only when Nic names it as a next action" in _text("next-action")
