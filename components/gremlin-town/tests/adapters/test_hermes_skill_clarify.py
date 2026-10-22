"""W15: supplied review text stays verbatim; clarification uses its references."""
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "adapters/hermes/skills"

def _text(name):
    return " ".join((SKILLS / name / "SKILL.md").read_text(encoding="utf-8").split())


def test_review_keeps_clarification_out_of_the_supplied_parts():
    text = _text("gremlin-town-review")
    assert "Never rewrite the review, insert suggestions into its lines" in text
    assert "Nic supplies parts and next actions" in text
    assert "Clear captures need no forced question" in text


def test_review_reads_the_request_for_numbers_with_recipient_filters_on_each_page():
    text = _text("gremlin-town-review")
    assert 'list `filters: {"recipient": "hermes-wa", "limit": 3}`' in text
    assert "newest whose `requester_id` is `exosuit-review`" in text
    assert "follow `next_cursor`, same filters" in text
    assert "Keep those filters while paging" in text
    assert "report the Telegram message ID" not in text
    assert "The courier posts the short 06:00 opening message, not the four parts" in text


def test_answers_change_work_through_the_door_instead_of_saving_clarification_facts():
    text = _text("gremlin-town-review")
    assert "Use one `bc_work` change per message with his exact raw message" in text
    assert "`answers` naming the supplied line references" in text
    assert "The question and reply remain evidence" in text
    assert "Save each answer as one `bc_memory_save`: kind fact" not in text
