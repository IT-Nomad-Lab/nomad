"""#883, #884 · the cockpit's autopilot controls: Run/Stop and a status line per project, and the
global strip with the budget switch, the caps and the totals (PRD 5.10).

Two kinds of check. The contract ones read the page as a string, like test_cockpit_projects.py:
the routes it calls, the finish-or-kill choice, the 44 px targets. The behaviour ones run the
page's own autopilot functions in node against a /status answer shaped like the supervisor's:
which row a project's line lands on, what the line says, which button it offers. They need node
and skip without it (the engine image has none; run them on the host).

The page is read from server.py's source rather than by importing server, so the node half runs
without the engine's dependencies:
    uv run --no-project --with pytest python -m pytest v2/test_cockpit_autopilot.py -q
"""
import ast
import json
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _cockpit():
    tree = ast.parse(open(os.path.join(HERE, "server.py")).read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "COCKPIT" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("no COCKPIT in server.py")


C = _cockpit()
AP = C[C.index("/* AUTOPILOT (#883, #884, PRD 5.10)"):C.index("/* BOARD panel")]


# ── the contract ────────────────────────────────────────────────────────────────────────────────

def test_it_reads_the_engine_proxy_not_the_supervisor():
    assert "fetch('/api/autopilot/status')" in AP
    assert "'/api/autopilot'+path" in AP
    assert "8092" not in C and "X-Nomad-Token" not in C       # the token stays in the engine


def test_run_starts_and_stop_asks_finish_or_kill():
    assert "`/projects/${encodeURIComponent(pid)}/start`" in AP
    assert "`/projects/${encodeURIComponent(pid)}/stop`, {mode}" in AP
    assert "apStopMode(this.dataset.pid,'finish')" in AP and "apStopMode(this.dataset.pid,'kill')" in AP
    assert "Let them finish" in AP and "Kill them" in AP


def test_the_strip_edits_the_budget_and_caps_through_config():
    assert "apPost('/config', patch)" in AP
    for field in ("budget_mode", "daily_cap_usd", "global_cap", "per_project_cap"):
        assert field in AP, field
    assert "apPost('/pass')" in AP


def test_every_state_has_a_label_and_a_colour():
    for s in ("off", "running", "waiting", "needs_you", "budget_paused", "error", "idle"):
        assert f".apst.{s}{{" in C, s
        assert re.search(rf"\b{s}:'", AP), s


def test_controls_are_phone_sized():
    for sel in (".apbtn{", ".apseg button{", ".apform input{"):
        rule = C[C.index(sel):C.index("}", C.index(sel))]
        assert "min-height:44px" in rule, sel
    assert "@media (max-width:600px){ .ap .acts{margin-left:0}" in C


def test_the_strip_sits_above_the_projects_panel():
    assert C.index("<div id=apstrip") < C.index("<div id=projects")


def test_a_refused_write_is_shown_not_swallowed():
    """The auth middleware answers a refusal in plain text, the proxy in JSON: both reach the page."""
    assert "text||('HTTP '+r.status)" in AP
    assert "d.error+(d.detail?': '+d.detail:'')" in AP


def test_a_poll_never_eats_a_keystroke():
    assert "if(apFormBusy()) return;" in AP


# ── the behaviour, in node ──────────────────────────────────────────────────────────────────────

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node not installed (run on the host)")

PRELUDE = r"""
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function ic(name){ return `<svg class=ic aria-hidden=true><use href="#i-${name}"/></svg>`; }
const escA=s=>esc(String(s??'')).replaceAll('"','&quot;');
let projData=null; let drawn=0; function drawProjects(){ drawn++; }
function navBadge(){}   // the phone tab bar's badge (#946); not what these tests look at
"""


def run_node(body):
    script = PRELUDE + AP.replace("apPoll(); setInterval(apPoll, 10000);", "") + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


NOW = 1_790_000_000
STATUS = {
    "ok": True, "heartbeat_age_s": 3,
    "config": {"budget_mode": "cap", "daily_cap_usd": 50, "per_project_cap": 3, "global_cap": 6},
    "budget": {"mode": "cap", "cap_usd": 50, "spent_usd": 12.5, "remaining_usd": 37.5,
               "allowed": True, "by_project": {"runematch": 12.5}},
    "runnable": ["civis-poc", "civis-site", "runematch"],
    "boards": {"runematch": "runematch", "civis-poc": "civis", "civis-site": "civis",
               "gone": "gone-board"},
    "projects": {
        "runematch": {"enabled": True, "state": "running",
                      "detail": "1 worker(s): #833; needs you: #840 awaiting approval; waiting: #841 blocked by #833",
                      "workers": [{"task": 833, "branch": "ap/833", "started": NOW - 600}],
                      "events": [{"t": NOW - 7200, "kind": "merge", "msg": "task 830: merged abc -> Done."},
                                 {"t": NOW - 60, "kind": "merge", "msg": "task 831: merged def -> Done."},
                                 {"t": NOW - 50, "kind": "merge", "msg": "task 832: failed -> Blocked."},
                                 {"t": NOW - 30, "kind": "started", "msg": "task 833"}]},
        "civis-poc": {"enabled": False, "state": "needs_you", "detail": "#12 awaiting approval; #13 x",
                      "workers": [], "events": []},
        "gone": {"enabled": False, "state": "off", "detail": "stopped (finish), 1 worker(s) finishing",
                 "workers": [{"task": 5, "started": NOW - 30}], "events": []},
    },
}


def js_setup(now=NOW):
    """Date.now() pinned, and local midnight pinned an hour before NOW, so "today" does not depend
    on the timezone the test runs in."""
    return (f"Date.now=()=>{now*1000}; apData={json.dumps(STATUS)};"
            f"projData={{projects:[{{identifier:'runematch',url:'https://pm.example/projects/runematch/work_packages'}},"
            f"{{identifier:'civis',url:'https://pm.example/projects/civis/work_packages'}}]}};"
            f"apDayStart=()=>{NOW - 3600};")


@needs_node
def test_the_full_page_script_parses():
    js = C[C.index("<script>\nfunction esc") + len("<script>"):C.rindex("</script>")]
    out = subprocess.run([NODE, "--check", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


@needs_node
def test_lines_land_on_their_boards_row():
    r = run_node(js_setup() + """
      console.log(JSON.stringify({civis: apPidsFor('civis'), rm: apPidsFor('runematch'),
        none: apRowLines('nomad-work'), civisHtml: apRowLines('civis')}));""")
    assert r["civis"] == ["civis-poc", "civis-site"] and r["rm"] == ["runematch"]
    assert r["none"] == ""
    assert 'data-pid="civis-poc"' in r["civisHtml"] and 'data-pid="civis-site"' in r["civisHtml"]


@needs_node
def test_a_running_line_says_state_workers_done_spend_last_event_and_needs_you():
    r = run_node(js_setup() + "console.log(JSON.stringify(apLine('runematch')));")
    assert 'class="apst running">running<' in r
    assert "Stop</span></button>" in r and "Run</span>" not in r
    assert 'href="https://pm.example/projects/runematch/work_packages/833/activity"' in r and "#833</a> 10 min" in r
    assert "done today <b>1</b>" in r              # 831 merged today; 830 was before midnight; 832 failed
    assert "spend today <b>$12.50</b>" in r
    needs = r.split("class=nyou>needs you:")[1].split("</span>")[0]
    assert "/projects/runematch/work_packages/840/activity" in needs and "/work_packages/841" not in needs   # 841 waits on a task
    assert "last: started · task 833 · 30 s ago" in r
    assert "recent events (4)" in r


@needs_node
def test_an_off_runnable_project_offers_run_and_needs_you_lists_every_task():
    r = run_node(js_setup() + "console.log(JSON.stringify(apLine('civis-poc')));")
    assert "Run</span></button>" in r and "Stop</span>" not in r
    assert "needs you:" in r and "/projects/civis/work_packages/12/activity" in r and "/projects/civis/work_packages/13/activity" in r


@needs_node
def test_a_project_never_started_gets_a_run_button_and_an_off_badge():
    r = run_node(js_setup() + "console.log(JSON.stringify(apLine('civis-site')));")
    assert 'class="apst off">off<' in r and "Run</span></button>" in r


@needs_node
def test_a_stopped_project_with_workers_still_finishing_can_kill_them():
    r = run_node(js_setup() + "console.log(JSON.stringify(apLine('gone')));")
    assert "Kill 1 worker</span>" in r
    assert "Run</span>" not in r        # not in runnable: dropped from projects.yaml


@needs_node
def test_stop_asks_only_when_workers_are_running():
    r = run_node(js_setup() + """
      const calls=[]; apStopMode=(pid,mode)=>calls.push([pid,mode]);
      apStop('civis-poc'); apStop('runematch');
      console.log(JSON.stringify({calls, asked:[...apAsk], line: apLine('runematch')}));""")
    assert r["calls"] == [["civis-poc", "finish"]]
    assert r["asked"] == ["runematch"]
    assert "Let them finish" in r["line"] and "Kill them" in r["line"] and "Cancel" in r["line"]


@needs_node
def test_run_posts_start_and_shows_a_refusal():
    r = run_node(js_setup() + """
      const sent=[];
      global.fetch=async (url,opt)=>{ sent.push([url,opt&&opt.method,opt&&opt.body]);
        if(opt&&opt.method==='POST') return {ok:false,status:403,text:async()=>'Refused: this write did not come from the cockpit\\'s own page.'};
        return {ok:true,status:200,json:async()=>apData}; };
      global.document={getElementById:()=>({innerHTML:'',textContent:'',className:'',contains:()=>false})};
      apRun('civis-poc').then(()=>console.log(JSON.stringify({sent, msg: apMsgs['civis-poc']})));""")
    assert r["sent"][0][:2] == ["/api/autopilot/projects/civis-poc/start", "POST"]
    assert r["msg"] == {"ok": False, "text": "Refused: this write did not come from the cockpit's own page."}


@needs_node
def test_save_sends_only_the_changed_caps():
    r = run_node(js_setup() + """
      const vals={apcap:'80', apglobal:'6', apper:'2'};
      global.document={activeElement:null, getElementById:id=>(id in vals?{value:vals[id],disabled:false}:
        {innerHTML:'',textContent:'',className:'',contains:()=>false})};
      let patch=null; apConfig=async p=>{ patch=p; };
      apSave(null); console.log(JSON.stringify(patch));""")
    assert r == {"daily_cap_usd": 80, "per_project_cap": 2}


@needs_node
def test_done_today_counts_only_merges_since_midnight():
    r = run_node(js_setup() + f"console.log(JSON.stringify(apDoneToday(apData.projects.runematch, {NOW})));")
    assert r == 1


@needs_node
def test_a_project_whose_board_has_no_row_is_drawn_in_the_strip():
    """Run and Stop stay reachable when OpenProject is down or the board is not listed."""
    r = run_node(js_setup() + """
      const box={innerHTML:''}; global.document={getElementById:()=>box};
      apDrawOther(); const listed=box.innerHTML;
      projData=null; apDrawOther();
      console.log(JSON.stringify({listed, down: box.innerHTML}));""")
    assert 'data-pid="gone"' in r["listed"] and 'data-pid="runematch"' not in r["listed"]
    for pid in ("gone", "runematch", "civis-poc", "civis-site"):
        assert f'data-pid="{pid}"' in r["down"], pid


# ── #890: Open in OpenProject, the Autopilot ON/OFF switch and Run/Stop on every row ────────────

ROW = C[C.index("const PROJ_CHIPS="):C.index("function drawProjects(){")]


PROJROW = ROW[ROW.index("function projRow"):]


def run_node_rows(body):
    return run_node(ROW[:ROW.index("\n")] + "\n" + PROJROW + body)   # PROJ_CHIPS, then projRow


def test_the_row_holds_open_then_the_switch_and_run_next_to_the_name():
    row = ROW[ROW.index("function projRow"):]
    assert row.index("<div class=pn>") < row.index("<div class=pctl>") < row.index("Open in OpenProject") \
        < row.index("apRowCtl(r.identifier)") < row.index("apRowLines(r.identifier)")


def test_the_switch_posts_to_its_route_and_is_phone_sized():
    assert "`/projects/${encodeURIComponent(pid)}/autopilot`, {on:!!on}" in AP
    rule = C[C.index(".apsw{"):C.index("}", C.index(".apsw{"))]
    assert "min-height:44px" in rule
    assert "role=switch aria-checked=" in AP
    assert ".proj .pctl{flex:1 1 100%}" in C          # the controls get their own line on a phone


def _rowjs(extra=""):
    status = json.loads(json.dumps(STATUS))
    status["projects"]["runematch"]["autopilot"] = True
    status["projects"]["runematch"]["run_mode"] = "continuous"
    return (f"Date.now=()=>{NOW*1000}; apData={json.dumps(status)};"
            "projData={projects:[{identifier:'runematch',name:'RuneMatch',url:'https://pm.example/projects/runematch/work_packages'},"
            "{identifier:'civis',name:'Civis',url:'https://pm.example/projects/civis/work_packages'},"
            "{identifier:'nomad-work',name:'NOMAD work',url:'https://pm.example/projects/nomad-work/work_packages'}]};"
            f"apDayStart=()=>{NOW - 3600};" + extra)


@needs_node
def test_every_row_gets_open_switch_and_run_in_that_order():
    r = run_node_rows(_rowjs("""
      const rows=Object.fromEntries(projData.projects.map(p=>[p.identifier, projRow({...p, open:1})]));
      console.log(JSON.stringify(rows));"""))
    for ident, row in r.items():
        head = row.split("<div class=aps>")[0]
        assert "Open in OpenProject" in head, ident
        assert f'href="https://pm.example/projects/{ident}/work_packages"' in head, ident
    rm = r["runematch"].split("<div class=aps>")[0]
    assert rm.index("Open in OpenProject") < rm.index("class=apsw") < rm.index("Stop</span>")
    assert 'aria-checked=true' in rm and "Autopilot ON" in rm
    # the line under the row keeps the status but not a second set of buttons
    line = r["runematch"].split("<div class=aps>")[1]
    assert 'class="apst running"' in line and "Stop</span>" not in line and "class=apsw" not in line


@needs_node
def test_a_board_without_an_autopilot_project_shows_them_disabled_and_open_still_works():
    r = run_node_rows(_rowjs("console.log(JSON.stringify(projRow({identifier:'nomad-work',name:'NOMAD work',"
                             "url:'https://pm.example/projects/nomad-work/work_packages',open:3})));"))
    assert "Open in OpenProject" in r and "target=_blank" in r
    sw = r[r.index("class=apsw"):]
    assert sw.index("disabled") < sw.index("</button>")
    assert 'title="not set up for autopilot"' in r
    run = r[r.index('class="apbtn run"'):]
    assert run.index("disabled") < run.index("</button>") and "onclick" not in run.split("</button>")[0]


@needs_node
def test_a_board_with_several_projects_puts_the_controls_on_each_line():
    r = run_node_rows(_rowjs("console.log(JSON.stringify({ctl: apRowCtl('civis'), lines: apRowLines('civis')}));"))
    assert r["ctl"] == ""
    for pid in ("civis-poc", "civis-site"):
        line = r["lines"].split(f'<div class=ap data-pid="{pid}">')[1].split("<div class=ap ")[0]
        assert "class=apsw" in line and "Run</span>" in line, pid


@needs_node
def test_the_switch_shows_off_for_a_project_never_switched_and_run_says_one_task():
    r = run_node_rows(_rowjs("console.log(JSON.stringify(apActs('civis-site', true)));"))
    assert "aria-checked=false" in r and "Autopilot OFF" in r
    assert "Run: the top-priority task only, then stop" in r


@needs_node
def test_without_the_supervisor_the_row_still_opens_openproject():
    r = run_node_rows("apData=null; apErr='autopilot service not running';"
                      "console.log(JSON.stringify(projRow({identifier:'runematch',name:'RuneMatch',url:'https://pm.example/p',open:1})));")
    assert "Open in OpenProject" in r and "autopilot status unavailable: autopilot service not running" in r
    assert r.count(" disabled") >= 2


@needs_node
def test_a_once_run_is_labelled_and_stop_still_asks_finish_or_kill():
    r = run_node_rows(_rowjs("""
      apData.projects.runematch.run_mode='once'; apData.projects.runematch.autopilot=false;
      apStop('runematch');
      console.log(JSON.stringify(projRow({identifier:'runematch',name:'RuneMatch',url:'https://pm.example/p',open:1})));"""))
    assert "one task</span>" in r
    assert "Let them finish" in r and "Kill them" in r


@needs_node
def test_the_switch_posts_on_and_off():
    r = run_node_rows(_rowjs("""
      const sent=[];
      global.fetch=async (url,opt)=>{ if(opt&&opt.method==='POST'){ sent.push([url, opt.body]);
          return {ok:true,status:200,text:async()=>'{"ok":true}'}; }
        return {ok:true,status:200,json:async()=>apData}; };
      global.document={getElementById:()=>({innerHTML:'',textContent:'',className:'',contains:()=>false})};
      apSwitchSet('civis-poc', true).then(()=>apSwitchSet('runematch', false)).then(()=>
        console.log(JSON.stringify({sent, msg: apMsgs['runematch']})));"""))
    assert r["sent"] == [["/api/autopilot/projects/civis-poc/autopilot", '{"on":true}'],
                         ["/api/autopilot/projects/runematch/autopilot", '{"on":false}']]
    assert r["msg"]["ok"] is True and "one task" in r["msg"]["text"]


# ── approve or reject a task in Awaiting approval (docs/prd/autopilot-approval.md 5.3) ──────────

def test_decide_posts_to_the_engine_and_a_poll_spares_the_answer_box():
    assert "apPost(`/tasks/${encodeURIComponent(d.id)}/decide`, {decision:d.decision, text})" in AP
    assert "if(!apDecideBusy()) drawProjects();" in AP
    rule = C[C.index(".ap .apdec textarea{"):C.index("}", C.index(".ap .apdec textarea{"))]
    assert "min-height:44px" in rule


@needs_node
def test_only_awaiting_approval_tasks_get_approve_and_reject():
    r = run_node(js_setup() + """
      apData.projects['civis-poc'].detail='#12 needs operator: Awaiting approval; #13 needs operator: Blocked with no open blocker';
      console.log(JSON.stringify({rm: apLine('runematch'), civis: apLine('civis-poc'),
        aw: [...apAwaiting(apData.projects['civis-poc'])]}));""")
    assert r["aw"] == ["12"]
    assert r["civis"].count(">Approve</button>") == 1 and r["civis"].count(">Reject</button>") == 1
    assert "data-id=\"12\" onclick=\"apDecideOpen(this.dataset.pid,this.dataset.id,'approve')\"" in r["civis"]
    assert 'data-id="13"' not in r["civis"]
    needs = r["rm"].split("class=nyou>needs you:")[1].split("</span>")[0]
    assert 'data-id="840"' in needs and 'data-id="841"' not in r["rm"]     # 841 waits on a task


@needs_node
def test_reject_needs_a_reason_and_approve_sends_the_answer():
    r = run_node(js_setup() + """
      const sent=[];
      global.fetch=async (url,opt)=>{ if(opt&&opt.method==='POST'){ sent.push([url, JSON.parse(opt.body)]);
          return {ok:true,status:200,text:async()=>JSON.stringify({ok:true,task:12,status:'Approved'})}; }
        return {ok:true,status:200,json:async()=>apData}; };
      global.document={activeElement:null, getElementById:()=>({innerHTML:'',textContent:'',className:'',contains:()=>false})};
      (async()=>{
        apDecideOpen('civis-poc','12','reject');
        const box=apLine('civis-poc');
        await apDecideSend('civis-poc'); const refused=apMsgs['civis-poc'];
        apDecideOpen('civis-poc','12','approve'); apDecideText({dataset:{pid:'civis-poc'}, value:'  (a)  '});
        const kept=apLine('civis-poc');
        await apDecideSend('civis-poc');
        console.log(JSON.stringify({box, refused, kept, sent, msg: apMsgs['civis-poc'], open: 'civis-poc' in apDecide}));
      })();""")
    assert "Reject #12" in r["box"] and "Send rejection" in r["box"]
    assert r["refused"]["ok"] is False and r["sent"][1:] == []
    assert "Approve #12" in r["kept"] and ">  (a)  </textarea>" in r["kept"]
    assert r["sent"] == [["/api/autopilot/tasks/12/decide", {"decision": "approve", "text": "(a)"}]]
    assert r["msg"]["ok"] is True and "Approved" in r["msg"]["text"] and r["open"] is False
