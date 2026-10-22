"""#1387 · the ask card for Open WebUI chats (docs/prd/project-types-and-inline-asks.md §6.1).

build_card(ask, answered=None, theme=None) -> one self-contained HTML string (inline CSS and JS, no
network). OWUI renders it as an `embeds` iframe (opaque srcdoc origin). The card answers by
    parent.postMessage({type: 'input:prompt:submit', text: 'answer <ask_id> <json>'}, '*')
and sizes itself with {type: 'iframe:height', height}.

ask = {
  "id": str,
  "kind": "questions" | "approve",          # anything else is treated as "questions"
  # kind "questions" (AskUserQuestion shape). One question = one tab. A question without options is
  # free text. multiSelect picks single (radio) or multi (checkbox).
  "questions": [{"header", "question", "multiSelect": bool, "options": [{"label", "description"}]}],
  # kind "approve"
  "tool": str, "summary": str, "reason": str,
}

Answer payloads (the json after `answer <id> `):
  questions:  {"answers": [{"question": <text>, "answer": <str> | [<str>, ...]}, ...]}
              str for single select and free text, list for multi select; "Other" is sent as the
              typed text itself.
  approve:    {"decision": "approve"}  or  {"decision": "deny", "note": <str>}
  Esc / close on either kind: {"decision": "deny", "note": "cancelled"}   (never approve)

answered (optional, read-only mode) uses the same shapes as the payloads above, e.g.
  {"answers": [{"question": ..., "answer": ...}]}  or  {"decision": "approve"|"deny", "note": ...}
In that mode the card has no controls at all.
"""
import json

CSS = """
:root{--bg:#1f1f1f;--fg:#e6e6e6;--mut:#9a9a9a;--bd:#3a3a3a;--ac:#4aa3ff;--ok:#3fb950;--bad:#f0645a;--hov:rgba(127,127,127,.14)}
:root[data-theme=light]{--bg:#fff;--fg:#1f1f1f;--mut:#666;--bd:#d0d0d0;--ac:#0a64d6;--ok:#1a7f37;--bad:#c93a30}
@media(prefers-color-scheme:light){:root:not([data-theme=dark]){--bg:#fff;--fg:#1f1f1f;--mut:#666;--bd:#d0d0d0;--ac:#0a64d6;--ok:#1a7f37;--bad:#c93a30}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
.card{border:1px solid var(--bd);border-radius:10px;padding:10px 14px;margin:2px}
.top{display:flex;align-items:center;gap:6px;min-width:0}
.tabs{display:flex;gap:16px;overflow-x:auto;flex:1;min-width:0;border-bottom:1px solid var(--bd);white-space:nowrap;scrollbar-width:thin}
.title{flex:1;min-width:0;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tab{padding:4px 0;color:var(--mut);cursor:pointer;border:0;border-bottom:2px solid transparent;background:none;font:inherit;flex:none}
.tab.on{color:var(--fg);border-bottom-color:var(--fg)}
.tab.done::after{content:" \\2713";color:var(--ok)}
.ib{flex:none;background:none;border:0;color:var(--mut);cursor:pointer;font:inherit;font-size:16px;line-height:1;padding:4px 6px;border-radius:6px}
.ib:hover{background:var(--hov);color:var(--fg)}
.collapsed .content{display:none}
.panel{margin-top:10px}
.panel.stack{margin-top:0;padding:8px 0;border-bottom:1px solid var(--bd)}.panel.stack:last-child{border-bottom:0}
.hd{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.q{font-weight:600;margin:2px 0 8px;overflow-wrap:anywhere}
.opt{display:flex;gap:10px;align-items:flex-start;padding:6px 6px;cursor:pointer;border-radius:6px}
.opt:hover{background:var(--hov)}
.opt input{margin:3px 0 0;flex:none;accent-color:var(--ac)}
.opt b{font-weight:600}.opt small{display:block;color:var(--mut);overflow-wrap:anywhere}
.opt span{min-width:0;overflow-wrap:anywhere}
.locked .opt{cursor:default}.locked .opt:not(.sel){opacity:.55}.locked .opt.sel{background:var(--hov);box-shadow:inset 3px 0 var(--ac)}.locked .opt:hover{background:none}
textarea,input[type=text]{width:100%;background:transparent;color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;font:inherit;resize:vertical}
input[type=text]{margin:2px 0 4px 0}
.oth{display:none;padding-left:28px}.oth.show{display:block}
.row{display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap}.sp{flex:1}
button.go{background:var(--ac);color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
button.go[disabled]{opacity:.4;cursor:default}
button.no{background:none;color:var(--bad);border:1px solid var(--bad);border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
button[disabled]{cursor:default}
.hint{color:var(--mut);font-size:12px;margin-top:6px;overflow-wrap:anywhere}
.sum{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;background:var(--hov);border-radius:6px;padding:6px 8px;margin:6px 0;overflow-wrap:anywhere;white-space:pre-wrap}
.res{font-weight:600;margin-top:6px}.res.approve{color:var(--ok)}.res.deny{color:var(--bad)}
.ans{color:var(--fg);white-space:pre-wrap;overflow-wrap:anywhere}
"""

JS = r"""
(function(){
var ASK=JSON.parse(document.getElementById('ask').textContent);
var DONE=ASK.answered||null;
var A=ASK.kind==='approve'?null:(ASK.questions||[]);
var st=[],cur=0,sent=false;
function $(s,r){return (r||document).querySelector(s)}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function post(m){try{parent.postMessage(m,'*')}catch(e){}}
function send(p){if(sent||DONE)return;sent=true;
  post({type:'input:prompt:submit',text:'answer '+ASK.id+' '+JSON.stringify(p)});lock()}
function deny(){send({decision:'deny',note:'cancelled'})}
function lock(){
  document.querySelector('.card').classList.add('locked');
  document.querySelectorAll('button:not(.ib),input,textarea').forEach(function(e){e.disabled=true});
  var ib=$('#close');if(ib)ib.disabled=true;
  var f=$('#foot');if(f)f.textContent='Answered. Waiting for the agent.'}
function ordered(q){var o=(q.options||[]).map(function(x,i){return {o:x,i:i}});
  return o.filter(function(x){return /\(Recommended\)/i.test(x.o.label||'')}).concat(o.filter(function(x){return !/\(Recommended\)/i.test(x.o.label||'')}))}
function valueOf(i){var q=A[i],s=st[i];
  if(!q.options||!q.options.length){var t=s.text.trim();return t?t:null}
  var v=[];q.options.forEach(function(o,j){if(s.sel[j])v.push(o.label)});
  var ot=s.other?s.otherText.trim():'';
  if(q.multiSelect){if(ot)v.push(ot);return v.length?v:null}
  if(s.other)return ot||null;
  return v.length?v[0]:null}
function count(){var n=0;for(var i=0;i<A.length;i++)if(valueOf(i)!==null)n++;return n}
function upd(){
  var n=count(),b=$('#sub');
  if(b){b.disabled=n!==A.length;b.textContent='Submit answers ('+n+'/'+A.length+')'}
  document.querySelectorAll('.tab').forEach(function(t,i){t.classList.toggle('done',valueOf(i)!==null)})}
function show(i){cur=i;
  document.querySelectorAll('.panel').forEach(function(p,k){p.hidden=k!==i});
  document.querySelectorAll('.tab').forEach(function(t,k){t.classList.toggle('on',k===i);t.setAttribute('aria-selected',k===i)});
  var t=$('.tab.on');if(t&&t.scrollIntoView)t.scrollIntoView({block:'nearest',inline:'nearest'})}
function preset(i,ans){var q=A[i],s=st[i];if(ans==null)return;
  var list=Array.isArray(ans)?ans:[ans];
  if(!q.options||!q.options.length){s.text=list.join(', ');return}
  list.forEach(function(a){var j=-1;q.options.forEach(function(o,k){if(o.label===a)j=k});
    if(j>=0)s.sel[j]=true;else{s.other=true;s.otherText=String(a)}})}
function panel(i,locked,stack){
  var q=A[i],s=st[i],p=el('div','panel'+(stack?' stack':''));
  if(stack&&q.header)p.appendChild(el('div','hd',q.header));
  p.appendChild(el('div','q',q.question||''));
  if(!q.options||!q.options.length){
    var ta=el('textarea');ta.rows=3;ta.placeholder='Type your answer';ta.value=s.text;ta.disabled=locked;
    ta.oninput=function(){s.text=ta.value;upd()};p.appendChild(ta);return p}
  var type=q.multiSelect?'checkbox':'radio',name='q'+i;
  var oth=el('div','oth'),ot=el('input');ot.type='text';ot.placeholder='Your answer';ot.value=s.otherText;ot.disabled=locked;
  function sync(){oth.classList.toggle('show',!!s.other)}
  ot.oninput=function(){s.otherText=ot.value;upd()};
  ordered(q).forEach(function(x){
    var l=el('label','opt'),inp=el('input');inp.type=type;inp.name=name;inp.checked=!!s.sel[x.i];inp.disabled=locked;if(locked&&s.sel[x.i])l.classList.add('sel');
    inp.onchange=function(){
      if(q.multiSelect)s.sel[x.i]=inp.checked;
      else{s.sel={};s.sel[x.i]=true;s.other=false;sync()}
      upd();
      if(!q.multiSelect&&A.length>1&&cur<A.length-1&&!locked)show(cur+1)};
    var sp=el('span');sp.appendChild(el('b',null,x.o.label||''));
    if(x.o.description)sp.appendChild(el('small',null,x.o.description));
    l.appendChild(inp);l.appendChild(sp);p.appendChild(l)});
  var lo=el('label','opt'),oi=el('input');oi.type=type;oi.name=name;oi.checked=!!s.other;oi.disabled=locked;if(locked&&s.other)lo.classList.add('sel');
  oi.onchange=function(){
    if(q.multiSelect)s.other=oi.checked;
    else{s.sel={};s.other=true}
    sync();upd();if(s.other&&!locked)ot.focus()};
  var os=el('span');os.appendChild(el('b',null,'Other'));lo.appendChild(oi);lo.appendChild(os);
  p.appendChild(lo);oth.appendChild(ot);p.appendChild(oth);sync();
  if(!q.multiSelect){ /* radios share a name; keep Other and the options mutually exclusive */
    p.addEventListener('change',function(e){if(e.target!==oi&&e.target.type==='radio'){oi.checked=false}})}
  return p}
function ib(id,txt,label,fn){var b=el('button','ib',txt);b.id=id;b.type='button';b.title=label;b.setAttribute('aria-label',label);b.onclick=fn;return b}
function collapseBtn(card){return ib('col','▾','Collapse',function(){
  var c=card.classList.toggle('collapsed');this.textContent=c?'▸':'▾';this.title=c?'Expand':'Collapse';sendH()})}
function build(){
  var card=$('.card'),body=$('#body');
  if(ASK.kind==='approve'){
    var top=el('div','top');top.appendChild(el('div','title','Approve '+(ASK.tool||'action')+'?'));
    if(!DONE){top.appendChild(collapseBtn(card));top.appendChild(ib('close','✕','Close (deny)',deny))}
    var c=el('div','content');
    if(ASK.summary)c.appendChild(el('div','sum',ASK.summary));
    if(ASK.reason)c.appendChild(el('div','hint','Reason: '+ASK.reason));
    if(DONE){card.classList.add('locked');
      c.appendChild(el('div','res '+(DONE.decision==='approve'?'approve':'deny'),DONE.decision==='approve'?'Approved':'Denied'));
      if(DONE.note)c.appendChild(el('div','hint','Note: '+DONE.note))}
    else{
      var note=el('textarea');note.id='note';note.rows=1;note.placeholder='Optional note for a deny';note.style.marginTop='8px';
      var row=el('div','row'),ap=el('button','go','Approve'),dn=el('button','no','Deny');ap.id='ap';dn.id='dn';ap.type=dn.type='button';
      ap.onclick=function(){send({decision:'approve'})};
      dn.onclick=function(){var n=note.value.trim(),p={decision:'deny'};if(n)p.note=n;send(p)};
      row.appendChild(ap);row.appendChild(dn);
      var foot=el('div','hint','Esc to deny');foot.id='foot';
      c.appendChild(note);c.appendChild(row);c.appendChild(foot)}
    body.appendChild(top);body.appendChild(c);return}
  A.forEach(function(q,i){st.push({sel:{},other:false,otherText:'',text:''})});
  if(DONE){card.classList.add('locked');
    var list=DONE.answers||[];A.forEach(function(q,i){var a=list[i];preset(i,a&&a.answer)});
    var c2=el('div','content');A.forEach(function(q,i){c2.appendChild(panel(i,true,true))});
    var t2=el('div','top');t2.appendChild(el('div','title','Answered'));body.appendChild(t2);body.appendChild(c2);return}
  var top2=el('div','top'),multi=A.length>1;
  if(multi){var tabs=el('div','tabs');tabs.setAttribute('role','tablist');
    A.forEach(function(q,i){var b=el('button','tab',q.header||('Q'+(i+1)));b.type='button';b.setAttribute('role','tab');b.onclick=function(){show(i)};tabs.appendChild(b)});
    top2.appendChild(tabs)}
  else top2.appendChild(el('div','title',(A[0]&&A[0].header)||'Question'));
  top2.appendChild(collapseBtn(card));top2.appendChild(ib('close','✕','Close (deny)',deny));
  var c3=el('div','content');
  A.forEach(function(q,i){c3.appendChild(panel(i,false,false))});
  var row2=el('div','row');row2.appendChild(el('span','sp'));
  var sub=el('button','go','Submit answers');sub.id='sub';sub.type='button';sub.disabled=true;
  sub.onclick=function(){if(count()!==A.length)return;
    send({answers:A.map(function(q,i){return {question:q.question,answer:valueOf(i)}})})};
  row2.appendChild(sub);c3.appendChild(row2);
  var foot2=el('div','hint','Esc to cancel (deny)');foot2.id='foot';c3.appendChild(foot2);
  body.appendChild(top2);body.appendChild(c3);show(0);upd()}
function sendH(){post({type:'iframe:height',height:Math.ceil(document.documentElement.getBoundingClientRect().height)+4})}
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!DONE){e.preventDefault();deny()}});
build();sendH();
if(window.ResizeObserver)new ResizeObserver(sendH).observe(document.body);
})();
"""


def _json_for_script(obj) -> str:
    """JSON that is safe inside <script type=application/json>: no raw <, > or & (so no `</script>`
    or `<!--` can end the element) and no raw U+2028/2029. Still valid JSON; JSON.parse restores it."""
    s = json.dumps(obj, ensure_ascii=True)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _normalise(ask: dict) -> dict:
    a = dict(ask)
    a["id"] = str(a.get("id", ""))
    a["kind"] = "approve" if a.get("kind") == "approve" else "questions"
    if a["kind"] == "questions":
        qs = []
        for q in a.get("questions") or []:
            qs.append({
                "header": str(q.get("header") or ""),
                "question": str(q.get("question") or ""),
                "multiSelect": bool(q.get("multiSelect")),
                "options": [{"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
                            for o in (q.get("options") or [])],
            })
        a["questions"] = qs
    return a


def build_card(ask: dict, answered: dict | None = None, theme: str | None = None) -> str:
    """Return the card HTML. `answered` renders it read-only. `theme` forces "light" or "dark"
    (default: follow the browser's prefers-color-scheme)."""
    data = _normalise(ask)
    if answered is not None:
        data["answered"] = answered
    attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    return ("<!doctype html><html" + attr + "><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<style>{CSS}</style></head><body><div class=card><div id=body></div></div>"
            f"<script id=ask type=application/json>{_json_for_script(data)}</script>"
            f"<script>{JS}</script></body></html>")


SAMPLE_Q = {"id": "a1b2", "kind": "questions", "questions": [
    {"header": "Breakpoints", "question": "#1314: approve the derived breakpoints?", "multiSelect": False,
     "options": [{"label": "Change values", "description": "Tell me the values in Other."},
                 {"label": "Approve as built (Recommended)", "description": "Already on master. I close #1314."}]},
    {"header": "Copy work", "question": "Which pages get new copy?", "multiSelect": True,
     "options": [{"label": "Home", "description": "hero + CTA"}, {"label": "About", "description": ""}]},
    {"header": "Notes", "question": "Anything else I should know?", "multiSelect": False, "options": []}]}
SAMPLE_SINGLE = {"id": "s1", "kind": "questions", "questions": [SAMPLE_Q["questions"][0]]}
SAMPLE_MULTI = {"id": "m1", "kind": "questions", "questions": [SAMPLE_Q["questions"][1]]}
SAMPLE_TEXT = {"id": "t1", "kind": "questions", "questions": [SAMPLE_Q["questions"][2]]}
SAMPLE_APPROVE = {"id": "c3d4", "kind": "approve", "tool": "Bash", "summary": "git push origin feat/x",
                  "reason": "Red tier: git push needs the operator's OK."}
SAMPLE_4TABS = {"id": "f4", "kind": "questions", "questions": SAMPLE_Q["questions"] + [
    {"header": "Launch date", "question": "When should this ship?", "multiSelect": False,
     "options": [{"label": "This week (Recommended)", "description": "Before the Friday freeze."},
                 {"label": "Next sprint", "description": "After the review."}]}]}
