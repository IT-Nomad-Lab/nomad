"""#1378 · the cockpit's Standards page (PRD project-types-and-inline-asks §6.3): the project types
(profiles/*.yaml), a form per profile with a raw YAML view, policy pickers that show the MCPs,
skills and tools each policy grants, and a dry run of what a new project of the type gets.

The page only calls the engine's /api/profiles routes (#1377), which proxy to the dispatcher with
its token, so no token reaches the browser. Check and Dry run send the text to be parsed, never
saved. Save (#1379) commits it on branch profile/<id>, never on master, after the same validation,
and shows the diff against master; merging the branch is a review step. A profile names policies, not
MCPs or skills (§6.5), so the picker is a policy picker and a filter finds the policy that grants a
given server or skill.

Served from a proxy (the phone on the tailnet), the editor is read-only, Save is hidden and the
tailnet refuses the POST anyway, and the dry run covers the
version on master, through the GET route the tailnet allows.
"""

PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>NOMAD · Standards</title>
<style>
*{box-sizing:border-box}
body{background:#05060a;color:#ffb000;font-family:ui-monospace,'Share Tech Mono',monospace;margin:0 auto;
  padding:14px 16px 40px;font-size:14px;line-height:1.45;max-width:1100px;overflow-wrap:anywhere}
a{color:#56b6ff}
.top{display:flex;align-items:center;gap:12px;margin:0 0 12px;font-size:12px;letter-spacing:.12em;flex-wrap:wrap}
.top a{display:inline-flex;align-items:center;min-height:44px;color:#9aa;text-decoration:none}
h1{font-size:15px;letter-spacing:.2em;margin:0;font-weight:400;flex:1}
h2{font-size:12px;letter-spacing:.14em;color:#56b6ff;font-weight:400;margin:18px 0 8px}
select,button,input[type=text],input[type=number],textarea{font:inherit;color:inherit;background:#0a0b12;border:1px solid #2a2a3a;
  border-radius:7px;padding:6px 10px;min-height:40px;max-width:100%}
textarea{width:100%;resize:vertical}
#yaml{min-height:420px;white-space:pre;overflow-wrap:normal;overflow-x:auto}
button{cursor:pointer}button:disabled{opacity:.4;cursor:default}
button.on{border-color:#ffb000;background:#1a1405}
.ok{border-color:#33dd88;color:#33dd88}.no{border-color:#e3554f;color:#e3554f}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
table{width:100%;border-collapse:collapse}
th{color:#56b6ff;font-size:11px;letter-spacing:.1em;text-align:left;font-weight:400;padding:4px 8px;white-space:nowrap}
td{border-top:1px solid #1b1b27;padding:8px;vertical-align:top}
tr.pick{cursor:pointer}tr.pick:hover td{background:#0d0e17}tr.sel td{background:#141320}
.tag{padding:2px 9px;border-radius:9px;font-size:12px;color:#000;font-weight:700;background:#445;white-space:nowrap}
.tag.allow{background:#33dd88}.tag.deny{background:#e3554f}.tag.ask{background:#ffb000}.tag.bad{background:#e3554f}
.tag.pend{background:#b388ff}
.mut{opacity:.65}.small{font-size:12px}
.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
.row label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:#56b6ff;letter-spacing:.06em}
.row label>*{color:#ffb000;font-size:14px;letter-spacing:0}
.grow{flex:1;min-width:200px}.grow>*{width:100%}
.box{border:1px solid #1b1b27;border-radius:9px;padding:8px 10px;margin:8px 0}
.pol{display:flex;gap:10px;align-items:flex-start;border:1px solid #1b1b27;border-radius:9px;padding:8px 10px;margin:6px 0;cursor:pointer}
.pol input{width:22px;height:22px;margin:2px 0 0;flex:none}
.pol.on{border-color:#ffb000;background:#100d04}
.chk{display:inline-flex;align-items:center;gap:6px;min-height:40px;margin-right:12px;cursor:pointer}
.chk input{width:20px;height:20px}
.msg{margin:8px 0;font-size:13px;white-space:pre-wrap}.msg.err{color:#e3554f}.msg.good{color:#33dd88}
pre.diff{background:#0a0b12;border:1px solid #1b1b27;border-radius:7px;padding:8px;overflow-x:auto;
  font-size:12px;white-space:pre;overflow-wrap:normal;max-height:420px;color:#ccc}
.notice{border:1px solid #2a2a3a;border-radius:9px;padding:8px 10px;margin:8px 0;font-size:13px}
#status{opacity:.7;font-size:12px;margin:0 0 10px}
.big{font-size:16px;margin:6px 0}
body.remote .write{display:none !important}
@media(max-width:640px){thead{display:none}tr,td{display:block}td{border:0;padding:3px 0}
  tr{border-top:1px solid #1b1b27;padding:9px 0}td::before{content:attr(data-h) ": ";color:#56b6ff;font-size:11px}
  .grow{min-width:100%}#yaml{min-height:300px}
  #drow tr{padding:5px 0}#drow td{display:inline;padding:0 6px 0 0}#drow td::before{content:none}}
</style></head><body>
<div class=top><a href="/cockpit">&larr; COCKPIT</a><h1>STANDARDS</h1><a href="/cockpit/policies" target=_blank rel=noopener>POLICIES &rarr;</a></div>
<div class=tabs role=tablist>
<button id=t-list class=on data-view=list>Types</button>
<button id=t-edit data-view=edit>Editor</button>
<button id=t-dry data-view=dry>Dry run</button>
</div>
<p id=status>loading...</p>
<p id=ro class="notice mut" hidden>Read-only from the phone: the tailnet allows no profile POST. The dry run shows the version on master; edit and check from the host.</p>

<section id=v-list>
<p class="small mut">A project type says what a new project of that type gets: interview questions, stack, policies, agents, gates, files and autopilot defaults. Pick one to edit it or dry-run it.</p>
<table><thead><tr><th>Type</th><th>Policies</th><th>Projects</th><th>State</th></tr></thead>
<tbody id=trow></tbody></table>
<div class="row write"><input type=text id=newid placeholder="new type id (a-z, 0-9, -)" aria-label="new type id">
<button id=newbtn>New type</button></div>
</section>

<section id=v-edit hidden>
<div class=row><label class=grow>Type<select id=epick aria-label="type to edit"></select></label></div>
<div id=enote></div>
<div class=tabs><button id=m-form class=on>Form</button><button id=m-yaml>YAML</button></div>
<div id=f-form>
<div class=row><label class=grow>id<input type=text id=f-id readonly></label>
<label class=grow>title<input type=text id=f-title></label></div>
<div class=row><label class=grow>description<textarea id=f-desc rows=2></textarea></label></div>

<h2>POLICIES</h2>
<p class="small mut">A type grants MCP servers, skills and tools only through policies. Tick the ones a new project of this type gets; the filter finds the policy that grants a server or skill. Guardrails apply to every project and are not listed.</p>
<div class=row><label class=grow>Find a server, skill or policy<input type=text id=pflt placeholder="figma, graphify, postgres..." aria-label="filter policies"></label></div>
<div id=f-pols></div>
<p class="small mut">A server or skill no policy grants needs a new policy first: <a href="/cockpit/policies" target=_blank rel=noopener>open Policies</a>.</p>

<h2>INTERVIEW</h2>
<p class="small mut">Questions + New project asks after the type, in order. Kind: single or multi takes options, one per line.</p>
<div id=f-ask></div>
<div class="row write"><button id=f-addq>Add question</button></div>
<div class=row><label class=grow>skip (generic question ids, comma separated)<input type=text id=f-skip></label></div>

<h2>AGENTS</h2>
<div id=f-agents></div>
<div class="row write"><button id=f-adda>Add agent</button></div>

<h2>GATES</h2>
<div class=row><label class=grow>pre-merge, one step per line<textarea id=f-pre rows=3></textarea></label>
<label class=grow>milestone, one step per line<textarea id=f-mile rows=3></textarea></label></div>

<h2>STACK, FILES, AUTOPILOT</h2>
<div class=row><label class=grow>stack, one <code>key: value</code> per line (JSON or text)<textarea id=f-stack rows=3></textarea></label>
<label class=grow>required files, one per line<textarea id=f-files rows=3></textarea></label></div>
<div class=row><label>cap<input type=number id=f-cap min=1 step=1 placeholder="default"></label>
<label>model<select id=f-model aria-label="autopilot model"></select></label>
<label>max budget, USD<input type=number id=f-budget min=0 step=any placeholder="default"></label></div>
<div class=row id=f-lanes></div>
<p class="small mut">Saved from the form, a field you did not change keeps its text and comments; a field you changed is written fresh. Use the YAML view to control the layout.</p>
</div>
<div id=f-yaml hidden><textarea id=yaml spellcheck=false aria-label="profile YAML"></textarea></div>
<div class=row><button id=check class=write>Check</button><button id=dbtn class=ok>Dry run</button><button id=save class=write>Save to branch</button></div>
<p class="small mut write">Save commits the file on branch <code>profile/&lt;id&gt;</code>, never on master. Merging the branch is a review step.</p>
<div id=emsg class=msg></div><pre id=ediff class=diff hidden></pre>
</section>

<section id=v-dry hidden>
<div id=dhead></div>
<div id=dchg></div>
<h2>A NEW PROJECT OF THIS TYPE GETS</h2>
<div class=row><label class=grow>Filter<input type=text id=dflt placeholder="figma, gate, skill..." aria-label=filter></label>
<label>Show<select id=dkind aria-label="show which"></select></label></div>
<table><thead><tr><th>What</th><th>Detail</th></tr></thead><tbody id=drow></tbody></table>
<h2>PROJECTS.YAML ENTRY</h2><pre id=dentry class=diff></pre>
</section>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
const REMOTE=document.body.classList.contains('remote');
let META={profiles:[],choices:{}},CUR=null,MODE='form',DRY=null;
const VIEWS=['list','edit','dry'];
function say(box,text,kind){box.textContent=text;box.className='msg'+(kind?' '+kind:'')}
async function api(method,path,body){
  let r;try{r=await fetch('/api/profiles'+path,method==='GET'?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
  catch(e){return {status:0,j:{ok:false,error:'engine unreachable'}}}
  let j={};try{j=await r.json()}catch(e){}
  return {status:r.status,j};
}
const err=res=>(res.j&&res.j.error)||('error '+res.status);
function cell(tr,h,x){const t=el('td','',x);t.dataset.h=h;tr.append(t);return t}
function opts(sel,vals,keep){const v=keep?sel.value:'';sel.replaceChildren(...vals.map(x=>{const o=el('option','',x);o.value=x;return o}));if(keep&&vals.includes(v))sel.value=v}
const uniq=a=>a.filter((x,i)=>x&&a.indexOf(x)===i);
const lines=t=>t.split('\n').map(x=>x.trim()).filter(Boolean);
const commas=t=>t.split(',').map(x=>x.trim()).filter(Boolean);
function view(v){
  VIEWS.forEach(n=>{$('v-'+n).hidden=n!==v});
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('on',b.dataset.view===v));
  try{localStorage.setItem('standards.view',v)}catch(e){}
}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>view(b.dataset.view));

// ── list ──
function typeRow(p){
  const tr=el('tr','pick'+(CUR&&CUR.id===p.id?' sel':''));
  const c=cell(tr,'Type');c.append(el('b','',p.title||p.id),el('span','mut',' '+p.id));
  if(p.description){const d=el('div','small mut clamp',p.description);d.title=p.description;c.append(d)}
  cell(tr,'Policies',(p.policies||[]).join(', ')||'-');
  cell(tr,'Projects',(p.projects||[]).join(', ')||'-');
  const s=cell(tr,'State');
  if(p.error)s.append(el('span','tag bad','invalid'),el('div','small',p.error));
  else if(p.pending)s.append(el('span','tag pend',p.new?'new, unmerged':'unmerged edit'),el('div','small mut','on profile/'+p.id));
  else s.append(el('span','tag allow','ok'));
  tr.onclick=()=>{open(p.id);view('edit')};
  return tr;
}
async function load(){
  const res=await api('GET','/list');
  if(res.status!==200||!res.j.ok){$('status').textContent=err(res);return}
  META=res.j;const C=META.choices;
  $('trow').replaceChildren(...META.profiles.map(typeRow));
  opts($('epick'),META.profiles.map(p=>p.id),true);
  opts($('f-model'),['',...(C.models||[])]);$('f-model').options[0].textContent='default';
  const bad=META.profiles.filter(p=>p.error).length;
  $('status').textContent=META.profiles.length+' types · '+(C.policies||[]).length+' policies to pick from'+(bad?' · '+bad+' invalid':'');
}

// ── editor: the form model and YAML ──
const Y=s=>JSON.stringify(String(s));   // a JSON string is a YAML double-quoted scalar
const YV=v=>JSON.stringify(v);          // and any JSON value is YAML flow style
function toYaml(m){
  const L=['id: '+m.id,'title: '+Y(m.title),'description: '+Y(m.description)];
  if(m.ask.length||m.skip.length){L.push('interview:');
    if(m.ask.length){L.push('  ask:');m.ask.forEach(q=>{L.push('    - id: '+Y(q.id),'      question: '+Y(q.question));
      if(q.kind)L.push('      kind: '+q.kind);if(q.options.length)L.push('      options: ['+q.options.map(Y).join(', ')+']')})}
    if(m.skip.length)L.push('  skip: ['+m.skip.map(Y).join(', ')+']')}
  const st=Object.entries(m.stack);
  if(st.length){L.push('stack:');st.forEach(([k,v])=>L.push('  '+Y(k)+': '+YV(v)))}
  if(m.policies.length)L.push('policies: ['+m.policies.map(Y).join(', ')+']');
  if(m.agents.length){L.push('agents:');m.agents.forEach(a=>L.push('  - {role: '+Y(a.role)+(a.lane?', lane: '+Y(a.lane):'')+'}'))}
  const g=['pre_merge','milestone'].filter(k=>m.gates[k].length);
  if(g.length){L.push('gates:');g.forEach(k=>L.push('  '+k+': ['+m.gates[k].map(Y).join(', ')+']'))}
  if(m.files.length)L.push('files: ['+m.files.map(Y).join(', ')+']');
  const ap=Object.entries(m.autopilot).filter(([k,v])=>v!=null&&v!==''&&!(Array.isArray(v)&&!v.length));
  if(ap.length){L.push('autopilot:');ap.forEach(([k,v])=>L.push('  '+k+': '+YV(v)))}
  return L.join('\n')+'\n';
}
function norm(raw){
  raw=raw||{};const list=x=>x==null?[]:(Array.isArray(x)?x.map(String):[String(x)]);
  const iv=raw.interview||{},g=raw.gates||{},ap=raw.autopilot||{};
  const a={};['cap','model','max_budget_usd'].forEach(k=>{if(ap[k]!=null)a[k]=ap[k]});if(ap.lanes!=null)a.lanes=list(ap.lanes);
  return {id:raw.id||'',title:raw.title||'',description:raw.description||'',
    ask:(iv.ask||[]).map(q=>({id:q.id||'',question:q.question||'',kind:q.kind||'',options:list(q.options)})),
    skip:list(iv.skip),stack:raw.stack||{},policies:list(raw.policies),
    agents:(raw.agents||[]).map(x=>({role:x.role||'',lane:x.lane||''})),
    gates:{pre_merge:list(g.pre_merge),milestone:list(g.milestone)},files:list(raw.files),autopilot:a};
}
function grantText(g){
  const out=Object.entries(g||{}).map(([k,v])=>k.replace('_',' ')+': '+v.join(', '));
  return out.length?out.join(' · '):'no grants: rules or settings only';
}
function drawPols(){
  const C=META.choices.policies||[],m=CUR.model,q=$('pflt').value.trim().toLowerCase(),box=$('f-pols');
  const known=Object.fromEntries(C.map(p=>[p.id,p]));
  const ids=uniq([...m.policies,...C.map(p=>p.id)]);   // the profile's order first, then the rest
  box.replaceChildren();
  ids.forEach(id=>{
    const p=known[id]||{id,description:'not a pickable permission policy',grants:{}};
    const on=m.policies.includes(id),gt=grantText(p.grants);
    if(q&&![id,p.description,gt].join(' ').toLowerCase().includes(q))return;
    const l=el('label','pol'+(on?' on':'')),x=el('input');x.type='checkbox';x.checked=on;x.disabled=REMOTE;
    x.setAttribute('aria-label','policy '+id);
    x.onchange=()=>{m.policies=x.checked?uniq([...m.policies,id]):m.policies.filter(y=>y!==id);drawPols()};
    const t=el('div');t.append(el('b','',id),el('div','small',gt));
    if(p.description){const d=el('div','small mut clamp',p.description);d.title=p.description;t.append(d)}
    l.append(x,t);box.append(l)});
  if(!box.children.length)box.append(el('p','small mut','No policy matches. A new grant needs a new policy.'));
}
function askBox(q,i){
  const box=el('div','box'),kinds=['',...(META.choices.question_kinds||[])];
  const r1=el('div','row');
  const il=el('label','','id'),ii=el('input');ii.type='text';ii.value=q.id;ii.oninput=()=>q.id=ii.value.trim();il.append(ii);
  const kl=el('label','','kind'),ks=el('select');opts(ks,kinds);ks.options[0].textContent='text (default)';ks.value=q.kind;
  ks.onchange=()=>{q.kind=ks.value;ol.hidden=!['single','multi'].includes(q.kind)&&!q.options.length};kl.append(ks);
  const del=el('button','no write','Remove');del.onclick=()=>{readForm();CUR.model.ask.splice(i,1);drawForm()};
  r1.append(il,kl,del);
  const r2=el('div','row'),ql=el('label','grow','question'),qi=el('input');qi.type='text';qi.value=q.question;qi.oninput=()=>q.question=qi.value;ql.append(qi);r2.append(ql);
  const ol=el('label','grow','options, one per line'),ot=el('textarea');ot.rows=Math.min(6,Math.max(2,q.options.length));
  ot.value=q.options.join('\n');ot.oninput=()=>q.options=lines(ot.value);ol.append(ot);
  ol.hidden=!['single','multi'].includes(q.kind)&&!q.options.length;
  const r3=el('div','row');r3.append(ol);
  box.append(r1,r2,r3);return box;
}
function agentRow(a,i){
  const C=META.choices,r=el('div','row');
  const rl=el('label','grow','role'),rs=el('select');opts(rs,uniq([a.role,...(C.roles||[])]));rs.value=a.role;rs.onchange=()=>a.role=rs.value;rl.append(rs);
  const ll=el('label','','lane'),ls=el('select');opts(ls,['',...uniq([a.lane,...(C.lanes||[])])]);ls.options[0].textContent='any';ls.value=a.lane;ls.onchange=()=>a.lane=ls.value;ll.append(ls);
  const del=el('button','no write','Remove');del.onclick=()=>{readForm();CUR.model.agents.splice(i,1);drawForm()};
  r.append(rl,ll,del);return r;
}
function stackText(st){return Object.entries(st||{}).map(([k,v])=>k+': '+(typeof v==='string'?v:JSON.stringify(v))).join('\n')}
function parseStack(t){
  const out={};for(const s of lines(t)){const i=s.indexOf(':');
    if(i<1)throw new Error('stack line needs "key: value": '+s);const k=s.slice(0,i).trim(),v=s.slice(i+1).trim();
    try{out[k]=JSON.parse(v)}catch(e){out[k]=v}}
  return out;
}
function drawForm(){
  const m=CUR.model,C=META.choices;
  $('f-id').value=m.id;$('f-title').value=m.title;$('f-desc').value=m.description;
  drawPols();
  $('f-ask').replaceChildren(...m.ask.map(askBox));if(!m.ask.length)$('f-ask').append(el('p','small mut','No questions: only the generic interview.'));
  $('f-skip').value=m.skip.join(', ');
  $('f-agents').replaceChildren(...m.agents.map(agentRow));if(!m.agents.length)$('f-agents').append(el('p','small mut','No agents named.'));
  $('f-pre').value=m.gates.pre_merge.join('\n');$('f-mile').value=m.gates.milestone.join('\n');
  $('f-stack').value=stackText(m.stack);$('f-files').value=m.files.join('\n');
  $('f-cap').value=m.autopilot.cap!=null?m.autopilot.cap:'';$('f-model').value=m.autopilot.model||'';
  $('f-budget').value=m.autopilot.max_budget_usd!=null?m.autopilot.max_budget_usd:'';
  const lanes=$('f-lanes');lanes.replaceChildren(el('span','small','autopilot lanes:'));
  uniq([...(m.autopilot.lanes||[]),...(C.lanes||[])]).forEach(n=>{const l=el('label','chk'),x=el('input');x.type='checkbox';x.value=n;
    x.checked=(m.autopilot.lanes||[]).includes(n);x.className='lane';l.append(x,document.createTextNode(n));lanes.append(l)});
  lockEditor(REMOTE);
}
function num(v){if(v==='')return undefined;const n=Number(v);if(!isFinite(n))throw new Error('not a number: '+v);return n}
function readForm(){
  const m=CUR.model;m.title=$('f-title').value.trim();m.description=$('f-desc').value.trim();
  m.skip=commas($('f-skip').value);
  m.gates={pre_merge:lines($('f-pre').value),milestone:lines($('f-mile').value)};
  m.stack=parseStack($('f-stack').value);m.files=lines($('f-files').value);
  const a={},cap=num($('f-cap').value),b=num($('f-budget').value);
  if(cap!==undefined)a.cap=cap;if($('f-model').value)a.model=$('f-model').value;if(b!==undefined)a.max_budget_usd=b;
  const ln=[...document.querySelectorAll('#f-lanes input.lane')].filter(x=>x.checked).map(x=>x.value);
  if(ln.length)a.lanes=ln;
  m.autopilot=a;return m;
}
$('f-addq').onclick=()=>{readForm();CUR.model.ask.push({id:'',question:'',kind:'',options:[]});drawForm()};
$('f-adda').onclick=()=>{readForm();CUR.model.agents.push({role:(META.choices.roles||[''])[0]||'',lane:'dev'});drawForm()};
$('pflt').oninput=()=>drawPols();
function text(){if(MODE==='yaml')return $('yaml').value;return toYaml(readForm())}
async function mode(m){
  if(m===MODE)return;
  if(m==='yaml'){try{$('yaml').value=toYaml(readForm())}catch(e){say($('emsg'),e.message,'err');return}}
  else{if(REMOTE){say($('emsg'),'The form view needs a check, which the phone cannot send.','err');return}
    const res=await api('POST','/validate',{id:CUR.id,yaml:$('yaml').value});
    if(res.status!==200||!res.j.ok){say($('emsg'),'Fix the YAML before going back to the form: '+err(res),'err');return}
    CUR.model=norm(res.j.profile);drawForm()}
  setMode(m);say($('emsg'),'');
}
function setMode(m){MODE=m;$('f-form').hidden=m!=='form';$('f-yaml').hidden=m!=='yaml';
  $('m-form').classList.toggle('on',m==='form');$('m-yaml').classList.toggle('on',m==='yaml')}
$('m-form').onclick=()=>mode('form');$('m-yaml').onclick=()=>mode('yaml');
function lockEditor(ro){
  document.querySelectorAll('#v-edit input,#v-edit textarea,#v-edit select:not(#epick)').forEach(x=>{
    if(x.id==='f-id'||x.id==='pflt')return;x.disabled=ro});
}
async function open(pid){
  $('epick').value=pid;say($('emsg'),'loading '+pid+'...');$('ediff').hidden=true;
  const res=await api('GET','/get?id='+encodeURIComponent(pid));
  if(res.status!==200||!res.j.ok){say($('emsg'),err(res),'err');return}
  const j=res.j,from=j.pending?j.pending.yaml:j.yaml;
  CUR={id:pid,isnew:!!j.new,source:from,projects:j.projects||[]};
  const n=$('enote');n.replaceChildren();
  if(j.pending){const d=el('div','notice');d.append(el('span','','Showing the unmerged version on branch '),el('b','',j.pending.branch),
    el('span','',' ('+j.pending.ahead+' commit(s) ahead of master). Merging it is a review step.'));n.append(d)}
  if(CUR.projects.length)n.append(el('div','notice small','Registered projects of this type: '+CUR.projects.join(', ')+'. An edit reaches them only when they are re-tagged or recompiled.'));
  $('yaml').value=from;
  CUR.model=norm(j.pending?null:j.profile);
  let ok=!j.error&&!j.pending;
  if(j.pending&&!REMOTE){const v=await api('POST','/validate',{id:pid,yaml:from});ok=v.status===200&&v.j.ok;if(ok)CUR.model=norm(v.j.profile);else j.error=err(v)}
  if(ok){drawForm();setMode('form');say($('emsg'),'')}
  else{CUR.model=norm(j.profile);drawForm();setMode('yaml');say($('emsg'),j.error?'The saved file does not validate: '+j.error:'Showing the YAML: the phone cannot check the unmerged version.',j.error?'err':'')}
  $('trow').querySelectorAll('tr').forEach((tr,i)=>tr.classList.toggle('sel',META.profiles[i]&&META.profiles[i].id===pid));
  DRY=null;$('dhead').replaceChildren(el('p','small mut','Press Dry run in the editor.'));$('dchg').replaceChildren();$('drow').replaceChildren();$('dentry').textContent='';
}
$('epick').onchange=()=>open($('epick').value);
$('newbtn').onclick=()=>{
  const id=$('newid').value.trim();
  if(!/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.test(id)){alert('A type id is lower-case words joined by dashes.');return}
  if(META.profiles.some(p=>p.id===id)){open(id);view('edit');return}
  CUR={id,isnew:true,source:'',projects:[],model:norm({id,title:'',description:''})};
  const o=el('option','',id+' (new)');o.value=id;$('epick').append(o);$('epick').value=id;
  $('enote').replaceChildren(el('div','notice','A new type, profiles/'+id+'.yaml. Check and Dry run work on it; Save puts it on branch profile/'+id+'.'));
  setMode('form');drawForm();say($('emsg'),'');$('ediff').hidden=true;view('edit');
};
$('check').onclick=async()=>{
  let t;try{t=text()}catch(e){say($('emsg'),e.message,'err');return}
  const res=await api('POST','/validate',{id:CUR.id,yaml:t,keep:MODE==='form'});
  if(res.status!==200||!res.j.ok){say($('emsg'),err(res),'err');$('ediff').hidden=true;return}
  say($('emsg'),'Valid. Nothing was saved.'+(res.j.diff?' The diff against master:':' It matches master.'),'good');
  const d=$('ediff');d.textContent=res.j.diff;d.hidden=!res.j.diff;
};
$('save').onclick=async()=>{
  let t;try{t=text()}catch(e){say($('emsg'),e.message,'err');return}
  const b=$('save');b.disabled=true;
  const res=await api('POST','/save',{id:CUR.id,yaml:t,keep:MODE==='form'});b.disabled=false;
  if(res.status!==200||!res.j.ok){say($('emsg'),'Not saved: '+err(res),'err');$('ediff').hidden=true;return}
  const j=res.j,was=MODE;
  if(!j.unchanged){await load();await open(CUR.id);if(was==='yaml')setMode('yaml')}
  say($('emsg'),j.unchanged?'Nothing to save: it matches '+(j.branch||'master')+'.'
    :'Saved on branch '+j.branch+' as '+j.commit+'. Master is unchanged; merging the branch is a review step.'+(j.diff?' The diff against master:':''),'good');
  const d=$('ediff');d.textContent=j.diff;d.hidden=!j.diff;
};

// ── dry run ──
const KINDS=['ask','stack','agent','gate','file','autopilot','policy','mcp','skill','tool','setting'];
async function dryRun(){
  let res;
  if(REMOTE)res=await api('GET','/dry-run?id='+encodeURIComponent(CUR.id));
  else{let t;try{t=text()}catch(e){say($('emsg'),e.message,'err');return}
    res=await api('POST','/dry-run',{id:CUR.id,yaml:t})}
  if(res.status!==200||!res.j.ok){say($('emsg'),err(res),'err');return}
  DRY=res.j;drawDry();view('dry');say($('emsg'),'');
}
$('dbtn').onclick=dryRun;
function drawDry(){
  const j=DRY,h=$('dhead'),c=$('dchg');
  h.replaceChildren(el('div','big','A new '+j.id+' project'+(j.edited?', with this edit':', as on master')),
    el('div','small mut',j.facts.length+' facts · '+((j.gets.layers.guardrail||[]).length)+' guardrails · type layer: '+((j.gets.layers.type||[]).join(', ')||'-')));
  c.replaceChildren();
  if(j.edited){
    if(j.new)c.append(el('div','notice','A new type: every line below is new.'));
    else if(!j.changes)c.append(el('div','notice','The edit changes nothing a new project gets.'));
    else{c.append(el('h2','','WHAT THE EDIT CHANGES'),el('pre','diff',j.changes.split('\n').filter(l=>!/^(---|\+\+\+|@@)/.test(l)).join('\n')))}
    if((j.projects||[]).length)c.append(el('div','small mut','Not yet for '+j.projects.join(', ')+': registered projects keep their configuration until re-tagged or recompiled.'))}
  const kinds=KINDS.filter(k=>j.facts.some(f=>f.startsWith(k+' ')));
  opts($('dkind'),['',...kinds],true);$('dkind').options[0].textContent='all';
  $('dentry').textContent=j.gets.registry_entry;drawFacts();
}
function drawFacts(){
  if(!DRY)return;
  const q=$('dflt').value.trim().toLowerCase(),k=$('dkind').value;
  const rows=DRY.facts.filter(f=>(!k||f.startsWith(k+' '))&&(!q||f.toLowerCase().includes(q)));
  $('drow').replaceChildren(...rows.map(f=>{const i=f.indexOf(' '),tr=el('tr');
    const w=cell(tr,'What'),kind=f.slice(0,i),rest=f.slice(i+1);
    const m=rest.match(/, (allow|ask|deny) by /);
    w.append(el('span','tag '+(m?m[1]:''),kind));cell(tr,'Detail',rest);return tr}));
  if(!rows.length)$('drow').append(el('tr')).append(el('td','mut','Nothing matches.'));
}
$('dflt').oninput=drawFacts;$('dkind').onchange=drawFacts;

(async()=>{
  $('ro').hidden=!REMOTE;
  await load();
  const pick=new URLSearchParams(location.search).get('id');
  if(META.profiles.length)await open(pick&&META.profiles.some(p=>p.id===pick)?pick:META.default||META.profiles[0].id);
  let v='list';try{v=localStorage.getItem('standards.view')||'list'}catch(e){}
  if(pick)v='edit';if(v==='dry')v='edit';
  view(VIEWS.includes(v)?v:'list');
})();
</script></body></html>"""
