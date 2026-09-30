"""#957, #958 · the runs table shows the autopilot's builds, and projects sort by running status.

Approving a task in the projects panel hands it to an autopilot worker, not to the engine pipeline,
so before #957 the runs table stopped moving once approvals moved there. The table now merges the
supervisor's worker runs (`/api/autopilot/runs`) with the engine's own, newest first.

The node tests run the cockpit's real JS, cut out of COCKPIT, with fetch and the DOM stubbed.

Run: see v2/test_cockpit_layout.py (the node tests skip where node is missing, as in the container).
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

C = server.COCKPIT
AP = C[C.index("/* AUTOPILOT (#883, #884, PRD 5.10)"):C.index("/* BOARD panel")]
RUNS_JS = C[C.index("const LANES="):C.index("\ntick();\n/* live push")]
SORT_JS = C[C.index("const PROJ_SORTS="):C.index("function projRow(")]


# ── the contract ────────────────────────────────────────────────────────────────────────────────

def test_the_table_fetches_both_run_sources_and_a_down_supervisor_only_drops_its_rows():
    assert "fetch('/runs')" in RUNS_JS and "fetch('/api/autopilot/runs?limit=40')" in RUNS_JS
    ap = RUNS_JS[RUNS_JS.index("async function apRuns"):RUNS_JS.index("const AP_OUTCOME")]
    assert "catch(e){ return []; }" in ap


def test_every_outcome_has_a_colour():
    for o in ("merge", "running", "failed", "needs_operator", "resume", "unknown"):
        assert f".ap-{o}{{" in C, o
    assert ".l-autopilot{" in C


def test_the_route_is_there():
    assert any(getattr(r, "path", "") == "/api/autopilot/runs" for r in server.app.routes)


def test_running_status_is_a_sort_choice():
    assert "run:'Running status'" in SORT_JS


# ── the behaviour, in node ──────────────────────────────────────────────────────────────────────

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node not installed (run on the host)")

PRELUDE = r"""
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function ic(name){ return `<svg class=ic aria-hidden=true><use href="#i-${name}"/></svg>`; }
const escA=s=>esc(String(s??'')).replaceAll('"','&quot;');
function navBadge(){} function vspeak(){} function drawProjects(){}
const els={};
const document={getElementById:id=>(els[id]=els[id]||{innerHTML:'',style:{}})};
"""


def run_node(script):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "TZ": "UTC"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def iso(t):
    return time.strftime("%Y-%m-%d %H:%M:%S+00:00", time.gmtime(t))


def table(engine, autopilot, lane="all", ap_status=200, boards=None):
    """Draw the runs table from these two answers; return the rows' HTML and the lane bar."""
    body = f"""
let apData={json.dumps({"boards": boards or {}})};
let projData={{projects:[{{url:'http://op/projects/nomad-work'}}]}};
const answers={{'/runs':{json.dumps({"runs": engine})},
  '/api/autopilot/runs?limit=40':{json.dumps({"ok": ap_status == 200, "runs": autopilot})}}};
globalThis.fetch=async u=>{{ if({ap_status}!==200 && u.startsWith('/api/autopilot')) throw new Error('down');
  return {{json:async()=>answers[u]}}; }};
""" + AP.replace("apPoll(); setInterval(apPoll, 10000);", "").replace("let apData=null, apErr='';", "let apErr='';") \
        + RUNS_JS + f"""
laneFilter={json.dumps(lane)};
tick().then(()=>console.log(JSON.stringify({{rows:els.rows.innerHTML, lanes:els.lanebar.innerHTML}})));
"""
    return run_node(PRELUDE + body)


NOW = int(time.time())
ENG = [{"run_id": "r2", "lane": "dev", "from_agent": "dev", "type": "code", "status": "executed",
        "created_at": iso(NOW - 60)},
       {"run_id": "r1", "lane": "comms", "from_agent": "mail", "type": "email", "status": "declined",
        "created_at": iso(NOW - 9 * 86400)}]
APR = [{"project": "runematch", "task": "648", "branch": "ap/648", "started": NOW - 30,
        "outcome": "running", "summary": "", "cost_usd": None},
       {"project": "runematch", "task": "764", "branch": "ap/764", "started": NOW - 3600,
        "outcome": "needs_operator", "summary": "Need a call on <the> API.", "cost_usd": 0.5},
       {"project": "nomad", "task": "955", "branch": "ap/955", "started": NOW - 2 * 86400,
        "outcome": "merge", "summary": "all good", "cost_usd": 0.2}]


@needs_node
def test_autopilot_runs_merge_into_the_table_newest_first():
    rows = table(ENG, APR, boards={"runematch": "runematch", "nomad": "nomad-work"})["rows"]
    order = [rows.index(k) for k in ("#648", "r2", "#764", "#955", "r1")]
    assert order == sorted(order)
    assert "ap-running" in rows and "RUNNING" in rows and "NEEDS YOU" in rows and "MERGED" in rows
    # The task links to its board in OpenProject, in a new tab.
    assert 'href="http://op/projects/runematch/work_packages/764/activity" target=_blank rel=noopener' in rows


@needs_node
def test_a_summary_shows_for_what_needs_a_look_and_is_escaped():
    rows = table([], APR)["rows"]
    assert "Need a call on &lt;the&gt; API." in rows
    assert "all good" not in rows        # a merged run's summary stays out of the way


@needs_node
def test_the_autopilot_lane_chip_filters_to_the_builds():
    out = table(ENG, APR, lane="autopilot")
    assert "#648" in out["rows"] and "r2" not in out["rows"]
    assert "AUTOPILOT<span class=ct>3</span>" in out["lanes"]
    assert "ALL<span class=ct>5</span>" in out["lanes"]


@needs_node
def test_a_down_supervisor_leaves_the_engine_rows():
    rows = table(ENG, APR, ap_status=503)["rows"]
    assert "r2" in rows and "r1" in rows and "#648" not in rows


@needs_node
def test_when_shows_the_date_only_for_older_runs():
    rows = table(ENG, [])["rows"]
    assert time.strftime("%Y-%m-%d", time.gmtime(NOW - 9 * 86400)) in rows
    assert time.strftime("%Y-%m-%d", time.gmtime(NOW)) + " " not in rows


def sort(projects, states, boards):
    body = f"""
let apData={json.dumps({"boards": boards, "projects": {k: {"state": v} for k, v in states.items()}})};
""" + AP.replace("apPoll(); setInterval(apPoll, 10000);", "").replace("let apData=null, apErr='';", "let apErr='';") \
        + SORT_JS + f"""
console.log(JSON.stringify(projSorted({json.dumps(projects)}, 'run').map(r=>r.identifier)));
"""
    return run_node(PRELUDE + body)


@needs_node
def test_running_status_sorts_by_the_most_urgent_state_then_name():
    P = [{"identifier": i, "name": i, "open": o} for i, o in
         (("zeta", 3), ("alpha", 0), ("civis", 1), ("quiet", 2), ("nobot", 9), ("beta", 1))]
    states = {"zeta": "waiting", "alpha": "running", "civis-poc": "off", "civis-site": "needs_you",
              "quiet": "off", "beta": "running"}
    boards = {"zeta": "zeta", "alpha": "alpha", "civis-poc": "civis", "civis-site": "civis",
              "quiet": "quiet", "beta": "beta"}
    # running (by name, empty projects included), needs you, waiting, off, then no autopilot at all
    assert sort(P, states, boards) == ["alpha", "beta", "civis", "zeta", "quiet", "nobot"]
