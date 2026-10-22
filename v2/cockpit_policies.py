"""#1397 · the cockpit's Policies page (PRD project-types-and-inline-asks §6.5): the policy list,
an editor with a form and a YAML view, attach and detach to a type or a project, and a project's
effective access with the statement that decides each item.

The page only calls the engine's /api/policies routes (#1396), which proxy to the dispatcher with
its token, so no token reaches the browser. Saves and attaches land on a branch (policy/<id>,
policy/attach-<target>-<name>, policy/tag-<project>) for review; nothing here merges.

#1398 adds the simulator (project + action -> effect and the deciding statement), the tag flow
(before/after diff, proposed changes, save with every add held, accept one change at a time) and
the guardrail save: a dialog shows the diff and the operator types the id. Served from a proxy
(the phone on the tailnet), the page is read-only, as the tailnet allows no policy POST.
"""

PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>NOMAD · Policies</title>
<style>
*{box-sizing:border-box}
body{background:#05060a;color:#ffb000;font-family:ui-monospace,'Share Tech Mono',monospace;margin:0 auto;
  padding:14px 16px 40px;font-size:14px;line-height:1.45;max-width:1100px;overflow-wrap:anywhere}
a{color:#56b6ff}
.top{display:flex;align-items:center;gap:12px;margin:0 0 12px;font-size:12px;letter-spacing:.12em;flex-wrap:wrap}
.top a{display:inline-flex;align-items:center;min-height:44px;color:#9aa;text-decoration:none}
h1{font-size:15px;letter-spacing:.2em;margin:0;font-weight:400;flex:1}
h2{font-size:12px;letter-spacing:.14em;color:#56b6ff;font-weight:400;margin:18px 0 8px}
select,button,input[type=text],textarea{font:inherit;color:inherit;background:#0a0b12;border:1px solid #2a2a3a;
  border-radius:7px;padding:6px 10px;min-height:40px;max-width:100%}
textarea{width:100%;resize:vertical}
#yaml{min-height:360px;white-space:pre;overflow-wrap:normal;overflow-x:auto}
button{cursor:pointer}button:disabled{opacity:.4;cursor:default}
button.on{border-color:#ffb000;background:#1a1405}
.ok{border-color:#33dd88;color:#33dd88}.no{border-color:#e3554f;color:#e3554f}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
table{width:100%;border-collapse:collapse}
th{color:#56b6ff;font-size:11px;letter-spacing:.1em;text-align:left;font-weight:400;padding:4px 8px}
td{border-top:1px solid #1b1b27;padding:8px;vertical-align:top}
tr.pick{cursor:pointer}tr.pick:hover td{background:#0d0e17}tr.sel td{background:#141320}
.tag{padding:2px 9px;border-radius:9px;font-size:12px;color:#000;font-weight:700;background:#445;white-space:nowrap}
.tag.allow{background:#33dd88}.tag.deny{background:#e3554f}.tag.ask{background:#ffb000}
.tag.guardrail{background:#b388ff}.tag.permission{background:#56b6ff}.tag.none{background:#667;color:#ddd}
.mut{opacity:.65}.small{font-size:12px}
.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
.row label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:#56b6ff;letter-spacing:.06em}
.row label>*{color:#ffb000;font-size:14px;letter-spacing:0}
.grow{flex:1;min-width:200px}.grow>*{width:100%}
.stmt{border:1px solid #1b1b27;border-radius:9px;padding:8px 10px;margin:8px 0}
.msg{margin:8px 0;font-size:13px;white-space:pre-wrap}.msg.err{color:#e3554f}.msg.good{color:#33dd88}
pre.diff{background:#0a0b12;border:1px solid #1b1b27;border-radius:7px;padding:8px;overflow-x:auto;
  font-size:12px;white-space:pre;overflow-wrap:normal;max-height:360px;color:#ccc}
.notice{border:1px solid #2a2a3a;border-radius:9px;padding:8px 10px;margin:8px 0;font-size:13px}
#status{opacity:.7;font-size:12px;margin:0 0 10px}
dialog{background:#05060a;color:#ffb000;border:1px solid #b388ff;border-radius:11px;padding:14px;
  width:min(760px,calc(100vw - 32px));max-height:calc(100vh - 40px);overflow:auto}
dialog::backdrop{background:rgba(0,0,0,.7)}
.big{font-size:16px;margin:6px 0}
tr.pick td[data-h=Item]{text-decoration:underline dotted}
body.remote .write{display:none !important}
@media(max-width:640px){thead{display:none}tr,td{display:block}td{border:0;padding:3px 0}
  tr{border-top:1px solid #1b1b27;padding:9px 0}td::before{content:attr(data-h) ": ";color:#56b6ff;font-size:11px}
  .grow{min-width:100%}#yaml{min-height:260px}
  #xrow tr{padding:6px 0}#xrow td{display:inline;padding:0 6px 0 0}#xrow td::before{content:none}
  #xrow td[data-h="Decided by"],#xrow td[data-h="Why"]{font-size:12px;opacity:.75}
  #xrow td[data-h="Decided by"]::before{content:"\A";white-space:pre}}
</style></head><body>
<div class=top><a href="/cockpit">&larr; COCKPIT</a><h1>POLICIES</h1></div>
<div class=tabs role=tablist>
<button id=t-list class=on data-view=list>Policies</button>
<button id=t-edit data-view=edit>Editor</button>
<button id=t-eff data-view=eff>Effective access</button>
<button id=t-sim data-view=sim>Simulator</button>
<button id=t-tag data-view=tag>Tag a project</button>
</div>
<p id=status>loading...</p>
<p id=ro class="notice mut" hidden>Read-only from the phone: the tailnet allows no policy change. Edit and attach from the host.</p>

<section id=v-list>
<table><thead><tr><th>Policy</th><th>Role</th><th>Statements</th><th>Settings</th><th>Attached to</th></tr></thead>
<tbody id=prow></tbody></table>
<div class="row write"><input type=text id=newid placeholder="new policy id (a-z, 0-9, -)" aria-label="new policy id">
<button id=newbtn>New permission policy</button></div>
</section>

<section id=v-edit hidden>
<div class=row><label class=grow>Policy<select id=epick aria-label="policy to edit"></select></label></div>
<div id=enote></div>
<div class=tabs><button id=m-form class=on>Form</button><button id=m-yaml>YAML</button></div>
<div id=f-form>
<div class=row>
<label class=grow>id<input type=text id=f-id readonly></label>
<label>role<select id=f-role></select></label></div>
<div class=row><label class=grow>description<textarea id=f-desc rows=3></textarea></label></div>
<h2>STATEMENTS</h2><div id=f-stmts></div>
<div class="row write"><button id=f-add>Add statement</button></div>
<h2>SETTINGS</h2>
<p class="small mut">One per line, <code>key: value</code>. Keys: <span id=f-skeys></span>. A value is JSON or plain text.</p>
<textarea id=f-settings rows=3 aria-label=settings></textarea>
<div id=f-lockrow><h2>LOCKS</h2><input type=text id=f-locks placeholder="setting keys, comma separated" aria-label=locks></div>
<p class="small mut">The form writes the file fresh: comments in the YAML do not survive it. Use the YAML view to keep them.</p>
</div>
<div id=f-yaml hidden><textarea id=yaml spellcheck=false aria-label="policy YAML"></textarea></div>
<div class="row write"><button id=check>Check</button><button id=save class=ok>Save to branch</button></div>
<div id=emsg class=msg></div><pre id=ediff class=diff hidden></pre>
<div id=attachbox>
<h2>ATTACHED TO</h2><div id=alist class=small></div>
<div class="row write"><select id=atarget aria-label="attach target"><option value=type>type</option><option value=project>project</option></select>
<select id=aname aria-label="type or project"></select><button id=abtn>Attach</button></div>
<div id=amsg class=msg></div><pre id=adiff class=diff hidden></pre>
</div>
</section>

<section id=v-eff hidden>
<div class=row><label class=grow>Project<select id=xpick aria-label=project></select></label>
<label class=grow>Filter<input type=text id=xflt placeholder="name, effect, policy..." aria-label=filter></label>
<label>Show<select id=xkind aria-label="show which"><option value=stmt>decided by a statement</option><option value="">all, defaults too</option><option>allow</option><option>ask</option><option>deny</option></select></label></div>
<div id=xhead class=small></div>
<table><thead><tr><th>Item</th><th>Kind</th><th>Effect</th><th>Decided by</th><th>Why</th></tr></thead>
<tbody id=xrow></tbody></table>
<p class="small mut">Pick a row to try it in the simulator with a lane, tier or task type.</p>
<h2>SETTINGS</h2>
<table><thead><tr><th>Setting</th><th>Value</th><th>From</th><th>Note</th></tr></thead><tbody id=xset></tbody></table>
</section>
<section id=v-sim hidden>
<p class="small mut">Would this project be allowed this, and which statement decides it. Deny beats allow; nothing named is a default deny.</p>
<div class=row><label class=grow>Project<select id=spick aria-label=project></select></label>
<label>Kind<select id=skind aria-label=kind></select></label>
<label class=grow>Name<input type=text id=sname placeholder="figma, Bash(git push *), graphify..." aria-label=name></label></div>
<div class=row id=swhen></div>
<div class="row write"><button id=sbtn class=ok>Simulate</button></div>
<div id=sres></div>
<h2>THIS SESSION</h2>
<table><thead><tr><th>Project</th><th>Action</th><th>Effect</th><th>Decided by</th></tr></thead><tbody id=shist></tbody></table>
</section>

<section id=v-tag hidden>
<p class="small mut">Give a project a type without changing what it may do: every server the type would add is held in legacy-&lt;project&gt;, so the diff stays empty. Accept the held changes one at a time afterwards.</p>
<div class=row><label class=grow>Project<select id=gpick aria-label=project></select></label>
<label class=grow>Type<select id=gtype aria-label=type></select></label></div>
<div class="row write"><button id=gdry>Dry run</button><button id=gsave class=ok disabled>Save tag to branch</button></div>
<div id=gres></div><div id=gmsg class=msg></div><pre id=gdiff class=diff hidden></pre>
<div id=gheld></div>
</section>

<dialog id=gdlg aria-labelledby=gdlgh>
<h2 id=gdlgh>SAVE A GUARDRAIL</h2>
<p class=small>A guardrail applies to every project and no lower layer can undo it. This saves to branch <b id=gdlgbr></b>; merging it is yours, as a red-tier change. This is what changes against master:</p>
<pre id=gdlgdiff class=diff></pre>
<label class=small>Type the policy id, <b id=gdlgid></b>, to confirm<br><input type=text id=gdlgin autocomplete=off spellcheck=false aria-label="type the policy id"></label>
<div class=row><button id=gdlgok class=no disabled>Save guardrail</button><button id=gdlgno>Cancel</button></div>
<div id=gdlgmsg class=msg></div>
</dialog>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
const REMOTE=document.body.classList.contains('remote');
let META={policies:[],types:[],projects:[],schema:{}},CUR=null,MODE='form',EFF=null;
const VIEWS=['list','edit','eff','sim','tag'];
function say(box,text,kind){box.textContent=text;box.className='msg'+(kind?' '+kind:'')}
async function api(method,path,body){
  let r;try{r=await fetch('/api/policies'+path,method==='GET'?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
  catch(e){return {status:0,j:{ok:false,error:'engine unreachable'}}}
  let j={};try{j=await r.json()}catch(e){}
  return {status:r.status,j};
}
const err=res=>(res.j&&res.j.error)||('error '+res.status);
function cell(tr,h,x){const t=el('td','',x);t.dataset.h=h;tr.append(t);return t}
function opts(sel,vals,keep){const v=keep?sel.value:'';sel.replaceChildren(...vals.map(x=>{const o=el('option','',x);o.value=x;return o}));if(keep&&vals.includes(v))sel.value=v}
function view(v){
  VIEWS.forEach(n=>{$('v-'+n).hidden=n!==v});
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('on',b.dataset.view===v));
  try{localStorage.setItem('policies.view',v)}catch(e){}
  if(v==='eff'&&!EFF)effective();
}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>view(b.dataset.view));

// ── list ──
function attachedText(a){const t=(a.types||[]).map(x=>'type '+x),p=(a.projects||[]).map(x=>'project '+x);return t.concat(p).join(', ')}
function listRow(p){
  const tr=el('tr','pick'+(CUR&&CUR.id===p.id?' sel':''));
  const c=cell(tr,'Policy');c.append(el('b','',p.id));if(p.description){const d=el('div','small mut clamp',p.description);d.title=p.description;c.append(d)}
  const r=cell(tr,'Role');r.append(el('span','tag '+(p.guardrail?'guardrail':'permission'),p.guardrail?'guardrail':p.role));
  cell(tr,'Statements',String(p.statements));
  cell(tr,'Settings',(p.settings||[]).join(', ')||'-');
  cell(tr,'Attached to',p.guardrail?'every project':(attachedText(p.attached||{})||'-'));
  tr.onclick=()=>{open(p.id);view('edit')};
  return tr;
}
async function load(){
  const res=await api('GET','');
  if(res.status!==200||!res.j.ok){$('status').textContent=err(res);return}
  META=res.j;
  $('prow').replaceChildren(...META.policies.map(listRow));
  opts($('epick'),META.policies.map(p=>p.id),true);
  opts($('xpick'),META.projects,true);opts($('spick'),META.projects,true);opts($('gpick'),META.projects,true);
  opts($('gtype'),META.types,true);opts($('skind'),META.schema.kinds||[],true);simWhen();
  opts($('f-role'),META.schema.roles||['permission']);
  $('f-skeys').textContent=(META.schema.setting_keys||[]).join(', ');
  $('status').textContent=META.policies.length+' policies · '+META.types.length+' types · '+META.projects.length+' projects';
}

// ── editor: the form model and YAML ──
const Y=s=>JSON.stringify(String(s));   // a JSON string is a YAML double-quoted scalar
const YV=v=>JSON.stringify(v);          // and any JSON value is YAML flow style
function toYaml(p){
  const L=['id: '+p.id,'role: '+p.role];
  if(p.description)L.push('description: '+Y(p.description));
  if(p.statements.length){L.push('statements:');p.statements.forEach(s=>{
    L.push('  - sid: '+Y(s.sid||''),'    effect: '+s.effect,'    kind: '+s.kind,'    match: ['+s.match.map(Y).join(', ')+']');
    const w=Object.entries(s.when||{}).filter(([k,v])=>v.length);
    if(w.length){L.push('    when:');w.forEach(([k,v])=>L.push('      '+k+': ['+v.map(Y).join(', ')+']'))}
  })}else L.push('statements: []');
  const st=Object.entries(p.settings||{});
  if(st.length){L.push('settings:');st.forEach(([k,v])=>L.push('  '+k+': '+YV(v)))}
  if((p.locks||[]).length)L.push('locks: ['+p.locks.map(Y).join(', ')+']');
  return L.join('\n')+'\n';
}
function norm(raw){
  raw=raw||{};const list=x=>x==null?[]:(Array.isArray(x)?x.map(String):[String(x)]);
  return {id:raw.id||'',role:raw.role||'permission',description:raw.description||'',
    statements:(raw.statements||[]).map(s=>({sid:s.sid||'',effect:s.effect||'deny',kind:s.kind||'mcp',match:list(s.match),
      when:Object.fromEntries(Object.entries(s.when||{}).map(([k,v])=>[k,list(v)]))})),
    settings:raw.settings||{},locks:list(raw.locks)};
}
function stmtBox(s,i){
  const S=META.schema,box=el('div','stmt');
  const sel=(label,vals,val,set)=>{const l=el('label','',label),x=el('select');opts(x,vals);x.value=val;x.onchange=()=>set(x.value);l.append(x);return l};
  const r1=el('div','row');
  const sl=el('label','grow','sid'),si=el('input');si.type='text';si.value=s.sid;si.oninput=()=>s.sid=si.value;sl.append(si);
  r1.append(sl,sel('effect',S.effects||[],s.effect,v=>s.effect=v),sel('kind',S.kinds||[],s.kind,v=>s.kind=v));
  const del=el('button','no write','Remove');del.onclick=()=>{CUR.model.statements.splice(i,1);drawForm()};r1.append(del);
  const ml=el('label','grow','match, one per line'),mt=el('textarea');mt.rows=Math.min(8,Math.max(2,s.match.length));
  mt.value=s.match.join('\n');mt.oninput=()=>s.match=mt.value.split('\n').map(x=>x.trim()).filter(Boolean);ml.append(mt);
  const r2=el('div','row');r2.append(ml);
  const r3=el('div','row');
  (S.when_keys||[]).forEach(k=>{const l=el('label','','when '+k),x=el('input');x.type='text';x.placeholder='any';
    x.value=(s.when[k]||[]).join(', ');x.oninput=()=>s.when[k]=x.value.split(',').map(y=>y.trim()).filter(Boolean);l.append(x);r3.append(l)});
  box.append(r1,r2,r3);return box;
}
function settingsText(st){return Object.entries(st||{}).map(([k,v])=>k+': '+JSON.stringify(v)).join('\n')}
function parseSettings(t){
  const out={};for(const line of t.split('\n')){const s=line.trim();if(!s)continue;const i=s.indexOf(':');
    if(i<1)throw new Error('settings line needs "key: value": '+s);const k=s.slice(0,i).trim(),v=s.slice(i+1).trim();
    try{out[k]=JSON.parse(v)}catch(e){out[k]=v}}
  return out;
}
function drawForm(){
  const m=CUR.model;$('f-id').value=m.id;$('f-role').value=m.role;$('f-desc').value=m.description;
  $('f-stmts').replaceChildren(...m.statements.map(stmtBox));
  if(!m.statements.length)$('f-stmts').append(el('p','small mut','No statements.'));
  $('f-settings').value=settingsText(m.settings);$('f-locks').value=m.locks.join(', ');
  $('f-lockrow').hidden=m.role!=='guardrail';
}
function readForm(){
  const m=CUR.model;m.role=$('f-role').value;m.description=$('f-desc').value.trim();
  m.settings=parseSettings($('f-settings').value);
  m.locks=$('f-locks').value.split(',').map(x=>x.trim()).filter(Boolean);
  return m;
}
$('f-add').onclick=()=>{readForm();CUR.model.statements.push({sid:'',effect:'deny',kind:'mcp',match:[],when:{}});drawForm()};
$('f-role').onchange=()=>{$('f-lockrow').hidden=$('f-role').value!=='guardrail'};
function text(){if(MODE==='yaml')return $('yaml').value;return toYaml(readForm())}
async function mode(m){
  if(m===MODE)return;
  if(m==='yaml'){try{$('yaml').value=toYaml(readForm())}catch(e){say($('emsg'),e.message,'err');return}}
  else{const res=await api('POST','/validate',{id:CUR.id,yaml:$('yaml').value});
    if(res.status!==200||!res.j.ok){say($('emsg'),'Fix the YAML before going back to the form: '+err(res),'err');return}
    CUR.model=norm(res.j.policy);drawForm()}
  setMode(m);say($('emsg'),'');
}
function setMode(m){MODE=m;$('f-form').hidden=m!=='form';$('f-yaml').hidden=m!=='yaml';
  $('m-form').classList.toggle('on',m==='form');$('m-yaml').classList.toggle('on',m==='yaml')}
$('m-form').onclick=()=>mode('form');$('m-yaml').onclick=()=>mode('yaml');
function lockEditor(ro){
  document.querySelectorAll('#v-edit input,#v-edit textarea,#v-edit select:not(#epick)').forEach(x=>{
    if(x.id==='f-id')return;x.disabled=ro});
  $('save').disabled=ro;$('f-add').disabled=ro;
}
function drawAttached(){
  const box=$('alist'),a=CUR.attached||{};box.replaceChildren();
  const items=(a.types||[]).map(n=>['type',n]).concat((a.projects||[]).map(n=>['project',n]));
  if(!items.length)box.append(el('span','mut','Not attached on master.'));
  items.forEach(([t,n])=>{const r=el('div','row');r.append(el('span','',t+' '+n));
    const b=el('button','no write','Detach');b.onclick=()=>attach(t,n,true,b);r.append(b);box.append(r)});
  const names=()=>opts($('aname'),$('atarget').value==='type'?META.types:META.projects);names();$('atarget').onchange=names;
}
async function open(pid){
  $('epick').value=pid;say($('emsg'),'loading '+pid+'...');$('ediff').hidden=true;$('adiff').hidden=true;say($('amsg'),'');
  const res=await api('GET','/get?id='+encodeURIComponent(pid));
  if(res.status!==200||!res.j.ok){say($('emsg'),err(res),'err');return}
  const j=res.j,from=j.pending?j.pending.yaml:j.yaml;
  CUR={id:pid,guardrail:j.guardrail,attached:j.attached,isnew:false};
  const n=$('enote');n.replaceChildren();
  if(j.pending){const d=el('div','notice');d.append(el('span','','Editing the unmerged version on branch '),el('b','',j.pending.branch),
    el('span','',' ('+j.pending.ahead+' commit(s) ahead of master). A save adds a commit there; merging it is a review step.'));n.append(d)}
  if(j.guardrail)n.append(el('div','notice',REMOTE?'A guardrail: it covers every project, so attach does not apply.'
    :'A guardrail: it covers every project, so attach does not apply. Saving shows the diff and asks you to type the id; it lands on a branch you merge.'));
  $('yaml').value=from;CUR.source=from;  // the file as written, comments and all
  const v=await api('POST','/validate',{id:pid,yaml:from});
  if(v.status===200&&v.j.ok){CUR.model=norm(v.j.policy);drawForm();say($('emsg'),'')}
  else{CUR.model=norm(j.policy);drawForm();setMode('yaml');say($('emsg'),'The saved file does not validate: '+err(v),'err')}
  lockEditor(REMOTE);$('attachbox').hidden=j.guardrail;drawAttached();
  $('save').textContent=j.guardrail?'Save guardrail...':'Save to branch';$('save').className=j.guardrail?'no':'ok';
  $('prow').querySelectorAll('tr').forEach((tr,i)=>tr.classList.toggle('sel',META.policies[i]&&META.policies[i].id===pid));
}
$('epick').onchange=()=>open($('epick').value);
$('newbtn').onclick=()=>{
  const id=$('newid').value.trim();
  if(!/^[a-z0-9][a-z0-9-]{0,63}$/.test(id)){alert('An id is lower-case letters, digits and dashes.');return}
  if(META.policies.some(p=>p.id===id)){open(id);view('edit');return}
  CUR={id,guardrail:false,attached:{},isnew:true,model:norm({id,role:'permission',statements:[]})};
  const o=el('option','',id+' (new)');o.value=id;$('epick').append(o);$('epick').value=id;
  $('enote').replaceChildren(el('div','notice','A new permission policy. It is saved under policies/types/ on branch policy/'+id+'.'));
  setMode('form');drawForm();lockEditor(false);$('save').textContent='Save to branch';$('save').className='ok';$('attachbox').hidden=true;say($('emsg'),'');$('ediff').hidden=true;view('edit');
};
$('check').onclick=async()=>{
  let t;try{t=text()}catch(e){say($('emsg'),e.message,'err');return}
  const res=await api('POST','/validate',{id:CUR.id,yaml:t});
  say($('emsg'),res.status===200&&res.j.ok?'Valid. Nothing was saved.':err(res),res.status===200&&res.j.ok?'good':'err');
};
const comments=t=>t.split('\n').filter(l=>/^\s*#/.test(l)||/\s#\s/.test(l)).length;
$('save').onclick=async()=>{
  let t;try{t=text()}catch(e){say($('emsg'),e.message,'err');return}
  const lost=MODE==='form'&&CUR.source?comments(CUR.source):0;
  if(lost&&!confirm('The form rewrites the file and drops its '+lost+' comment line(s). Save anyway? (The YAML view keeps them.)'))return;
  if(CUR.guardrail){
    const v=await api('POST','/validate',{id:CUR.id,yaml:t});
    if(v.status!==200||!v.j.ok){say($('emsg'),err(v),'err');return}
    return guardDialog(t,v.j.diff);
  }
  $('save').disabled=true;say($('emsg'),'saving...');
  const res=await api('POST','/save',{id:CUR.id,yaml:t});$('save').disabled=false;
  if(res.status!==200||!res.j.ok){say($('emsg'),err(res),'err');return}
  say($('emsg'),'Saved on branch '+res.j.branch+' as '+res.j.commit+' ('+res.j.file+'). Master is unchanged until the branch is merged.','good');
  const d=$('ediff');d.textContent=res.j.diff||'(no change from master)';d.hidden=false;
  if(CUR.isnew){CUR.isnew=false;await load();$('epick').value=CUR.id}
};
function guardDialog(t,diff){
  const dl=$('gdlg');$('gdlgid').textContent=CUR.id;$('gdlgbr').textContent='policy/'+CUR.id;
  $('gdlgdiff').textContent=diff||'(no change from master)';$('gdlgin').value='';$('gdlgok').disabled=true;say($('gdlgmsg'),'');
  $('gdlgin').oninput=()=>{$('gdlgok').disabled=$('gdlgin').value!==CUR.id};
  $('gdlgno').onclick=()=>dl.close();
  $('gdlgok').onclick=async()=>{
    const typed=$('gdlgin').value;if(typed!==CUR.id)return;
    $('gdlgok').disabled=true;say($('gdlgmsg'),'saving...');
    const res=await api('POST','/guardrail-save',{id:CUR.id,yaml:t,confirm:typed});
    if(res.status!==200||!res.j.ok){say($('gdlgmsg'),err(res),'err');$('gdlgok').disabled=false;return}
    dl.close();
    say($('emsg'),'Guardrail saved on branch '+res.j.branch+' as '+res.j.commit+' ('+res.j.file+'). Merging it is a red-tier change: yours.','good');
    const d=$('ediff');d.textContent=res.j.diff||'(no change from master)';d.hidden=false;
  };
  dl.showModal();$('gdlgin').focus();
}
async function attach(target,name,detach,btn){
  if(btn)btn.disabled=true;say($('amsg'),(detach?'detaching':'attaching')+'...');
  const res=await api('POST','/attach',{target,name,policy:CUR.id,detach});if(btn)btn.disabled=false;
  if(res.status!==200||!res.j.ok){say($('amsg'),err(res),'err');return}
  say($('amsg'),(detach?'Detached from ':'Attached to ')+target+' '+name+' on branch '+res.j.branch+' ('+res.j.commit+'). '
    +name+' would have: '+(res.j.policies.join(', ')||'none')+'. Master is unchanged until the branch is merged.','good');
  const d=$('adiff');d.textContent=res.j.diff||'(no change from master)';d.hidden=false;
}
$('abtn').onclick=()=>{if($('aname').value)attach($('atarget').value,$('aname').value,false,$('abtn'))};

// ── effective access ──
function decided(i){return i.policy?(i.layer+' · '+i.policy+(i.sid?' · '+i.sid:'')):(i.layer||'default')}
function drawEff(){
  if(!EFF)return;
  const q=$('xflt').value.trim().toLowerCase(),k=$('xkind').value;
  const rows=EFF.items.filter(i=>(k==='stmt'?i.reason!=='default-deny':(!k||i.effect===k))&&(!q||[i.name,i.kind,i.channel,i.effect,i.policy,i.sid,i.reason,i.layer].join(' ').toLowerCase().includes(q)));
  $('xrow').replaceChildren(...rows.map(i=>{
    const tr=el('tr','pick');cell(tr,'Item',i.name);cell(tr,'Kind',i.kind+(i.channel?' · '+i.channel:''));
    tr.onclick=()=>{$('spick').value=EFF.project;$('skind').value=i.kind;$('sname').value=i.name;
      if(i.channel)$('sw-channel').value=i.channel;view('sim');if(!REMOTE)simulate()};
    cell(tr,'Effect').append(el('span','tag '+(i.effect||'none'),i.effect||'none'));
    cell(tr,'Decided by',decided(i));cell(tr,'Why',i.reason||'');return tr}));
  if(!rows.length)$('xrow').append(el('tr')).append(el('td','mut','Nothing matches.'));
  const c={allow:0,ask:0,deny:0};EFF.items.forEach(i=>{if(i.effect in c)c[i.effect]++});
  $('xhead').textContent='type '+(EFF.type||'-')+' · guardrail: '+(EFF.layers.guardrail||[]).join(', ')+' · type: '+((EFF.layers.type||[]).join(', ')||'-')
    +' · project: '+((EFF.layers.project||[]).join(', ')||'-')+' · '+c.allow+' allow, '+c.ask+' ask, '+c.deny+' deny'
    +(k==='stmt'?' · '+EFF.items.filter(i=>i.reason==='default-deny').length+' default denies hidden (no statement names them)':'');
  $('xset').replaceChildren(...Object.entries(EFF.settings||{}).map(([k,s])=>{
    const tr=el('tr');cell(tr,'Setting',k);cell(tr,'Value',JSON.stringify(s.value));
    cell(tr,'From',(s.layer||'')+(s.policy?' · '+s.policy:''));
    const note=[];if(s.locked)note.push('locked');(s.ignored||[]).forEach(x=>note.push('ignored: '+x.join(' ')));
    cell(tr,'Note',note.join('; '));return tr}));
}
async function effective(){
  const p=$('xpick').value;if(!p)return;
  $('xhead').textContent='loading '+p+'...';
  const res=await api('GET','/effective?project='+encodeURIComponent(p));
  if(res.status!==200||!res.j.ok){EFF=null;$('xhead').textContent=err(res);$('xrow').replaceChildren();$('xset').replaceChildren();return}
  EFF=res.j;drawEff();
}
$('xpick').onchange=effective;$('xflt').oninput=drawEff;$('xkind').onchange=drawEff;

// ── simulator ──
function simWhen(){
  const S=META.schema,box=$('swhen'),keep={};box.querySelectorAll('[id^=sw-]').forEach(x=>keep[x.id]=x.value);box.replaceChildren();
  (S.when_keys||[]).forEach(k=>{
    const l=el('label','','when '+k);let x;
    const choice={tier:S.tiers,channel:S.channels}[k];
    if(choice){x=el('select');opts(x,['',...choice]);x.options[0].textContent='any'}
    else{x=el('input');x.type='text';x.placeholder='any'}
    x.id='sw-'+k;x.setAttribute('aria-label','when '+k);if(keep[x.id])x.value=keep[x.id];l.append(x);box.append(l)});
}
async function simulate(){
  const ctx={};(META.schema.when_keys||[]).forEach(k=>{const v=$('sw-'+k).value.trim();if(v)ctx[k]=v});
  const body={project:$('spick').value,kind:$('skind').value,name:$('sname').value.trim(),context:ctx};
  const box=$('sres');
  if(!body.name){box.replaceChildren(el('div','msg err','Name the action first.'));return}
  box.replaceChildren(el('div','msg','simulating...'));
  const res=await api('POST','/simulate',body);
  if(res.status!==200||!res.j.ok){box.replaceChildren(el('div','msg err',err(res)));return}
  const j=res.j,d=el('div','notice');
  const head=el('div','big');head.append(el('span','tag '+(j.effect||'none'),j.effect||'none'),document.createTextNode(' '+j.kind+' '+j.name));
  const ctxs=Object.entries(j.context||{}).map(([k,v])=>k+'='+v).join(', ');
  d.append(head,el('div','small','in '+j.project+(ctxs?' with '+ctxs:'')),
    el('div','','Decided by: '+(j.policy?decided(j):'no statement names it: default deny')),el('div','small mut','reason: '+(j.reason||'')));
  box.replaceChildren(d);
  const tr=el('tr');cell(tr,'Project',j.project);cell(tr,'Action',j.kind+' '+j.name+(ctxs?' ['+ctxs+']':''));
  cell(tr,'Effect').append(el('span','tag '+(j.effect||'none'),j.effect||'none'));cell(tr,'Decided by',decided(j));
  $('shist').prepend(tr);while($('shist').children.length>10)$('shist').lastChild.remove();
}
$('sbtn').onclick=simulate;$('sname').onkeydown=e=>{if(e.key==='Enter'&&!REMOTE)simulate()};

// ── tag a project ──
let TAG=null;
function heldBox(state){
  const box=$('gheld');box.replaceChildren();
  if(!state)return;
  box.append(el('h2','',state.branch?'SAVED TAG ON '+state.branch.toUpperCase():'TAG ON MASTER'));
  box.append(el('div','small','type '+(state.type||'-')+' · '+state.held.length+' change(s) held'));
  if(!state.branch&&state.held.length)box.append(el('div','small mut','Accepting a change starts branch policy/tag-'+$('gpick').value+'.'));
  state.held.forEach(id=>{const r=el('div','row');r.append(el('code','',id));
    const b=el('button','write','Accept');b.onclick=()=>accept(id,b);r.append(b);box.append(r)});
}
async function dry(){
  $('gsave').disabled=true;$('gdiff').hidden=true;say($('gmsg'),'');
  const box=$('gres');box.replaceChildren(el('div','msg','planning...'));
  const res=await api('POST','/tag-dry-run',{project:$('gpick').value,type:$('gtype').value});
  if(res.status!==200||!res.j.ok){box.replaceChildren(el('div','msg err',err(res)));heldBox(null);return}
  const j=TAG=res.j,d=el('div','notice');
  const empty=!j.diff.length;
  d.append(el('div','big',empty?'Empty diff: '+j.project+' as '+j.type+' keeps its configuration.':'The diff is not empty: the tag would change '+j.project+'.'));
  if(!empty)d.append(el('pre','diff',j.diff.join('\n')));
  if(j.proposed.length){d.append(el('h2','','PROPOSED BY THE TYPE'));
    j.proposed.forEach(id=>d.append(el('div','',id+(id.includes(':+')?'  (held: accept it after saving)':'  (a removal: no hold can keep it)'))))}
  else d.append(el('div','small mut','The type proposes no change.'));
  if(j.refused.length){d.append(el('h2','','REFUSED'));j.refused.forEach(w=>d.append(el('div','msg err',w)))}
  box.replaceChildren(d);heldBox(j.saved);
  const branch=j.saved&&j.saved.branch;
  $('gsave').disabled=REMOTE||!j.can_save||!empty||!!branch;
  if(branch)say($('gmsg'),'A tag is already saved on '+branch+': accept its changes below, or merge the branch before tagging again.');
}
$('gdry').onclick=dry;$('gpick').onchange=()=>{TAG=null;$('gres').replaceChildren();heldBox(null);$('gsave').disabled=true};
$('gtype').onchange=$('gpick').onchange;
$('gsave').onclick=async()=>{
  if(!TAG)return;$('gsave').disabled=true;say($('gmsg'),'saving...');
  const res=await api('POST','/tag-save',{project:TAG.project,type:TAG.type});
  if(res.status!==200||!res.j.ok){say($('gmsg'),err(res),'err');return}
  say($('gmsg'),'Tag saved on branch '+res.j.branch+' as '+res.j.commit+' ('+res.j.files.join(', ')+'). '+res.j.held.length
    +' change(s) held. Master is unchanged until the branch is merged.','good');
  const d=$('gdiff');d.textContent=res.j.diff;d.hidden=false;
  heldBox({branch:res.j.branch,type:TAG.type,held:res.j.held});
};
async function accept(id,btn){
  const p=$('gpick').value;
  if(!confirm('Accept '+id+' for '+p+'? Unlike the tag, this changes what the project may do once the branch is merged and deployed.'))return;
  btn.disabled=true;say($('gmsg'),'accepting...');
  const res=await api('POST','/tag-accept',{project:p,change:id});
  if(res.status!==200||!res.j.ok){say($('gmsg'),err(res),'err');btn.disabled=false;return}
  say($('gmsg'),'Accepted '+id+' on branch '+res.j.branch+' as '+res.j.commit+'. policy-compile.py --check shows it until it is deployed.','good');
  const d=$('gdiff');d.textContent=res.j.diff;d.hidden=false;
  heldBox({branch:res.j.branch,type:(TAG&&TAG.saved&&TAG.saved.type)||$('gtype').value,held:res.j.held});
}

(async()=>{
  $('ro').hidden=!REMOTE;
  await load();
  const pick=new URLSearchParams(location.search);
  if(pick.get('project')&&META.projects.includes(pick.get('project')))$('xpick').value=pick.get('project');
  if(META.policies.length)await open(pick.get('id')||META.policies[0].id);
  let v='list';try{v=localStorage.getItem('policies.view')||'list'}catch(e){}
  if(pick.get('project'))v='eff';else if(pick.get('id'))v='edit';
  view(VIEWS.includes(v)?v:'list');
})();
</script></body></html>"""
