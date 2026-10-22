"""#1175 · the Now queue, run in node: the cockpit's real JS with fetch and the DOM stubbed.

Run: see v2/test_cockpit_layout.py (skips where node is missing, as in the container).
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
NOW_JS = C[C.index("let nowRuns=[];"):C.index("function nowPick(")]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node not installed (run on the host)")

PRELUDE = r"""
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function ic(name){ return `<svg class=ic aria-hidden=true><use href="#i-${name}"/></svg>`; }
const escA=s=>esc(String(s??'')).replaceAll('"','&quot;');
function ldot(l){ return `<i class=ldot></i>`; }
function apAge(s){ return Math.round(s)+' s'; }
function apTask(id){ return `<a class=wp>#${id}</a>`; }
function runTime(r){ return Date.parse(r.created_at)||0; }
const AP_LANE='autopilot';
function apNeedsYou(p){ return p.needs||[]; }
function apAwaiting(p){ return new Set(p.awaiting||[]); }
const badges=[];
function navBadge(id,n){ badges.push([id,n]); }
const els={nowq:{innerHTML:''},nowstatus:{innerHTML:''}};
const document={getElementById:id=>els[id]||null,activeElement:null};
"""


def draw(runs, projects, question=None):
    script = PRELUDE + f"""
let apData={json.dumps({"projects": projects, "boards": {}})};
globalThis.fetch=async u=>({{ok:true,json:async()=>({json.dumps(question or {})})}});
""" + NOW_JS + f"""
nowRuns={json.dumps(runs)};
drawNow();
setTimeout(()=>console.log(JSON.stringify({{html:els.nowq.innerHTML,status:els.nowstatus.innerHTML,badges}})),50);
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


RUN = {"run_id": "r9", "lane": "comms", "type": "email", "status": "awaiting-approval",
       "created_at": "2026-10-12T10:00:00Z",
       "proposal": {"action": "send_email", "target": "a@b.c", "preview": "Hello there", "proposal_hash": "h"}}
FAIL = {"run_id": "r8", "lane": "dev", "type": "code", "status": "failed", "created_at": "2026-10-12T09:00:00Z"}
ASK = {"ok": True, "id": "11534", "subject": "Pick the art pack", "stem": "Which pack?", "complex": False,
       "options": [{"key": "1", "text": "Itch.io, 12 USD"}, {"key": "2", "text": "Unity store, 20 USD"}]}


@needs_node
def test_an_empty_queue_says_so():
    j = draw([], {})
    assert "Nothing waits on you." in j["html"] and ["nb-now", 0] in j["badges"]


@needs_node
def test_a_run_at_the_gate_and_a_failed_run_get_cards_and_the_badge_counts_both():
    j = draw([RUN, FAIL], {})
    assert "send_email" in j["html"] and "gate(this.dataset.rid,'approved')" in j["html"]
    assert "retry(this.dataset.rid)" in j["html"]
    assert ["nb-now", 2] in j["badges"]


@needs_node
def test_a_task_shows_its_options_whole_and_approve_waits_for_a_pick():
    j = draw([], {"runematch": {"needs": ["11534"], "awaiting": ["11534"]}}, ASK)
    assert "Itch.io, 12 USD" in j["html"] and "Unity store, 20 USD" in j["html"]
    assert "Pick an option first" in j["html"]
    assert 'class="nq-btn go"' in j["html"] and "disabled" in j["html"]


@needs_node
def test_a_complex_question_points_at_the_ticket():
    q = {**ASK, "complex": True, "options": [], "stem": "A long question…"}
    j = draw([], {"p": {"needs": ["7"], "awaiting": ["7"]}}, q)
    assert "Read it in the ticket before you answer." in j["html"]


@needs_node
def test_a_blocked_task_only_links():
    j = draw([], {"p": {"needs": ["7"], "awaiting": []}})
    assert "Blocked: open the ticket" in j["html"] and "nowSend" not in j["html"]


def test_an_answer_without_options_does_not_break_the_queue():
    """The question endpoint's body is read as data; a missing `options` must not throw in drawNow."""
    assert "{...d,options:d.options||[]}" in C


# ---- agent asks (#1363): pending bridge asks are cards in the Now queue -------------------------

ASK_JS = C[C.index("function nowAskPick("):C.index("nowLoadAsks(); setInterval")]
QASK = {"ask_id": "ask-1", "kind": "question", "tool": "AskUserQuestion", "state": "pending",
        "created": 1792155695.4, "summary": "{\"questions\": …}",
        "questions": [{"question": "Which colour?", "header": "Colour", "multiSelect": False,
                       "options": [{"label": "Red", "description": "Red"}, {"label": "Blue", "description": "Cool"}]},
                      {"question": "Why?", "header": "Reason", "options": []}]}
PASK = {"ask_id": "ask-2", "kind": "permission", "tool": "Write", "state": "pending", "created": 1792155600,
        "summary": "colour.txt", "reason": "writes outside the scratch dir"}


def ask_script(asks, steps=""):
    return PRELUDE + """
let apData={"projects":{},"boards":{}};
const posts=[];
globalThis.fetch=async (u,o)=>{ if(o&&o.method==='POST'){ posts.push([u,JSON.parse(o.body)]); return {status:200,json:async()=>({ok:true})}; }
  return {ok:true,json:async()=>({})}; };
""" + NOW_JS + ASK_JS + f"""
nowAsks={json.dumps(asks)};
drawNow();
(async()=>{{ {steps}
setTimeout(()=>console.log(JSON.stringify({{html:els.nowq.innerHTML,status:els.nowstatus.innerHTML,badges,posts,
  state:nowAskS,left:nowAsks.map(a=>a.ask_id)}})),50); }})();
"""


def run_asks(asks, steps=""):
    out = subprocess.run([NODE, "-e", ask_script(asks, steps)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def pick(ask, q, o):
    return f"nowAskPick({{dataset:{{id:'{ask}',q:'{q}',o:'{o}'}}}});"


def text(ask, q, v):
    return f"nowAskText({{dataset:{{id:'{ask}',q:'{q}'}},value:{json.dumps(v)}}});"


@needs_node
def test_asks_sit_on_top_of_the_queue_and_count_in_the_badge():
    j = run_asks([QASK, PASK])
    h = j["html"]
    assert h.index('data-ask="ask-2"') < h.index('data-ask="ask-1"')       # oldest first
    assert "Colour: Which colour?" in h and "<b>Blue</b><span>Cool</span>" in h and "<b>Red</b></button>" in h
    assert "Your answer" in h and "Other (optional)" in h
    assert "Write: colour.txt" in h and "writes outside the scratch dir" in h and "Deny note" in h
    assert "{\\\"questions" not in h and '{"questions' not in h              # the raw summary of a question stays out
    assert ["nb-now", 2] in j["badges"] and "2 waiting on you" in j["status"]


@needs_node
def test_submit_waits_for_every_question():
    j = run_asks([QASK], pick("ask-1", 0, 1) + "await nowAskSend('ask-1','approve');")
    assert j["posts"] == [] and "Answer every question first." in j["html"]


@needs_node
def test_submit_posts_the_answers_and_the_card_leaves():
    steps = pick("ask-1", 0, 1) + text("ask-1", 1, "  it is calm ") + "await nowAskSend('ask-1','approve');"
    j = run_asks([QASK], steps)
    assert j["posts"] == [["/api/asks/decide", {"ask_id": "ask-1", "decision": "approve",
                                                 "answers": {"Which colour?": "Blue", "Why?": "it is calm"}}]]
    assert j["left"] == [] and "Nothing waits on you." in j["html"]


@needs_node
def test_typed_other_wins_on_a_single_choice_and_a_pick_shows_checked():
    j = run_asks([QASK], pick("ask-1", 0, 0) + "drawNow();")
    assert 'aria-checked=true data-id="ask-1" data-q="0" data-o="0"' in j["html"]
    steps = pick("ask-1", 0, 0) + text("ask-1", 0, "Teal") + text("ask-1", 1, "x") + "await nowAskSend('ask-1','approve');"
    assert run_asks([QASK], steps)["posts"][0][1]["answers"]["Which colour?"] == "Teal"


@needs_node
def test_multi_select_joins_picks_and_other():
    q = {**QASK, "questions": [{**QASK["questions"][0], "multiSelect": True}]}
    steps = pick("ask-1", 0, 0) + pick("ask-1", 0, 1) + text("ask-1", 0, "Teal") + "await nowAskSend('ask-1','approve');"
    assert run_asks([q], steps)["posts"][0][1]["answers"] == {"Which colour?": "Red, Blue, Teal"}


@needs_node
def test_permission_deny_carries_the_note_and_approve_none():
    j = run_asks([PASK], text("ask-2", "note", " not there ") + "await nowAskSend('ask-2','deny');")
    assert j["posts"][0][1] == {"ask_id": "ask-2", "decision": "deny", "answers": {"_note": "not there"}}
    j = run_asks([PASK], "await nowAskSend('ask-2','approve');")
    assert j["posts"][0][1] == {"ask_id": "ask-2", "decision": "approve"}


@needs_node
def test_an_expired_ask_says_so():
    steps = """globalThis.fetch=async()=>({status:409,json:async()=>({ok:false,state:'expired'})});
await nowAskSend('ask-2','approve');"""
    j = run_asks([PASK], steps)
    assert "expired when the bridge restarted" in j["html"] and j["left"] == ["ask-2"]


def test_the_cockpit_polls_pending_asks():
    assert "fetch('/api/asks?state=pending')" in C and "setInterval(nowLoadAsks, 5000)" in C
