"""#1363 · the cockpit's "Pending asks" view: the second answer channel for inline asks (PRD
project-types-and-inline-asks §6.1, A1). The agent bridge (agent-bridge/, host :8093) holds each
permission request or question as a durable ask; this lists them and posts the operator's answer
to `POST /agent/decide`, which continues the run.

The page talks to the engine, and the engine adds the bridge token (the same owui token the pipe
uses, `owui.token()`), so the token never reaches the browser. Stdlib only, so it tests without
the engine's dependencies.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import owui

BRIDGE_URL = os.environ.get("NOMAD_AGENT_BRIDGE_URL", "http://172.17.0.1:8093").rstrip("/")
TIMEOUT = float(os.environ.get("NOMAD_AGENT_BRIDGE_TIMEOUT", "5"))
STATES = ("pending", "answered", "timeout", "cancelled", "expired")
ASK_ID = re.compile(r"^ask-[0-9a-f]{4,32}$")
DOWN = "agent bridge not reachable"


def _call(method: str, path: str, body: dict | None = None) -> tuple:
    """(status, json) from the bridge. Never raises: a bridge that is down is a 503 the page prints."""
    tok = owui.token()
    if not tok:
        return 503, {"ok": False, "error": "no bridge token available to the engine",
                     "detail": "set NOMAD_OWUI_TOKEN or create ~/.config/nomad/owui-token"}
    req = urllib.request.Request(
        BRIDGE_URL + path, method=method,
        headers={"X-Nomad-Token": tok, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read() or b"{}")
        except Exception:
            out = {}
        if e.code == 401:                     # not passed through: the browser would think its login failed
            return 502, {"ok": False, "error": "the bridge refused the engine's token"}
        if not isinstance(out, dict) or not out:
            out = {"ok": False, "error": f"bridge returned {e.code}"}
        return e.code, out
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 503, {"ok": False, "error": DOWN, "detail": str(getattr(e, "reason", e))[:200]}
    except ValueError:
        return 502, {"ok": False, "error": "the bridge sent a reply that is not JSON"}


def list_asks(state: str = "") -> tuple:
    if state and state not in STATES:
        return 400, {"ok": False, "error": f"state must be one of {', '.join(STATES)}"}
    q = "?" + urllib.parse.urlencode({"state": state}) if state else ""
    code, out = _call("GET", "/agent/pending" + q)
    if code != 200:
        return code, out
    rows = out.get("pending") if isinstance(out, dict) else None
    # newest first, and what is waiting on the operator stays on top (sort is stable)
    rows = sorted(rows if isinstance(rows, list) else [], key=lambda r: str(r.get("created") or ""), reverse=True)
    rows.sort(key=lambda r: r.get("state") != "pending")
    return 200, {"ok": True, "asks": rows}


def decide(req: dict) -> tuple:
    """Validate the page's answer and forward it. The bridge's 409 (already answered, expired) passes through."""
    ask_id = str((req or {}).get("ask_id") or "")
    decision = str((req or {}).get("decision") or "")
    if not ASK_ID.match(ask_id):
        return 400, {"ok": False, "error": "ask_id missing or malformed"}
    if decision not in ("approve", "deny"):
        return 400, {"ok": False, "error": "decision must be approve or deny"}
    body = {"ask_id": ask_id, "decision": decision}
    answers = req.get("answers")
    if answers is not None:
        if not isinstance(answers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                    for k, v in answers.items()):
            return 400, {"ok": False, "error": "answers must be {question: answer text}"}
        body["answers"] = answers
    return _call("POST", "/agent/decide", body)


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>NOMAD · Pending asks</title>
<style>
*{box-sizing:border-box}
body{background:#05060a;color:#ffb000;font-family:ui-monospace,'Share Tech Mono',monospace;margin:0 auto;
  padding:14px 16px 40px;font-size:14px;line-height:1.45;max-width:1000px;overflow-wrap:anywhere}
a{color:#56b6ff}
.top{display:flex;align-items:center;gap:12px;margin:0 0 12px;font-size:12px;letter-spacing:.12em}
.top a{display:inline-flex;align-items:center;min-height:44px;color:#9aa;text-decoration:none}
h1{font-size:15px;letter-spacing:.2em;margin:0;font-weight:400;flex:1}
select,button,input[type=text]{font:inherit;color:inherit;background:#0a0b12;border:1px solid #2a2a3a;
  border-radius:7px;padding:6px 10px;min-height:40px}
button{cursor:pointer}button:disabled{opacity:.4;cursor:default}
.ok{border-color:#33dd88;color:#33dd88}.no{border-color:#e3554f;color:#e3554f}
table{width:100%;border-collapse:collapse}
th{color:#56b6ff;font-size:11px;letter-spacing:.1em;text-align:left;font-weight:400;padding:4px 8px}
td{border-top:1px solid #1b1b27;padding:9px 8px;vertical-align:top}
.tag{padding:2px 9px;border-radius:9px;font-size:12px;color:#000;font-weight:700;background:#445}
.tag.pending{background:#ffb000}.tag.answered{background:#33dd88}.tag.timeout,.tag.expired,.tag.cancelled{background:#667;color:#ddd}
.settled td{opacity:.65}
.q{margin:6px 0 10px}.q b{display:block}.opt{display:block;padding:3px 0}
.opt small{opacity:.7;display:block;margin-left:24px}
.act{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.msg{margin-top:6px;font-size:13px}.msg.err{color:#e3554f}
#status{opacity:.7;font-size:12px;margin:0 0 10px}
@media(max-width:640px){thead{display:none}tr,td{display:block}td{border:0;padding:3px 0}
  tr{border-top:1px solid #1b1b27;padding:9px 0}td::before{content:attr(data-h) ": ";color:#56b6ff;font-size:11px}}
</style></head><body>
<div class=top><a href="/cockpit">&larr; COCKPIT</a><h1>PENDING ASKS</h1>
<select id=flt aria-label="filter by state"><option value=pending>pending</option><option value="">all</option>
<option>answered</option><option>timeout</option><option>cancelled</option><option>expired</option></select></div>
<p id=status>loading...</p>
<table><thead><tr><th>Chat</th><th>Kind</th><th>Tool / summary</th><th>Created</th><th>State</th></tr></thead>
<tbody id=rows></tbody></table>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
let busy=false;
async function post(body){
  const r=await fetch('/api/asks/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let j={};try{j=await r.json()}catch(e){}
  return {status:r.status,j};
}
function say(box,text,err){box.textContent=text;box.className='msg'+(err?' err':'')}
function fail(box,res){
  const e=(res.j&&res.j.error)||('error '+res.status);
  if(res.status===409&&e==='already answered')say(box,'Already answered elsewhere (chat or another tab). Nothing was changed.',true);
  else if(res.status===409&&res.j.state==='expired')say(box,'This ask expired when the bridge restarted. Nothing was applied.',true);
  else say(box,e,true);
}
async function send(body,box,btns){
  busy=true;btns.forEach(b=>b.disabled=true);say(box,'sending...');
  const res=await post(body);
  if(res.status===200&&res.j.ok){say(box,'Sent. The run continues.');busy=false;setTimeout(load,800);return}
  fail(box,res);busy=false;if(res.status!==409)btns.forEach(b=>b.disabled=false);
}
function permission(a,td){
  const msg=el('div','msg'),note=el('input');note.type='text';note.placeholder='note on deny (optional)';note.setAttribute('aria-label','deny note');
  const ap=el('button','ok','Approve'),dn=el('button','no','Deny'),act=el('div','act');
  ap.onclick=()=>send({ask_id:a.ask_id,decision:'approve'},msg,[ap,dn]);
  dn.onclick=()=>send({ask_id:a.ask_id,decision:'deny',answers:note.value.trim()?{_note:note.value.trim()}:undefined},msg,[ap,dn]);
  act.append(ap,dn,note);td.append(act,msg);
}
function question(a,td){
  const msg=el('div','msg'),getters=[];
  (a.questions||[]).forEach((q,qi)=>{
    const box=el('div','q');box.append(el('b','',(q.header?q.header+': ':'')+q.question));
    const multi=!!q.multiSelect,name='q'+a.ask_id+qi,inputs=[],opts=q.options||[];
    opts.forEach(o=>{
      const l=el('label','opt'),i=el('input');i.type=multi?'checkbox':'radio';i.name=name;i.value=o.label;
      l.append(i,document.createTextNode(' '+o.label));if(o.description)l.append(el('small','',o.description));
      box.append(l);inputs.push(i);
    });
    const ol=el('label','opt'),oi=el('input');oi.type=multi?'checkbox':'radio';oi.name=name;
    const ot=el('input');ot.type='text';ot.placeholder='Other';ot.setAttribute('aria-label','Other answer');
    ot.oninput=()=>{if(ot.value)oi.checked=true};
    if(opts.length)ol.append(oi,document.createTextNode(' Other '),ot);else ol.append(ot);
    box.append(ol);td.append(box);
    getters.push(()=>{
      const v=inputs.filter(i=>i.checked).map(i=>i.value);
      if(ot.value.trim()&&(oi.checked||!opts.length))v.push(ot.value.trim());
      return v.length?[q.question,v.join(', ')]:null;
    });
  });
  const sub=el('button','ok','Submit'),dn=el('button','no','Deny'),act=el('div','act');
  sub.onclick=()=>{
    const ans={};
    for(const g of getters){const r=g();if(!r){say(msg,'Answer every question first.',true);return}ans[r[0]]=r[1]}
    send({ask_id:a.ask_id,decision:'approve',answers:ans},msg,[sub,dn]);
  };
  dn.onclick=()=>send({ask_id:a.ask_id,decision:'deny'},msg,[sub,dn]);
  act.append(sub,dn);td.append(act,msg);
}
function readonly(a,td){
  if(a.answers&&typeof a.answers==='object')
    Object.entries(a.answers).forEach(([k,v])=>td.append(el('div','msg',k+' -> '+v)));
  else if(a.decision)td.append(el('div','msg','decision: '+(a.decision.decision||a.decision)));
}
function row(a){
  const tr=el('tr',a.state==='pending'?'':'settled');
  const c=(h,x)=>{const t=el('td','',x);t.dataset.h=h;tr.append(t);return t};
  c('Chat',a.chat_id||'');c('Kind',a.kind||'');
  const td=c('Ask',(a.tool?a.tool+(a.summary?': ':''):'')+(a.summary||''));
  if(a.reason)td.append(el('div','msg',a.reason));
  c('Created',a.created||'');
  const st=c('State');st.append(el('span','tag '+a.state,a.state));
  if(a.state==='pending'){a.kind==='question'?question(a,td):permission(a,td)}else readonly(a,td);
  return tr;
}
async function load(){
  if(busy)return;
  const f=$('flt').value;
  let r;try{r=await fetch('/api/asks'+(f?'?state='+f:''))}catch(e){$('status').textContent='engine unreachable';return}
  const j=await r.json().catch(()=>({}));
  if(!r.ok){$('status').textContent=(j.error||'error '+r.status)+(j.detail?' ('+j.detail+')':'');return}
  if(document.activeElement&&$('rows').contains(document.activeElement))return;   // never redraw under a field being typed in
  $('rows').replaceChildren(...j.asks.map(row));
  $('status').textContent=j.asks.length?j.asks.length+' ask(s)':'No asks in this view.';
}
$('flt').onchange=()=>{busy=false;load()};
load();setInterval(load,5000);
</script></body></html>"""
