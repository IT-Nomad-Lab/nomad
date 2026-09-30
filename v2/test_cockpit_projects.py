"""#787-#789 · the cockpit's PROJECTS panel reads OpenProject, not the NocoDB goals.

The behaviour (sort order, empty projects last, the sort surviving a reload, the stale notice, the
360 px layout) was checked in headless Chrome against live counts. These tests pin the contract the
page depends on, so a change to the endpoint or the page that breaks it shows up here first.

Run: python3 -m pytest v2/test_cockpit_projects.py -q   (engine deps live in the container)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

C = server.COCKPIT


def test_the_panel_reads_pm_projects_and_says_where_from():
    assert "fetch('/pm-projects')" in C and "fetch('/project-goals')" not in C
    assert "PROJECTS · OpenProject" in C


def test_four_sorts_default_to_priority_and_empty_projects_go_last():
    # Running status (#958) is the one sort that keeps empty projects in place: see test_cockpit_runs.
    assert ("prio:'Tasks by priority', open:'Number of tasks', run:'Running status', "
            "name:'Project name'") in C
    assert "return 'prio';" in C
    assert "list.filter(r=>r.open>0).sort(cmp), ...list.filter(r=>!(r.open>0)).sort(byName)" in C


def test_priority_sort_goes_immediate_high_normal_low_then_name():
    assert ("(p(b).Immediate-p(a).Immediate)||(p(b).High-p(a).High)||(p(b).Normal-p(a).Normal)"
            "||(p(b).Low-p(a).Low)||byName(a,b)") in C


def test_the_remembered_sort_survives_a_browser_without_storage():
    assert "try{ const v=localStorage.getItem('nomad.projSort')" in C
    assert "try{ localStorage.setItem('nomad.projSort',v); }catch(e){}" in C


def test_chips_follow_the_prd_order_and_blocked_and_awaiting_stand_out():
    assert "['Ready','In Progress','In Review','Blocked','Awaiting approval','Backlog','New']" in C
    assert ".proj .chip.blk{" in C and ".proj .chip.aw{" in C


def test_open_in_openproject_is_a_new_tab_link_at_least_44px():
    assert 'class=opbtn href="${escA(r.url)}" target=_blank rel=noopener' in C
    assert ".proj .opbtn{" in C and "min-height:44px" in C


def test_a_stale_answer_says_how_old_it_is():
    assert "OpenProject unreachable. These counts are ${age} old." in C


# ── 'next' per project (#793) ──

def test_each_row_has_a_next_button_between_openproject_and_the_autopilot():
    assert "<span>Open in OpenProject</span></a>`+projNextBtn(r)+apRowCtl(r.identifier)" in C
    assert "class=\"apbtn plain pnext\"" in C and "${ic('skip-forward')}" in C


def test_next_posts_the_rows_project_to_run_next_task():
    assert "onclick=\"projNext(+this.dataset.id)\"" in C and 'data-id="${escA(r.id)}"' in C
    assert ("fetch('/run-next-task',{method:'POST',headers:{'Content-Type':'application/json'},\n"
            "        body:JSON.stringify({project:id})})") in C


def test_next_is_off_with_the_reason_when_nothing_is_pickable():
    assert "const dis=!n||projNextBusy.has(r.id)?' disabled':'';" in C
    assert "Nothing to pick: ${held} Ready or Backlog task${held>1?'s have':' has'} an open blocker" in C
    assert "'Nothing to pick: no Task or Bug in Ready or Backlog'" in C
    assert 'title="${escA(t)}"' in C


def test_next_says_what_went_to_the_gate():
    assert "is at the gate${d.lane?' ('+d.lane+')':''}. Approve or reject it there." in C


@pytest.fixture
def route(monkeypatch):
    from starlette.testclient import TestClient
    seen = {"calls": [], "invalidated": 0}

    def run_next_task(goal_id=None, project=None):
        seen["calls"].append((goal_id, project))
        if seen.get("fail"):
            raise server.PMError("OpenProject unreachable")
        return {"task_id": 42, "task_title": "Draft", "run_id": "r1", "status": "awaiting-approval"}
    monkeypatch.setattr(server.eng, "run_next_task", run_next_task)
    monkeypatch.setattr(server, "_bump_runs", lambda: None)
    monkeypatch.setattr(server.pm_board, "invalidate_projects",
                        lambda: seen.__setitem__("invalidated", seen["invalidated"] + 1))
    return TestClient(server.app), seen


def test_the_route_passes_the_project_on_and_recounts_the_rows(route):
    client, seen = route
    r = client.post("/run-next-task", json={"project": 17})
    assert r.status_code == 200 and r.json()["task_id"] == 42
    assert seen["calls"] == [(None, 17)] and seen["invalidated"] == 1


def test_the_route_still_takes_a_goal_id(route):
    client, seen = route
    client.post("/run-next-task", json={"goal_id": 5})
    assert seen["calls"] == [(5, None)] and seen["invalidated"] == 0


def test_openproject_down_is_a_502_with_the_error(route):
    client, seen = route
    seen["fail"] = True
    r = client.post("/run-next-task", json={"project": 17})
    assert r.status_code == 502 and "unreachable" in r.json()["error"]


def test_pm_projects_endpoint_and_hook_invalidation_exist():
    paths = {r.path for r in server.app.routes}
    assert "/pm-projects" in paths and "/project-goals" in paths     # the chat and briefing keep theirs
    import inspect
    assert "pm_board.invalidate_projects()" in inspect.getsource(server.pm_hook)
