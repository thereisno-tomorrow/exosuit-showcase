"""Confirmation checks use canonical planning and all scheduled work."""
from datetime import datetime, timezone
from types import SimpleNamespace
from gremlin_town.review_card.planning import planning_summary

NOW = datetime(2026,10,4,8,tzinfo=timezone.utc)

def item(ref,title,minutes,planning=None):
    return SimpleNamespace(id=ref,title=title,state="open",data={"rough_minutes":minutes, "planning":planning or {}})


def test_full_scheduled_work_counts_once_not_only_focus():
    index={"a":item("a","Picked",80), "b":item("b","Other scheduled work",120)}
    view={"today":[{"ref":"a","state":"open"},{"ref":"b","state":"open"}], "timeline":[]}
    result=planning_summary(index,selected=("a",),day_view=view,capacity_minutes=180,now=NOW)
    assert result["required_minutes"] == 200
    assert result["issues"] and "200" in result["issues"][0] and "180" in result["issues"][0]


def test_equipment_conflict_survives_ended_item_and_personal_minutes_fit():
    occupied={"resources":[{"resource":"pump","start":"2026-10-04T08:00:00+00:00","end":"2026-10-04T10:00:00+00:00"}]}
    index={"a":item("a","First irrigation",10,occupied),"b":item("b","Second irrigation",10,occupied)}
    index["a"].state="done"
    result=planning_summary(index,selected=("b",),capacity_minutes=60,now=NOW)
    assert any("pump" in issue for issue in result["issues"])


def test_unknown_capacity_is_not_a_claim_of_feasible_timed_schedule():
    result=planning_summary({"a":item("a","Work",20)},selected=("a",),now=NOW)
    assert result["provisional"] and any("time" in notice.lower() for notice in result["notices"])


def test_required_followup_effort_is_counted_before_start():
    followup="brn_20261004_090000_1234"
    operation={"operation":{"followup_ref":followup,"delay_minutes":30,"approved":True}}
    index={"a":item("a","Start pump",10,operation),followup:item(followup,"Stop pump",5)}
    result=planning_summary(index,selected=("a",),capacity_minutes=10,now=NOW)
    assert result["required_minutes"] == 15 and result["issues"]
    assert result["conditional_followups"][0]["conditional"] is True


def test_fixed_appointment_is_not_counted_twice_against_flexible_time():
    index={"a":item("a","Flexible work",30),"b":item("b","Fixed appointment",60)}
    index["b"].data["dates"]={"appointment":{"day":"2026-10-04"}}
    view={"today":[{"ref":"b","state":"open"}],"timeline":[]}
    result=planning_summary(index,selected=("a",),day_view=view,capacity_minutes=30,now=NOW)
    assert result["required_minutes"] == 30
    assert not result["issues"]


def test_timebox_counts_session_instead_of_whole_remaining_task():
    record=item('a','Research',240,{'work_sessions':[{'day':'2026-10-04','minutes':45,'kind':'timebox'}]})
    result=planning_summary({'a':record},selected=('a',),capacity_minutes=45,now=NOW)
    assert result['required_minutes']==45
    assert not result['issues']


def test_unassigned_hold_and_shared_travel_count_once():
    block={'id':'trip','kind':'travel','minutes':30,'task_refs':['a','b']}
    hold={'start':'2026-10-04T10:00:00+00:00','end':'2026-10-04T10:30:00+00:00'}
    index={'a':item('a','Bank',20,{'shared_blocks':[block]}),
           'b':item('b','Post',10,{'shared_blocks':[block]}),
           'hold':item('hold','Keep free',None,{'held_time':[hold]})}
    result=planning_summary(index,selected=('a','b'),capacity_minutes=80,now=NOW)
    assert result['required_minutes']==90
    assert result['issues']


def test_other_persons_work_does_not_use_personal_capacity():
    record=item('a','Colleague contribution',120)
    record.data['whose']='Colleague'
    result=planning_summary({'a':record},day_view={'today':[{'ref':'a','state':'open'}]},capacity_minutes=0,now=NOW)
    assert result['required_minutes']==0


def test_placement_outside_confirmed_window_is_not_confirmed_fit():
    record=item('a','Bank',30,{'windows':[{'start':'2026-10-04T08:00:00+00:00','end':'2026-10-04T09:00:00+00:00'}]})
    view={'timeline':[{'ref':'a','start':'2026-10-04T09:00:00+00:00','end':'2026-10-04T09:30:00+00:00'}]}
    result=planning_summary({'a':record},selected=('a',),day_view=view,capacity_minutes=30,now=NOW)
    assert result['issues'] and result['provisional']


def test_portion_selection_does_not_approve_newly_ready_portion():
    spec={'portions':[{'id':'first','quantity':10,'ready':True,'checks':['ready']},
                      {'id':'later','quantity':10,'ready':True,'checks':['ready']}],
          'portion_selections':[{'day':'2026-10-04','portions':[{'id':'first','quantity':5}]}]}
    result=planning_summary({'a':item('a','Harvest',100,spec)},selected=('a',),capacity_minutes=100,now=NOW)
    assert [(p['id'],p['selected_quantity']) for p in result['selected_portions']['a']]==[('first',5)]
    assert result['provisional']  # A whole-task estimate cannot prove partial effort.


def test_overlapping_unassigned_holds_do_not_double_count_time():
    index={'a':item('a','Keep free',None,{'held_time':[
        {'start':'2026-10-04T10:00:00+00:00','end':'2026-10-04T11:00:00+00:00'},
        {'start':'2026-10-04T10:30:00+00:00','end':'2026-10-04T11:30:00+00:00'}]})}
    result=planning_summary(index,capacity_minutes=90,now=NOW)
    assert result['held_minutes']==90 and not result['issues']


def test_explicit_portion_choice_is_in_day_without_whole_parent_focus():
    spec={'portions':[{'id':'ready','quantity':8,'ready':True,'checks':['verified']}],
          'portion_selections':[{'day':'2026-10-04','portions':[{'id':'ready','quantity':8}]}],
          'work_sessions':[{'day':'2026-10-04','minutes':25,'kind':'timebox'}]}
    result=planning_summary({'a':item('a','Harvest',200,spec)},capacity_minutes=25,now=NOW)
    assert result['required_minutes']==25
    assert result['selected_portions']['a'][0]['selected_quantity']==8


def test_started_operation_followup_survives_empty_picks_and_rollover():
    followup="brn_20261004_090000_1234"
    operation={"operation":{"followup_ref":followup,"delay_minutes":30,"approved":True,
                            "actual_start":"2026-10-04T08:00:00+00:00"}}
    source=item("a","Start pump",10,operation)
    source.state="done"
    index={"a":source,followup:item(followup,"Stop pump",5)}
    result=planning_summary(index,capacity_minutes=5,now=NOW)
    assert result["required_minutes"] == 5
    assert any("Stop pump" in notice and "08:30" in notice for notice in result["notices"])
    assert result["provisional"]  # Triggered but no placement confirmed.
    result=planning_summary(index,capacity_minutes=5,now=NOW.replace(day=5))
    assert result["required_minutes"] == 5
    assert any("passed" in issue for issue in result["issues"])


def test_actual_followup_time_rejects_old_placement_and_checks_conflict():
    followup="brn_20261004_090000_1234"
    operation={"operation":{"followup_ref":followup,"delay_minutes":30,"approved":True,
                            "actual_start":"2026-10-04T09:00:00+00:00"}}
    index={"a":item("a","Start pump",10,operation),followup:item(followup,"Stop pump",5)}
    view={"timeline":[{"ref":followup,"start":"2026-10-04T09:02:00+00:00","end":"2026-10-04T09:07:00+00:00"},
                      {"ref":"appointment","start":"2026-10-04T09:20:00+00:00","end":"2026-10-04T10:00:00+00:00"}]}
    result=planning_summary(index,day_view=view,capacity_minutes=60,now=NOW)
    assert any("09:30" in issue and "schedule shows a different time" in issue for issue in result["issues"])
    assert any("conflicts" in issue for issue in result["issues"])
    assert result["provisional"]


def test_finished_followup_does_not_remain_outstanding():
    followup="brn_20261004_090000_1234"
    operation={"operation":{"followup_ref":followup,"delay_minutes":30,"approved":True,
                            "actual_start":"2026-10-04T08:00:00+00:00"}}
    target=item(followup,"Stop pump",5); target.state="done"
    result=planning_summary({"a":item("a","Start pump",10,operation),followup:target},
                           capacity_minutes=0,now=NOW)
    assert result["required_minutes"]==0
    assert not result["issues"]


def test_followup_placement_compares_instants_during_repeated_clock_hour():
    from zoneinfo import ZoneInfo
    now=datetime(2026,11,1,1,10,tzinfo=ZoneInfo("America/New_York"),fold=1)
    followup="brn_20261004_090000_1234"
    operation={"operation":{"followup_ref":followup,"delay_minutes":30,"approved":True,
                            "actual_start":"2026-11-01T01:00:00-05:00"}}
    index={"a":item("a","Start",10,operation),followup:item(followup,"Stop",5)}
    view={"timeline":[{"ref":followup,"start":"2026-11-01T01:30:00-05:00","end":"2026-11-01T01:35:00-05:00"}]}
    result=planning_summary(index,day_view=view,capacity_minutes=30,now=now)
    assert not result["issues"]
    assert result["conditional_followups"][0]["at"] == "2026-11-01T01:30:00-05:00"
    assert not result["provisional"]


def test_overdue_followup_uses_elapsed_time_in_repeated_hour():
    from zoneinfo import ZoneInfo
    now=datetime(2026,11,1,1,10,tzinfo=ZoneInfo("America/New_York"),fold=1)
    followup="brn_20261004_090000_1234"
    operation={"operation":{"followup_ref":followup,"delay_minutes":40,"approved":True,
                            "actual_start":"2026-11-01T01:00:00-04:00"}}
    result=planning_summary({"a":item("a","Start",10,operation),followup:item(followup,"Stop",5)},
                           capacity_minutes=30,now=now)
    assert any("passed" in issue for issue in result["issues"])


def test_started_return_cannot_disappear_when_target_was_dropped_or_missing():
    followup = 'brn_20261004_090000_1234'
    operation = {'operation': {'followup_ref': followup, 'delay_minutes': 30,
                              'approved': True, 'actual_start': '2026-10-04T08:00:00+00:00'}}
    for ending in ('dropped', 'no_longer_needed', None):
        index = {'a': item('a', 'Start pump', 10, operation)}
        if ending:
            target = item(followup, 'Stop pump', 5)
            target.state = ending
            index[followup] = target
        result = planning_summary(index, capacity_minutes=0, now=NOW)
        assert any(f['item_ref'] == followup for f in result['conditional_followups'])
        assert result['issues'], 'An unresolved physical return must stay visible'
        assert result['provisional']