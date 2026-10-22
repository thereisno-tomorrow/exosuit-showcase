"""Plain review wording retains the underlying action contracts."""
import pytest
from tests.review_card.test_render import fixture
from gremlin_town.review_card.render import choices, modal, render, walk


def nodes(result):
    return list(walk(result["components"]))


def test_answered_item_cannot_offer_update_when_an_earlier_step_is_open():
    state, view = fixture()
    state["screen"] = "answered"
    view["item"]["prerequisites_unfinished"] = True
    actions = []
    result = render(state, view, lambda label, action, options: actions.append(action) or action)
    assert "resume_review" in actions and "inspect_saved" not in actions
    assert "Going back does not undo saved answers" in str(result)


def test_settled_schedule_notices_keep_dashboard_link_on_navigation():
    state, view = fixture()
    state["screen"] = "settled"
    view["planning"] = {"issues": ["Equipment has overlapping bookings."], "notices": ["A time estimate is missing."]}
    result = render(state, view, lambda *args: "control", dashboard_url="https://example.test/app")
    assert any(node.get("url") == "https://example.test/app" for node in nodes(result))


@pytest.mark.parametrize("screen", ["open", "day", "settled"])
def test_waiting_list_is_an_explicit_choice(screen):
    state, view = fixture()
    state["screen"] = screen
    actions = []
    result = render(state, view, lambda label, action, options: actions.append(action) or action)
    assert 'open_more' in actions or 'day_more' in actions
    assert 'waiting_updates' not in actions
    view['details'] = {'waiting': {'ref':'waiting', 'status':'waiting', 'arrival_available':True, 'state':'open'}}
    state['screen'] = 'open_more' if screen == 'open' else 'day_more'
    actions.clear()
    result = render(state, view, lambda label, action, options: actions.append(action) or action)
    assert 'waiting_updates' in actions
    assert 'Update something waiting…' in str(result)
    assert len(nodes(result)) <= 40


def test_waiting_screen_paginates_five_items_and_explains_its_scope():
    state, view = fixture()
    state.update(screen="waiting_updates", page=0)
    view["waiting_items"] = [dict(ref=f"waiting-{i}", title=f"Delivery {i}") for i in range(6)]
    actions = []
    result = render(state, view, lambda label, action, options: actions.append((action, options)) or action)
    assert len([action for action, _ in actions if action == "waiting_item"]) == 5
    assert any(action == "more" for action, _ in actions)
    assert "until the wait is cleared" in str(result)
    assert len(nodes(result)) <= 40


@pytest.mark.parametrize("suggestions,label", [({"suggested_name": "Clear name"}, "Use this name"), ({"suggested_name": "Clear name", "suggested_label": "Home"}, "Use name and label"), ({"suggested_label": "Home"}, "Use this label")])
def test_name_approval_says_exactly_what_is_saved(suggestions, label):
    state, view = fixture()
    view["line"].update(suggestions)
    _, answers = choices(view["line"], view["item"], state, False)
    assert answers[0] == (label, "save", {})


def test_outcome_form_uses_plain_choices_with_existing_values():
    result = modal("control", {"action": "dated_outcome"}, __import__("datetime").date(2026, 10, 5))
    options = result["data"]["components"][0]["component"]["options"]
    assert [(option["label"], option["value"]) for option in options] == [("Finished", "finished"), ("Made progress", "progress"), ("Didn't do it", "not_done")]


@pytest.mark.parametrize("action,label", [("wait", "Who or what is missing?"), ("delegate", "Who is taking this over?")])
def test_wait_and_handoff_name_the_right_person(action, label):
    result = modal("control", {"action": action}, __import__("datetime").date(2026, 10, 5))
    assert result["data"]["components"][0]["label"] == label


def test_project_form_opens_with_a_plain_project_name_field():
    result = modal("control", {"action": "under"}, __import__("datetime").date(2026, 10, 5))
    assert result["data"]["title"] == "Add to a project"
    assert result["data"]["components"][0]["label"] == "Existing project title"


@pytest.mark.parametrize("action", ["add", "parts", "next", "rename", "change", "progress", "wait", "inspect", "correction", "arrival", "delegate", "note", "capacity", "timezone", "under", "duplicate_choose", "requirement", "portion_result", "portion_select", "timebox", "hold", "dated_outcome", "appointment", "planning_resource", "planning_portion", "planning_operation", "planning_window", "planning_shared", "planning_start"])
def test_form_wording_fits_discord_limits(action):
    data = modal("control", {"action": action}, __import__("datetime").date(2026, 10, 5))["data"]
    assert len(data["title"]) <= 45
    assert len(data["components"]) <= 5
    assert all(len(field["label"]) <= 45 and len(field.get("description", "")) <= 100 for field in data["components"])



def test_missed_deadline_and_follow_up_use_plain_actions():
    state, view = fixture()
    question, answers = choices({"kind": "missed"}, view["item"], state, False)
    assert question == "This deadline has passed. What should happen now?"
    assert [action for _, action, _ in answers] == ["when", "change", "when", "drop"]
    assert answers[0][0] == "Set a new deadline\u2026"
    question, answers = choices({"kind": "to_chase"}, view["item"], state, True)
    assert question == "Time to follow up?"
    assert [label for label, _, _ in answers] == ["Draft a follow-up", "Skip for now"]


def test_planned_date_shows_readable_date_without_internal_scope():
    state, view = fixture()
    state["screen"] = "yesterday"
    view["line"].update(commitment_day="2026-10-04", scope="task")
    result = render(state, view, lambda *args: "control")
    assert "You planned this for Sun 4 Oct." in str(result)
    assert "2026-10-04" not in str(result)


def test_note_classification_explains_everyday_meanings():
    data = modal("control", {"action": "classify"}, __import__("datetime").date(2026, 10, 5))["data"]
    options = data["components"][0]["component"]["options"]
    assert {option["value"] for option in options} == {"information", "reflection", "idea", "arrangement"}
    assert all(option["description"] and len(option["description"]) <= 100 for option in options)


def test_combined_approval_shows_the_label_being_approved():
    state, view = fixture()
    state.update(screen="inbox", stage="inbox")
    view["line"].update(suggested_name="Call the office", suggested_label="Phone calls")
    result=render(state,view,lambda *args:"control")
    assert "Suggested label: **Phone calls**" in str(result)
    assert "Use name and label" in str(result)



def test_planning_menu_hides_specialist_controls_until_more():
    state, view = fixture()
    state.update(screen="planning_menu", page=0)
    actions = []
    render(state, view, lambda label, action, options: actions.append(action) or action)
    assert {"planning_window", "planning_menu", "back"} <= set(actions)
    assert not {"planning_resource", "planning_portion", "planning_operation", "planning_shared"} & set(actions)
    state["page"] = 1
    actions.clear()
    render(state, view, lambda label, action, options: actions.append(action) or action)
    assert {"planning_resource", "planning_portion", "planning_operation", "planning_shared", "back"} <= set(actions)
