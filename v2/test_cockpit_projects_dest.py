"""#1176 · Projects destination: one filtered list, project detail at #projects/<id>, board only there.

Node tests run the cockpit's real JS, cut out of COCKPIT, with the DOM stubbed.
Run: see v2/test_cockpit_layout.py (the node tests skip where node is missing, as in the container).
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
from test_cockpit_run_detail import BOOT  # noqa: E402

C = server.COCKPIT
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node not installed (run on the host)")
LIST_JS = C[C.index("const PROJ_STATE_LABEL="):C.index("function drawProjDetail(")]


def test_the_projects_section_has_a_list_and_a_detail_and_the_detail_starts_hidden():
    sec = C[C.index("<section id=dest-projects"):]
    sec = sec[:sec.index("</section>")]
    assert "<div id=projects" in sec and "<div id=projsheet class=projsheet hidden>" in sec
    assert sec.index("id=projects") < sec.index("id=projdetail") < sec.index("id=pmboard")


def test_the_board_sits_inside_the_detail_not_beside_the_list():
    sheet = C[C.index("<div id=projsheet"):C.index("</section>", C.index("<div id=projsheet"))]
    assert "id=pmboard" in sheet and "id=projdetail" in sheet


def test_the_shell_hands_the_project_in_the_hash_to_the_detail():
    assert "projSheetSet(cur==='projects' && hp[1]" in C


def test_the_project_state_is_declared_before_the_shell_uses_it():
    assert C.index("let projSheet=null") < C.index("(function(){\n  const tabs=")


def test_the_board_is_fetched_for_the_open_project_only():
    assert "fetch('/pm-summary?project='+encodeURIComponent(id))" in C
    assert "pollBoard(); setInterval" not in C


def test_a_row_is_a_button_not_a_link():
    assert "<button type=button class=\"prow" in C and 'onclick="projOpen(this.dataset.ident)"' in C


@needs_node
def test_filter_chips_count_each_state_and_a_row_shows_what_needs_the_operator():
    js = LIST_JS + r"""
const esc=s=>String(s), escA=s=>String(s), ic=()=>'';
const RUN_ORDER=['running','needs_you','waiting','budget_paused','error','idle','off'];
let projFilter='all', projData=null;
function drawProjects(){}
let apData={boards:{a:['A'],b:['B']},projects:{A:{state:'needs_you'},B:{state:'idle'}}};
function apPidsFor(b){ return apData.boards[b]||[]; }
function apProj(pid){ return apData.projects[pid]||{state:'off'}; }
function apNeedsYou(p){ return p.state==='needs_you'?[1,2]:[]; }
const list=[{identifier:'a',name:'Alpha',open:3,by_priority:{High:1}},{identifier:'z',name:'Zed',open:0}];
const bar=projFilterBar(list), row=projListRow(list[0]), zero=projListRow(list[1]);
console.log(JSON.stringify({bar,row,zero,sa:projState('a'),sz:projState('z')}));
"""
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    d = json.loads(out.stdout)
    assert d["sa"] == "needs_you" and d["sz"] == "off"
    assert "ALL<span class=ct>2</span>" in d["bar"] and "NEEDS YOU<span class=ct>1</span>" in d["bar"]
    assert "IDLE" not in d["bar"]
    assert "NEEDS YOU" in d["row"] and "3 open" in d["row"] and "1 high" in d["row"] and "2 need you" in d["row"] or "2 needs you" in d["row"]
    assert "prow zero" in d["zero"] and 'data-ident="z"' in d["zero"]


@needs_node
@pytest.mark.parametrize("hash_", ["#projects", "#projects/nomad", "#projects/no%20such"])
def test_the_whole_page_script_runs_to_its_end_with_a_project_open(hash_):
    js = max(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", C, re.S), key=len)
    boot = BOOT.replace("hash:'#projects'", "hash:" + json.dumps(hash_))
    out = subprocess.run([NODE, "-e", boot + "try{ (0,eval)(" + json.dumps(js) + "); console.log('ok'); }"
                          "catch(e){ console.log('THROW '+e.name+': '+e.message); }"],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "ok", out.stdout + out.stderr
