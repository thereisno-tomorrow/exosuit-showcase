from datetime import timedelta

from gremlin_town.flowsavvy.mapping import map_tasks
from gremlin_town.flowsavvy.settings import FlowSavvySettings
from test_connector import NOW, item


def test_today_selection_prioritizes_task_and_children_but_not_future_days():
    settings = FlowSavvySettings(list_id="fixture-list", admin_hours_id="fixture-hours")
    today = {"day": NOW.date().isoformat()}
    tomorrow = {"day": (NOW.date() + timedelta(days=1)).isoformat()}
    parent = item(1, minutes=None, called="project", dates={"planned": today})
    child = item(2, parent=parent.id)
    selected = item(3, dates={"planned": today})
    future = item(4, dates={"planned": tomorrow})
    _, tasks = map_tasks([parent, child, selected, future], settings=settings, now=NOW)
    tasks = {task.item_ref: task for task in tasks}
    assert tasks[child.id].payload["priority"] == "high"
    assert tasks[selected.id].payload["priority"] == "high"
    assert tasks[future.id].payload["priority"] == "normal"
    assert tasks[selected.id].deadline is None


def test_deadline_priority_still_wins_over_today_selection():
    settings = FlowSavvySettings(list_id="fixture-list", admin_hours_id="fixture-hours")
    task = item(1, dates={"planned": {"day": NOW.date().isoformat()},
                          "deadline": {"day": NOW.date().isoformat()}})
    _, tasks = map_tasks([task], settings=settings, now=NOW)
    tasks = {task.item_ref: task for task in tasks}
    assert tasks[task.id].payload["priority"] == "asap"
