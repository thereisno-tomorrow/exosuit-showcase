"""Discord limits, escaping and the approved button vocabulary, entirely local."""
from copy import deepcopy

import pytest

from gremlin_town.review_card.models import initial_state
from gremlin_town.review_card.render import render, walk, choices, GOLD, QUIET, RED, WHITE


def fixture():
    card = dict(ref="synthetic-item", title="A synthetic thing @everyone", labels=["Fixture"],
        crumbs=["Fixture parent"], facts=["Due today"], next_after="Fixture next step", stake="Fixture reason",
        length_minutes=30, picked=True, schedule=[], status="open", dates={}, progress={"total": 2, "text": "1 of 2"})
    line = dict(reference="synthetic-line", item_ref=card["ref"], kind="new_schedule", offered=["today", "park", "drop"], answered=False)
    day = dict(timeline=[], today=[], due_today=[card], due_tomorrow=[card], notices=[], moved_groups=[],
        schedule={"available": False, "label": "Couldn't see the schedule"})
    view = dict(day=day, item=card, line=line, queue=[line], candidates=[dict(card, ref=f"fixture-{i}") for i in range(10)],
        weekly=[{"title": "Loose ends", "lines": ["A fictional reminder"]}])
    state = {**initial_state(), "day": "2026-10-02", "item": line["reference"], "stage": "new"}
    return state, view


@pytest.mark.parametrize("screen", ["open", "catch", "new", "when", "ask", "anything", "day", "item_length", "lengths", "settled", "weekly", "expired"])
def test_every_screen_fits_discord_and_disables_mentions(screen):
    state, view = fixture()
    state["screen"] = screen
    result = render(state, view, lambda *args: "fictional-control", dashboard_url="https://example.test/app", ask=True)
    nodes = list(walk(result["components"]))
    assert len(nodes) <= 40 and result["allowed_mentions"] == {"parse": []}
    assert all(len(node["content"]) <= 4000 for node in nodes if node["type"] == 10)
    assert all(len(node["components"]) <= 5 for node in nodes if node["type"] == 1)
    assert "@everyone" not in str(result)
    assert all(node.get("url") != "https://example.test/app/app" for node in nodes)


def test_opening_colours_and_overflow_are_explicit():
    state, view = fixture()
    view["day"]["due_today"] *= 9
    view["day"]["due_tomorrow"] *= 9
    result = render(state, view, lambda *args: "fictional-control")
    assert [node["accent_color"] for node in walk(result["components"]) if node["type"] == 17] == [GOLD, QUIET, RED, WHITE]
    assert "5 more · open My day" in str(result)


def test_draft_and_nudge_screens_are_bounded_and_only_steps_offer_save():
    state, view = fixture()
    state["screen"] = "catch"
    for draft in ({"state": "draft", "operation_id": "fixture-op", "draft_sha256": "a" * 64,
                   "steps": [{"title": "x" * 256, "detail": "y" * 1000}] * 20},
                  {"state": "nudge", "text": "z" * 1600}):
        result = render(state, view, lambda *args: "fixture", draft=draft)
        assert sum(len(node.get("content", "")) for node in walk(result["components"])) <= 4000
        assert ("Save these steps" in str(result)) == (draft["state"] == "draft")


def test_chase_and_next_only_choices_respect_offered_actions():
    state, view = fixture()
    view["line"].update(kind="to_chase", offered=["draft a nudge", "leave it"])
    _, answers = choices(view["line"], view["item"], state, True)
    assert [answer[0] for answer in answers] == ['Draft a follow-up', 'Skip for now']
    state["screen"] = "ask"
    view["line"]["offered"] = ["next_action"]
    result = render(state, view, lambda *args: "fixture", ask=True)
    assert "Draft the next step" in str(result) and "Research and break it down" not in str(result)
