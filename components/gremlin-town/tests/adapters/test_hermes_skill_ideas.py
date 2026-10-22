"""W12/W17 retire search reminders; W15 skills ask first."""
from pathlib import Path

from adapters.hermes import plugin
from tests.adapters.test_hermes_brain import server_tools

SKILLS = Path(__file__).resolve().parents[2] / "adapters/hermes/skills"
TOOL_SENTENCE = (
    "When a new topic comes up, search it for Nic's open ideas; mention each related one at most "
    "once per conversation, and it stays an idea unless he says so."
)

def _text(name):
    return " ".join((SKILLS / name / "SKILL.md").read_text(encoding="utf-8").split())


def test_skills_remove_unasked_topic_idea_reminders():
    for name in ("gremlin-town", "gremlin-town-review", "next-action"):
        text = _text(name)
        assert "mention each related one at most once per conversation" not in text
        assert "search it for Nic's open ideas" not in text
        assert "it stays an idea unless he says so" not in text
    assert "Ideas do not appear in recall and are never raised unasked" in _text("gremlin-town")


def test_friday_asks_before_listing_ideas_in_nics_words():
    text = _text("gremlin-town-review")
    assert "ask first whether he wants to go through ideas" in text
    assert "Only his yes lists them, each in his own words" in text
    assert "Never raise ideas unasked in an ordinary conversation" in text


def test_search_tool_text_removes_the_retired_rule_and_damiens_copy_matches_the_hub():
    hub = next(tool for tool in server_tools() if tool.name == "bc_memory_search")
    damien = next(d for d in plugin.BRAIN_TOOL_DEFINITIONS if d.name == "bc_memory_search")
    assert TOOL_SENTENCE not in hub.description
    assert TOOL_SENTENCE not in damien.description
    assert damien.description == hub.description


def test_retired_ideas_reminder_is_absent_from_actual_instruction_surfaces_after_w17():
    component = SKILLS.parents[2]
    paths = [component / "host_packs/shared/CONTRACT.md",
             component / "src/gremlin_town/mcp/server.py",
             *(SKILLS / name / "SKILL.md" for name in
               ("gremlin-town", "gremlin-town-review", "next-action"))]
    paths.extend((component / "host_packs").glob("*/HOST.md"))
    paths.extend((component / "host_packs").glob("*/GATEWAY.md"))
    texts = [" ".join(path.read_text(encoding="utf-8").split()) for path in paths]
    texts.extend(tool.description for tool in server_tools())
    texts.extend(tool.description for tool in plugin.BRAIN_TOOL_DEFINITIONS)
    for text in texts:
        assert "mention each related one at most once per conversation" not in text
        assert TOOL_SENTENCE not in text
