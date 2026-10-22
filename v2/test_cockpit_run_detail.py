"""#1177 · Runs: the stage filter, the run detail sheet with its sticky gate bar, the reject sheet.

Node tests run the cockpit's real JS, cut out of COCKPIT, with fetch and the DOM stubbed.
Run: see v2/test_cockpit_layout.py (the node tests skip where node is missing, as in the container).
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

C = server.COCKPIT
AP = C[C.index("/* AUTOPILOT (#883, #884, PRD 5.10)"):C.index("/* BOARD panel")]
GATE_JS = C[C.index("async function gate("):C.index("async function retry(")]
RUNS_JS = C[C.index("const LANES="):C.index("\ntick();\n/* live push")]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node not installed (run on the host)")


# ── the page ────────────────────────────────────────────────────────────────────────────────────

def test_runs_has_a_list_and_a_sheet_and_the_sheet_starts_hidden():
    assert "<div id=runslist>" in C and "<div id=runsheet class=runsheet hidden></div>" in C
    assert "<div id=stagebar" in C and "<div id=lanebar" in C


def test_the_shell_hands_the_run_id_in_the_hash_to_the_sheet():
    assert "runSheet(cur==='runs' && hp[1]" in C


def test_a_run_row_opens_the_sheet_and_no_row_expands_in_place():
    assert "toggleDetail" not in C and "openRuns" not in C
    assert "onclick=\"openRun('" in RUNS_JS


def test_the_phone_makes_a_run_a_two_line_row_without_decision_buttons():
    css = C[C.index("Runs on a phone (#1177)"):]
    css = css[:css.index("}\n.rlink")]
    assert "#runs thead{display:none}" in css and "display:grid" in css
    assert "#runs td:nth-child(5) .gate{display:none}" in css


def test_reject_is_confirmed_in_a_sheet_not_a_browser_dialog():
    assert "confirm('Reject this run?')" not in C
    assert "await askSheet('Reject run '" in GATE_JS
    assert '<div id=csheetwrap class=cs-hidden></div>' in C


# ── the behaviour, in node ──────────────────────────────────────────────────────────────────────

PRELUDE = r"""
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function ic(name){ return `<svg class=ic aria-hidden=true><use href="#i-${name}"/></svg>`; }
const escA=s=>esc(String(s??'')).replaceAll('"','&quot;');
function navBadge(){} function vspeak(){} function drawProjects(){} function drawNow(){}
globalThis.scrollTo=()=>{};
const els={};
const document={getElementById:id=>(els[id]=els[id]||{innerHTML:'',style:{},hidden:false}),
  querySelectorAll:()=>[]};
const fetched=[];
let sheetRid=null, sheetBuilt=null, runsAll=[];   // the page declares these ahead of the shell
"""


def run(tail, engine=(), autopilot=(), detail=None):
    answers = {"/runs": {"runs": list(engine)}, "/api/autopilot/runs?limit=40": {"ok": True, "runs": list(autopilot)},
               "/resume": {"ok": True}}
    script = PRELUDE + f"""
let apData={{boards:{{}}}};
let projData={{projects:[]}};
const answers={json.dumps(answers)};
const details={json.dumps(detail or {})};
globalThis.fetch=async (u,o)=>{{ fetched.push([u,o&&o.body||null]);
  if(u.startsWith('/run?id=')) return {{json:async()=>details[decodeURIComponent(u.slice(8))]||{{}}}};
  return {{json:async()=>answers[u]||{{}}}}; }};
""" + AP.replace("apPoll(); setInterval(apPoll, 10000);", "").replace("let apData=null, apErr='';", "let apErr='';") \
        + RUNS_JS + GATE_JS + tail
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def prop(h="h1"):
    return {"action": "send_email", "target": "a@b.c", "preview": "Hello there", "proposal_hash": h}


def eng(rid, status, **kw):
    return {"run_id": rid, "lane": "comms", "from_agent": "mail", "type": "email", "status": status,
            "created_at": "2026-10-12 10:00:00+00:00", **kw}


ENG = [eng("r1", "new"), eng("r2", "awaiting-approval", proposal=prop()), eng("r3", "executing"),
       eng("r4", "executed"), eng("r5", "failed"), eng("r6", "declined")]
APR = [{"project": "x", "task": "1", "started": 1790000000, "outcome": "running", "summary": ""},
       {"project": "x", "task": "2", "started": 1790000100, "outcome": "needs_operator", "summary": "q"},
       {"project": "x", "task": "3", "started": 1790000200, "outcome": "merge", "summary": ""}]
DONE = "tick().then(()=>{%s});"


@needs_node
def test_the_stage_chips_count_each_stage():
    out = run(DONE % "console.log(JSON.stringify({s:els.stagebar.innerHTML}))", ENG, APR)["s"]
    for label, n in (("ALL", 9), ("PROCESS", 1), ("HUMAN GATE", 2), ("EXECUTE", 2), ("LOG&amp;LEARN", 4)):
        assert f"{label}<span class=ct>{n}</span>" in out, (label, out)


@needs_node
def test_the_human_gate_stage_shows_what_waits_and_nothing_else():
    out = run("stageFilter='gate'; " + DONE % "console.log(JSON.stringify({r:els.rows.innerHTML}))", ENG, APR)["r"]
    assert "r2" in out and "#2" in out
    for gone in ("r1", "r3", "r4", "r5", "r6", "#1<", "#3<"):
        assert gone not in out, gone


@needs_node
def test_the_stage_and_lane_filters_combine():
    out = run("stageFilter='done'; laneFilter='comms'; " + DONE % "console.log(JSON.stringify({r:els.rows.innerHTML,s:els.stagebar.innerHTML}))", ENG, APR)
    assert "r4" in out["r"] and "r5" in out["r"] and "r2" not in out["r"] and "#3" not in out["r"]
    assert "LOG&amp;LEARN<span class=ct>3</span>" in out["s"]      # counted inside the lane


@needs_node
def test_a_row_opens_the_run_in_the_sheet():
    out = run(DONE % "console.log(JSON.stringify({r:els.rows.innerHTML}))", ENG)["r"]
    assert "onclick=\"openRun('r2')\"" in out and "rowTap(event,'r2')" in out


SHEET = """
tick().then(async ()=>{ runSheet('%s'); await new Promise(r=>setTimeout(r,20));
  console.log(JSON.stringify({bar:els.gatebar.innerHTML, meta:els['rs-meta'].innerHTML,
    prop:els['rs-prop'].innerHTML, sheet:els.runsheet.innerHTML, hidden:[els.runsheet.hidden, els.runslist.hidden],
    det:(els['det-%s']||{}).innerHTML||''})); });
"""


@needs_node
def test_a_run_at_the_gate_gets_a_sticky_bar_with_reject_and_approve():
    out = run(SHEET % ("r2", "r2"), ENG, detail={"r2": {"status": "awaiting-approval", "goal": "Say hello",
                                                         "proposal_hash": "h1"}})
    assert out["hidden"] == [False, True]
    assert ">" in out["bar"] and "Reject" in out["bar"] and "Approve" in out["bar"]
    assert "disabled" not in out["bar"]
    assert "send_email" in out["prop"] and "Hello there" in out["prop"]
    assert "AWAITING-APPROVAL" in out["meta"]
    assert "Say hello" in out["det"]


@needs_node
def test_approve_stays_off_when_no_proposal_was_drawn():
    bare = eng("r2", "awaiting-approval", proposal={"action": "send_email"})
    out = run(SHEET % ("r2", "r2"), [bare])
    assert "Reject" in out["bar"] and out["bar"].count("disabled") == 1
    assert "gb ok" in out["bar"].split("disabled")[0].rsplit("<button", 1)[1]


@needs_node
def test_a_failed_run_gets_retry_and_a_finished_one_gets_no_bar():
    assert "Retry" in run(SHEET % ("r5", "r5"), ENG)["bar"]
    assert run(SHEET % ("r4", "r4"), ENG)["bar"] == ""


@needs_node
def test_a_run_missing_from_the_list_still_opens_from_the_engine():
    out = run(SHEET % ("old9", "old9"), ENG, detail={"old9": {"status": "executed", "goal": "Old goal"}})
    assert "Not in the recent list" in out["meta"] and out["bar"] == "" and "Old goal" in out["det"]


@needs_node
def test_closing_the_sheet_shows_the_list_again():
    out = run("tick().then(()=>{ runSheet('r2'); runSheet(null); console.log(JSON.stringify("
              "{h:[els.runsheet.hidden, els.runslist.hidden]})); });", ENG)
    assert out["h"] == [True, False]


REJECT = """
const asked=[]; let answer=%s;
askSheet=async (t,b,ok)=>{ asked.push([t,b,ok]); return answer; };
tick().then(async ()=>{ fetched.length=0; await gate('r2','rejected'); await new Promise(r=>setTimeout(r,20));
  console.log(JSON.stringify({asked, resume:fetched.filter(f=>f[0]==='/resume')})); });
"""


@needs_node
def test_reject_asks_in_the_sheet_and_names_what_will_not_happen():
    out = run(REJECT % "false", ENG)
    assert out["asked"][0][0] == "Reject run r2?"
    assert "send_email to a@b.c is dropped" in out["asked"][0][1] and "cannot be undone" in out["asked"][0][1]
    assert out["resume"] == []                      # kept: nothing was sent to the engine


@needs_node
def test_a_confirmed_reject_goes_to_the_engine():
    out = run(REJECT % "true", ENG)
    assert len(out["resume"]) == 1
    assert json.loads(out["resume"][0][1]) == {"run_id": "r2", "decision": "rejected"}


# ── the page must evaluate to its last line ─────────────────────────────────────────────────────

BOOT = r"""
const mk=()=>{ const f=function(){}; return new Proxy(f,{
  get:(t,k)=>k===Symbol.toPrimitive?()=>'':k==='length'?0:k==='then'?undefined:
     k==='matches'?false:(k in t&&typeof k==='symbol'?t[k]:mk()),
  set:()=>true, apply:()=>mk(), construct:()=>mk(), has:()=>true }); };
globalThis.document=mk(); globalThis.window=globalThis;
globalThis.location={hash:'#projects',protocol:'http:',host:'x',origin:'http://x',search:'',pathname:'/'};
globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}}; globalThis.sessionStorage=globalThis.localStorage;
globalThis.matchMedia=()=>({matches:false,addEventListener(){},addListener(){}});
globalThis.addEventListener=()=>{}; globalThis.scrollTo=()=>{};
globalThis.EventSource=class{}; globalThis.WebSocket=class{}; globalThis.Terminal=class{}; globalThis.FitAddon={FitAddon:class{}};
globalThis.fetch=()=>new Promise(()=>{}); globalThis.navigator={}; globalThis.requestAnimationFrame=()=>0;
globalThis.setInterval=()=>0; globalThis.setTimeout=()=>0;
process.on('unhandledRejection',()=>{});
"""


@needs_node
def test_the_whole_page_script_runs_to_its_end_on_load():
    """A `let` read before its line, or any top-level throw, stops the script: the page then sits at
    'loading…' with no destination chosen. The node tests above cut slices and cannot see that."""
    import re
    js = max(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", C, re.S), key=len)
    out = subprocess.run([NODE, "-e", BOOT + "try{ (0,eval)(" + json.dumps(js) + "); console.log('ok'); }"
                          "catch(e){ console.log('THROW '+e.name+': '+e.message); }"],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "ok", out.stdout + out.stderr


def test_the_run_sheet_state_is_declared_before_the_shell_uses_it():
    assert C.index("let sheetRid=null") < C.index("(function(){\n  const tabs=")


def test_the_chat_dock_wraps_instead_of_scrolling_sideways():
    dock = C[C.index("@media (min-width:1024px){"):C.index("</style>")]
    assert "#dest-chat #voicebar{flex-wrap:wrap" in dock and "overflow:hidden" in dock
    assert "#dest-chat .crow{margin-top:auto;flex-wrap:wrap}" in dock
