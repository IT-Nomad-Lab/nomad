const $ = s => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h !== undefined) e.innerHTML = h; return e; };
async function getJSON(u){ try{ const r = await fetch(u); return await r.json(); }catch(e){ return null; } }

let alarms = { approvals:0, gpuTemp:0, down:0 };
function refreshAlert(){
  const red = alarms.approvals>0 || alarms.gpuTemp>=87 || alarms.down>0;
  const a = $('#alert');
  if(red){ a.className='alert red';
    a.textContent = '◉ ' + (alarms.approvals>0 ? `${alarms.approvals} APPROVAL${alarms.approvals>1?'S':''} PENDING`
                    : alarms.down>0 ? 'SERVICE OFFLINE' : 'GPU THERMAL'); }
  else { a.className='alert nominal'; a.textContent='● NOMINAL'; }
}

/* ── clock / stardate ── */
function tick(){
  const now = new Date();
  $('#clock').textContent = now.toISOString().slice(11,19);
  const sd = 41000 + (now - Date.UTC(2026,0,1))/86400000 * 2.731;
  $('#stardate').textContent = sd.toFixed(2);
}
setInterval(tick,1000); tick();

/* ── systems online ── */
/* ── live sparklines (rolling history, no deps) ── */
const HIST = { cpu:[], mem:[], gutil:[], gtemp:[] };
function pushHist(key, v){ if(v==null||isNaN(v)) return; const a=HIST[key]; a.push(+v); if(a.length>48) a.shift(); }
function spark(arr, color, max){
  if(!arr || arr.length<2) return '<span class="spark-empty"></span>';
  const w=120, h=20, mx=max||Math.max(1,...arr);
  const pts=arr.map((v,i)=>`${(i/(arr.length-1)*w).toFixed(1)},${(h-(Math.min(v,mx)/mx)*h).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">`+
    `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
}
function sparkrow(lbl,arr,color,max){ return `<div class="sparkrow"><span class="lbl">${lbl}</span>${spark(arr,color,max)}</div>`; }

async function pollServices(){
  const s = await getJSON('/api/services'); if(!s) return;
  alarms.down = s.filter(x=>!x.up).length; refreshAlert();
  const box = $('#systems'); box.innerHTML='';
  s.forEach(x=>{
    const r = el('div','row svc-item');
    r.title = 'Service details';
    r.appendChild(el('span','k', `<span class="dot ${x.up?'up':'down'}"></span>${x.name}`));
    r.appendChild(el('span','v', x.up?`${x.ms} ms`:'OFFLINE'));
    r.onclick = (ev)=>{
      ev.stopPropagation();
      const cn = 'nomad-' + x.name.toLowerCase().replace(/[^a-z0-9]/g,'');
      popover(r, `${x.name} · ${x.up?'ONLINE':'OFFLINE'}`, [
        x.up ? `latency: ${x.ms} ms` : 'not responding',
        x.url ? `probe: ${x.url}` : '',
        `restart (run in a terminal):`,
        `  docker compose up -d --force-recreate ${cn}`,
      ].filter(Boolean));
    };
    box.appendChild(r);
  });
}

/* ── telemetry + gpu ── */
function bar(lbl,pct,num,warn){
  return `<div class="barrow"><span class="lbl">${lbl}</span>
    <span class="bar ${warn?'warn':''}"><i style="width:${Math.min(100,pct||0)}%"></i></span>
    <span class="num">${num}</span></div>`;
}
async function pollSystem(){
  const s = await getJSON('/api/system'); if(!s) return;
  $('#rail-uptime').textContent = 'UP ' + fmtDur(s.uptime_s);
  pushHist('cpu', s.cpu_percent); pushHist('mem', s.mem_percent);
  let h = bar('CPU', s.cpu_percent, `${s.cpu_percent.toFixed(0)}%`, s.cpu_percent>92);
  h += bar('MEM', s.mem_percent, `${s.mem_used_gb}/${s.mem_total_gb}G`, s.mem_percent>92);
  if(s.disk_percent!=null) h += bar('DSK', s.disk_percent, `${s.disk_percent.toFixed(0)}%`);
  h += `<div class="cores">${(s.per_cpu||[]).map(c=>`<span class="core" style="background:${c>70?'#ff5544':c>30?'#ffcc66':'#1c1c2a'}" title="${c.toFixed(0)}%"></span>`).join('')}</div>`;
  h += sparkrow('CPU', HIST.cpu, '#6699ff', 100) + sparkrow('MEM', HIST.mem, '#ffcc66', 100);
  $('#telemetry').innerHTML = h;

  const g = s.gpu;
  if(g){
    alarms.gpuTemp = g.temp || 0; refreshAlert();
    pushHist('gutil', g.util); pushHist('gtemp', g.temp);
    const t = g.temp!=null ? g.temp.toFixed(0)+'°C' : '--';
    let gh = `<div class="row"><span class="k">${g.name}</span><span class="v">${t}</span></div>`;
    if(g.util!=null) gh += bar('UTIL', g.util, `${g.util.toFixed(0)}%`, g.util>97);
    if(g.mem_used!=null && g.mem_total) gh += bar('VRAM', g.mem_used/g.mem_total*100, `${(g.mem_used/1024).toFixed(1)}/${(g.mem_total/1024).toFixed(0)}G`, g.mem_used/g.mem_total>0.92);
    if(g.power!=null && g.power_limit) gh += bar('PWR', g.power/g.power_limit*100, `${g.power.toFixed(0)}/${g.power_limit.toFixed(0)}W`);
    else if(g.power!=null) gh += `<div class="barrow"><span class="lbl">PWR</span><span class="num" style="width:auto">${g.power.toFixed(0)} W</span></div>`;
    gh += sparkrow('UTIL', HIST.gutil, '#6699ff', 100) + sparkrow('TEMP', HIST.gtemp, '#ff7700', 100);
    $('#gpu').innerHTML = gh;
  } else { $('#gpu').innerHTML = '<div class="muted">nvidia-smi unavailable in container</div>'; }
}
function fmtDur(s){ const h=Math.floor(s/3600),m=Math.floor(s%3600/60); return h?`${h}h${m}m`:`${m}m`; }

/* ── projects ── */
async function pollProjects(){
  const d = await getJSON('/api/projects'); if(!d) return;
  const box = $('#projects'); box.innerHTML='';
  if(!d.projects.length){ box.innerHTML='<div class="muted">no projects yet — add Goals/Projects in Notion</div>'; return; }
  d.projects.forEach(p=>{
    const r = el('div','row proj-item');
    r.title = 'Open an interactive workspace in this project';
    r.appendChild(el('span','k', `▸ ${p.name}`));
    const st = (p.status||'—').replace(/[^A-Za-z]/g,'');
    r.appendChild(el('span','', `<span class="tag ${st}">${p.status}</span>`));
    r.onclick = ()=>{ if(window.NomadWorkspace) window.NomadWorkspace.open(p.name); };
    box.appendChild(r);
  });
}

/* ── popover (agent drill-in) ── */
function popover(anchor, title, lines){
  closePopover();
  const pop = el('div','popover');
  pop.appendChild(el('div','pop-title', title));
  if(!lines.length) pop.appendChild(el('div','muted','no recent activity'));
  lines.forEach(l=> pop.appendChild(el('div','pop-line', l)));
  document.body.appendChild(pop);
  const rc = anchor.getBoundingClientRect();
  pop.style.top = Math.min(rc.bottom + 6, window.innerHeight - pop.offsetHeight - 10) + 'px';
  pop.style.left = Math.max(8, Math.min(rc.left, window.innerWidth - pop.offsetWidth - 10)) + 'px';
  setTimeout(()=>document.addEventListener('click', closePopover, {once:true}), 0);
}
function closePopover(){ const p = $('.popover'); if(p) p.remove(); }

/* ── agents ── */
async function pollAgents(){
  const d = await getJSON('/api/agents'); const o = await getJSON('/api/ollama');
  if(!d) return;
  const box = $('#agents'); box.innerHTML='';
  d.agents.forEach(a=>{
    const r = el('div','row agent-item');
    r.title = 'Recent activity';
    r.appendChild(el('span','k',`${a.name}<span class="muted"> · ${a.model}</span>`));
    r.appendChild(el('span','', `<span class="tag ${a.status==='ACTIVE'?'active':'standby'}">${a.status}</span>`));
    r.onclick = async (ev)=>{
      ev.stopPropagation();
      const act = await getJSON('/api/activity'); const all = (act&&act.activity)||[];
      const mine = all.filter(x=> (x.agent||'').toLowerCase().includes(a.name.toLowerCase())).slice(0,8);
      popover(r, `${a.name} · recent`, mine.map(x=> `${x.ts?x.ts.slice(11,16):'--:--'}  ${x.action}`));
    };
    box.appendChild(r);
  });
  if(o && o.length){
    const r = el('div','row');
    r.appendChild(el('span','k','◇ loaded model'));
    r.appendChild(el('span','v', o.map(m=>`${m.name} (${m.size_gb}G)`).join(', ')));
    box.appendChild(r);
  }
}

/* ── activity ── */
async function pollActivity(){
  const d = await getJSON('/api/activity'); if(!d) return;
  const box = $('#activity'); box.innerHTML='';
  if(!d.activity.length){ box.innerHTML='<div class="muted">no logged actions yet</div>'; return; }
  d.activity.forEach(a=>{
    const t = a.ts ? a.ts.slice(11,16) : '--:--';
    const r = el('div','row');
    r.appendChild(el('span','k',`<span class="muted">${t}</span> ${a.action}`));
    r.appendChild(el('span','v',a.agent));
    box.appendChild(r);
  });
}

/* ── approvals (inline approve/reject → engine gate) ── */
async function decide(runId, decision, btn){
  if(decision==='rejected' && !confirm('Reject this run?')) return;
  document.querySelectorAll(`[data-rid="${runId}"]`).forEach(b=>b.disabled=true);
  try{ await fetch('/api/approvals/decision',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({run_id:runId, decision})}); }catch(e){}
  pollApprovals();
}
async function pollApprovals(){
  const d = await getJSON('/api/approvals'); if(!d) return;
  alarms.approvals = d.approvals.length; refreshAlert();
  const box = $('#approvals'); box.innerHTML='';
  if(!d.approvals.length){ box.innerHTML='<div class="muted">queue clear — no pending approvals</div>'; return; }
  d.approvals.forEach(a=>{
    const r = el('div','row appr');
    r.appendChild(el('span','k',`<span class="tag off">${a.type}</span> ${a.action}`));
    if(a.run_id){
      const acts = el('span','appr-acts');
      const ok = el('button','appr-ok','✓'); ok.title='approve & execute'; ok.dataset.rid=a.run_id;
      ok.onclick=()=>decide(a.run_id,'approved',ok);
      const no = el('button','appr-no','✗'); no.title='reject'; no.dataset.rid=a.run_id;
      no.onclick=()=>decide(a.run_id,'rejected',no);
      acts.appendChild(ok); acts.appendChild(no); r.appendChild(acts);
    } else { r.appendChild(el('span','v',a.by)); }
    box.appendChild(r);
    if(a.preview){ const pv = el('div','appr-pv', a.preview); box.appendChild(pv); }
  });
}

/* ── engineering crew (dev team) ── */
let backlogBusy = false;
async function runBacklog(btn){
  if(backlogBusy) return; backlogBusy = true;
  btn.disabled = true; const orig = btn.textContent; btn.textContent = 'QUEUING…';
  try{
    const r = await fetch('/api/devteam/run-backlog',{method:'POST'});
    const j = await r.json().catch(()=>({}));
    btn.textContent = j.queued!=null ? `QUEUED ${j.queued}` : (j.error ? 'ERR' : 'DONE');
  }catch(e){ btn.textContent = 'ERR'; }
  setTimeout(()=>{ btn.textContent = orig; btn.disabled = false; backlogBusy = false; }, 4000);
}
async function pollDevTeam(){
  const d = await getJSON('/api/devteam'); if(!d) return;
  const stat = $('#builder-stat');
  if(stat){ stat.textContent = d.builder_online ? `BUILDER ONLINE · ${d.buildable} repos` : 'BUILDER OFFLINE';
    stat.className = 'tag ' + (d.builder_online ? 'on' : 'off'); }
  const box = $('#devteam'); if(!box) return; box.innerHTML='';
  d.agents.forEach(a=>{
    const r = el('div','row');
    r.appendChild(el('span','k',`${a.name} <span class="muted">${a.role}</span>`));
    r.appendChild(el('span','v',a.model));
    box.appendChild(r);
  });
  const act = el('div','crew-act');
  const b = el('button','crew-btn','▶ WORK BACKLOG');
  b.title = d.builder_online ? 'Have the crew work its Notion backlog' : 'Builder offline';
  if(!d.builder_online) b.disabled = true;
  b.onclick = ()=>runBacklog(b);
  act.appendChild(b); box.appendChild(act);
}

/* ── proactive briefing: NOMAD speaks up about new alerts ── */
const announced = new Set();
async function pollBriefing(){
  const d = await getJSON('/api/briefing'); if(!d || !d.alerts) return;
  const live = new Set(d.alerts.map(a=>a.key));
  // forget cleared conditions so they can re-announce if they recur
  for(const k of [...announced]) if(!live.has(k)) announced.delete(k);
  for(const a of d.alerts){
    if(announced.has(a.key)) continue;
    announced.add(a.key);
    const node = addMsg('nomad', '', a.severity==='critical'?'proactive crit':'proactive');
    const line = '◉ '+a.text;
    node.textContent = line;
    if(ttsOn) speak(a.text);
  }
}

/* ── chat ── */
const history = [];
let ttsOn = false;
/* stable session id so NOMAD persists & rehydrates this conversation across reloads */
const SESSION_ID = (()=>{ let s=localStorage.getItem('nomad_session');
  if(!s){ s = (crypto.randomUUID ? crypto.randomUUID() : 'sess-'+Date.now()+'-'+Math.random().toString(16).slice(2));
    localStorage.setItem('nomad_session', s); } return s; })();
function addMsg(role, text, cls){
  const m = el('div', `msg ${role==='you'?'you':'nomad'} ${cls||''}`);
  m.appendChild(el('span','who', role==='you'?'OPERATOR':'NOMAD'));
  const body = el('span','body'); m.appendChild(body);
  $('#chatlog').appendChild(m); $('#chatlog').scrollTop=1e9;
  if(text!==undefined) body.textContent=text;
  return body;
}
/* lightweight markdown → HTML for display (bold/italic/code/headers/links/bullets), HTML-escaped */
function mdToHtml(t){
  let h = t.replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  h = h.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  h = h.replace(/^\s{0,3}#{1,6}\s+(.+)$/gm, '<b>$1</b>');
  h = h.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>').replace(/__([^_\n]+)__/g, '<b>$1</b>');
  h = h.replace(/(^|[^\w*])\*([^*\n]+)\*(?=[^\w*]|$)/g, '$1<i>$2</i>');
  h = h.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/^\s*[-*]\s+(.+)$/gm, '• $1');
  return h.replace(/\n/g, '<br>');
}
/* strip markdown to clean speakable text (so Piper doesn't read "asterisk asterisk") */
function mdToPlain(t){
  return t.replace(/`([^`\n]+)`/g,'$1')
    .replace(/\*\*([^*\n]+)\*\*/g,'$1').replace(/__([^_\n]+)__/g,'$1')
    .replace(/\*([^*\n]+)\*/g,'$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm,'')
    .replace(/\[([^\]]+)\]\([^)]+\)/g,'$1')
    .replace(/^\s*[-*]\s+/gm,'')
    .replace(/[*_`#>]/g,'').replace(/[ \t]{2,}/g,' ').trim();
}
function typewrite(node, text){
  return new Promise(resolve=>{
    node.parentElement.classList.add('cursor');
    const plain = mdToPlain(text); let i=0;          // animate clean text (no raw ** flashing)
    const iv=setInterval(async ()=>{ node.textContent=plain.slice(0,i++);
      $('#chatlog').scrollTop=1e9;
      if(i>plain.length){ clearInterval(iv); node.parentElement.classList.remove('cursor');
        node.innerHTML = mdToHtml(text);              // then render the markdown (bold/links/…)
        $('#chatlog').scrollTop=1e9;
        if(ttsOn) await speak(text);                  // speak() strips markdown internally
        resolve(); } }, 8);
  });
}
/* speak via local Piper TTS (NOMAD's on-device voice); fall back to browser speech */
let curAudio = null;
function speakBrowser(t){ try{ const u=new SpeechSynthesisUtterance(mdToPlain(t).slice(0,600)); u.rate=1.02; u.pitch=.9;
  speechSynthesis.cancel(); speechSynthesis.speak(u);}catch(e){} }
async function speak(t){
  const spoken = mdToPlain(t || '');
  if(!spoken.trim()) return;
  try{
    if(curAudio){ curAudio.pause(); curAudio=null; }
    const r = await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:spoken.slice(0,1200)})});
    if(!r.ok) throw new Error('tts '+r.status);
    const url = URL.createObjectURL(await r.blob());
    curAudio = new Audio(url);
    await new Promise(res=>{                       // resolve when playback ENDS (so a follow-up
      curAudio.onended=()=>{ URL.revokeObjectURL(url); res(); };   // listen doesn't capture NOMAD)
      curAudio.onerror=()=>res();
      curAudio.play().catch(()=>res());
    });
  }catch(e){ speakBrowser(t); }   // voice service down → browser speech
}

async function sendChat(text){
  if(!text.trim()) return;
  addMsg('you', text); history.push({role:'user',content:text});
  const think = addMsg('nomad', 'Working…', 'think');
  const resp = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({messages:history.slice(-12), model:$('#model').value, session_id:SESSION_ID})}).then(r=>r.json()).catch(()=>({reply:'⚠ link error'}));
  think.parentElement.classList.remove('think');
  const reply = resp.reply || '(no response)';
  history.push({role:'assistant',content:reply});
  await typewrite(think, reply);     // resolves after NOMAD has typed AND spoken the reply
  return reply;
}
async function dispatch(mode, project, task){
  addMsg('you', `/${mode} ${project}: ${task}`);
  const think = addMsg('nomad', `Dispatching Builder → ${project} (${mode})… working in the repo, this can take a minute.`, 'think');
  const r = await fetch('/api/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project,task,mode})}).then(x=>x.json()).catch(()=>({ok:false,error:'link error'}));
  think.parentElement.classList.remove('think');
  let out;
  if(!r.ok){ out = '⚠ '+(r.error||'dispatch failed'); }
  else {
    out = `[${(r.mode||mode).toUpperCase()} · ${r.project} · ${r.secs}s]\n\n${r.summary||'(done)'}`;
    if(r.mode==='build'){
      out += (r.changed && r.changed.length)
        ? `\n\n▼ uncommitted changes (review before committing):\n${r.changed.join('\n')}\n\n${r.diffstat||''}`
        : `\n\n(no file changes made)`;
    }
  }
  typewrite(think, out);
}
$('#chatform').addEventListener('submit', e=>{ e.preventDefault();
  const v=$('#chatin').value.trim(); $('#chatin').value='';
  const m = v.match(/^\/(build|plan)\s+([^:]+):\s*([\s\S]+)/i);
  if(m){ dispatch(m[1].toLowerCase(), m[2].trim(), m[3].trim()); return; }
  sendChat(v);
});
$('#tts').addEventListener('click', ()=>{ ttsOn=!ttsOn; $('#tts').classList.toggle('off',!ttsOn);
  if(!ttsOn) speechSynthesis.cancel(); });

/* ── voice: 100% local via Whisper (/api/stt) — works in the desktop shell (WebView2)
      where browser speech recognition is unavailable. ── */
const hasMic = !!(navigator.mediaDevices && window.MediaRecorder);
let micStream = null;
async function getMic(){ if(!micStream) micStream = await navigator.mediaDevices.getUserMedia({audio:true}); return micStream; }

/* record a fixed-length clip from the shared mic stream → Blob */
function recordClip(stream, ms){
  return new Promise(res=>{
    let rec; try{ rec = new MediaRecorder(stream); }catch(e){ return res(null); }
    const ch=[]; rec.ondataavailable=e=>{ if(e.data && e.data.size) ch.push(e.data); };
    rec.onstop=()=>res(new Blob(ch, {type: rec.mimeType || 'audio/webm'}));
    rec.start(); setTimeout(()=>{ try{ rec.stop(); }catch(_){ } }, ms);
  });
}
async function transcribe(blob){
  if(!blob || blob.size < 1200) return '';
  try{ const r=await fetch('/api/stt',{method:'POST',headers:{'Content-Type':blob.type},body:blob});
    return ((await r.json()).text || '').trim(); }catch(e){ return ''; }
}

/* push-to-talk: click to start, click again to stop → transcribe → send */
let pttRec = null;
async function toggleMic(){
  if(pttRec){ try{ pttRec.stop(); }catch(_){ } return; }
  let stream; try{ stream = await getMic(); }
  catch(e){ addMsg('nomad','⚠ microphone blocked: '+((e&&e.message)||e),'think'); return; }
  const ch=[]; pttRec = new MediaRecorder(stream);
  pttRec.ondataavailable=e=>{ if(e.data && e.data.size) ch.push(e.data); };
  pttRec.onstop=async()=>{
    $('#mic').classList.remove('rec'); const r=pttRec; pttRec=null;
    const blob=new Blob(ch,{type:r.mimeType||'audio/webm'});
    if(blob.size < 1200) return;
    const think=addMsg('nomad','Transcribing…','think');
    const t=await transcribe(blob); think.parentElement.remove();
    if(t) sendChat(t); else addMsg('nomad','(didn’t catch that — try again)','think');
  };
  pttRec.start(); $('#mic').classList.add('rec');
}
if(hasMic){ $('#mic').addEventListener('click', toggleMic); } else { $('#mic').style.display='none'; }

/* always-on wake word: continuously transcribe short windows; on “Hey NOMAD …”, run the
   command. Fully local (Whisper) so it works in the desktop shell. */
let wakeOn=false;
const WAKE=/\b(hey\s+|ok\s+|okay\s+)?(nomad|jarvis|computer)\b[\s,.:;!?-]*/i;
async function wakeLoop(){
  if(!wakeOn) return;
  if(pttRec || convoMode){ setTimeout(wakeLoop, 600); return; }   // yield to PTT / active conversation
  let stream; try{ stream=await getMic(); }catch(e){ wakeOn=false; $('#wake').classList.add('off'); return; }
  const clip=await recordClip(stream, 3500);
  if(wakeOn && !convoMode && clip){
    const said=await transcribe(clip);
    const m=said && said.match(WAKE);
    if(m){
      const cmd=said.slice(m.index+m[0].length).trim();
      if(cmd.length>1){ await sendChat(cmd); await onWake(); }   // command after wake → then converse
      else { await onWake(); }                                  // just the wake word → enter conversation
    }
  }
  if(wakeOn) wakeLoop();
}
/* on wake-word detection → enter CONVERSATION MODE: say "Hey Jarvis" ONCE, then just keep
   talking. After each reply NOMAD listens for your next turn (no wake word needed) until you
   go quiet for one window — then it returns to wake-word listening. */
let convoMode=false;
async function onWake(){
  if(pttRec || convoMode) return;
  convoMode=true; $('#wake').classList.add('convo');
  let first=true;
  try{
    while(convoMode){
      const prompt = first ? 'Listening…' : 'Listening… (just talk — no wake word; pause to end)';
      first=false;
      const think=addMsg('nomad', prompt, 'think');
      let stream; try{ stream=await getMic(); }catch(e){ think.parentElement.remove(); break; }
      const clip=await recordClip(stream, 5000);
      think.parentElement.remove();
      const t=await transcribe(clip);
      if(!t){ break; }                 // silence → end the conversation
      await sendChat(t);               // resolves after NOMAD has finished speaking
    }
  }catch(e){}
  finally{
    convoMode=false; $('#wake').classList.remove('convo');
    if(wakeOn) addMsg('nomad','— conversation ended. Say “Hey Jarvis” to talk again. —','think');
  }
}

/* snappy on-device wake word via openWakeWord — stream 16 kHz mono PCM to nomad-voice (/ws/wake);
   it runs the 'hey jarvis' model and signals back the instant it fires. Local, free, no account,
   and works in the desktop shell. Falls back to the Whisper listen-loop if it can't start. */
let oww=null;
function downsample(buf, inRate, outRate){
  if(outRate>=inRate) return buf;
  const ratio=inRate/outRate, len=Math.floor(buf.length/ratio), out=new Float32Array(len);
  for(let i=0;i<len;i++) out[i]=buf[Math.floor(i*ratio)];
  return out;
}
function startOWW(){
  return new Promise(async (resolve,reject)=>{
    let stream; try{ stream=await getMic(); }catch(e){ return reject(e); }
    const proto = location.protocol==='https:'?'wss':'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/wake`); ws.binaryType='arraybuffer';
    const AC = window.AudioContext || window.webkitAudioContext;
    let ac; try{ ac = new AC({sampleRate:16000}); }catch(e){ ac = new AC(); }
    const src = ac.createMediaStreamSource(stream);
    const proc = ac.createScriptProcessor(4096,1,1);
    const sink = ac.createGain(); sink.gain.value = 0;   // run the processor without echoing to speakers
    const inRate = ac.sampleRate;
    proc.onaudioprocess = e=>{
      if(!ws || ws.readyState!==1) return;
      const pcm = inRate===16000 ? e.inputBuffer.getChannelData(0) : downsample(e.inputBuffer.getChannelData(0), inRate, 16000);
      const i16 = new Int16Array(pcm.length);
      for(let i=0;i<pcm.length;i++){ const s=Math.max(-1,Math.min(1,pcm[i])); i16[i]=s<0?s*0x8000:s*0x7fff; }
      ws.send(i16.buffer);
    };
    src.connect(proc); proc.connect(sink); sink.connect(ac.destination);
    oww={ws,ac,src,proc,sink,stream};
    ws.onmessage=ev=>{ try{ const d=JSON.parse(ev.data); if(d.error){ stopOWW(); if(wakeOn) wakeLoop(); } else if(d.wake){ onWake(); } }catch(_){ } };
    ws.onopen=()=>resolve();
    ws.onerror=()=>{};
    ws.onclose=()=>{ if(oww && oww.ws===ws){ stopOWW(); } };
    setTimeout(()=> ws.readyState===1 ? resolve() : reject(new Error('wake ws timeout')), 4000);
  });
}
function stopOWW(){
  if(!oww) return; const o=oww; oww=null;
  try{o.proc.disconnect();}catch(_){ } try{o.sink.disconnect();}catch(_){ } try{o.src.disconnect();}catch(_){ }
  try{o.ac.close();}catch(_){ } try{o.ws.close();}catch(_){ }
}

if(hasMic){
  $('#wake').addEventListener('click', async ()=>{
    if(wakeOn){                                   // turn OFF
      wakeOn=false; $('#wake').classList.add('off'); stopOWW();
      return;
    }
    wakeOn=true; $('#wake').classList.remove('off');
    try{ await startOWW(); addMsg('nomad','Wake word armed — say “Hey Jarvis”.','think'); return; }   // preferred: openWakeWord (instant)
    catch(e){ addMsg('nomad','Listening for “Hey Jarvis …” (local fallback — a touch slower).','think'); }
    wakeLoop();                                   // fallback: Whisper listen-loop
  });
} else { $('#wake').style.display='none'; }

/* ── LIVE VOICE: real-time, interruptible conversation (barge-in) via nomad-voice/WebRTC ──
   Audio streams both ways over a peer connection to the host-native voice service; Silero VAD
   yields the instant you speak, Whisper transcribes, NOMAD's brain (memory + intent + gate) replies,
   Piper speaks it. The reply step is the SAME brain as the text chat — so "run diagnostics",
   "research X", "approve"/"reject" all work by voice. Signaling is cross-origin to :8200 (media is
   peer-to-peer); the classic push-to-talk 🎤 and 👂 wake word stay as-is. */
let rtPc=null, rtStream=null, rtBase=null;
async function rtConfig(){
  if(rtBase!==null) return rtBase;
  const c = await getJSON('/api/voice-config'); rtBase = (c && c.rt_url) || 'http://localhost:8200';
  return rtBase;
}
function rtStatus(t){ const b=$('#live'); if(b) b.title='live voice — '+t; }
async function rtHangup(msg){
  if(rtStream){ rtStream.getTracks().forEach(t=>{ try{t.stop();}catch(_){ } }); rtStream=null; }
  if(rtPc){ try{ rtPc.close(); }catch(_){ } rtPc=null; }
  const a=$('#rt-audio'); if(a) a.srcObject=null;
  $('#live').classList.add('off');
  if(msg) addMsg('nomad', msg, 'think');
}
async function rtConnect(){
  const base = await rtConfig();
  let stream; try{ stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}}); }
  catch(e){ addMsg('nomad','⚠ microphone blocked: '+((e&&e.name)||e),'think'); $('#live').classList.add('off'); return; }
  rtStream=stream;
  const pc=new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]});
  rtPc=pc; rtStatus('connecting…');
  stream.getTracks().forEach(t=>pc.addTrack(t,stream));
  pc.ontrack=e=>{ $('#rt-audio').srcObject=e.streams[0]; };
  pc.onconnectionstatechange=()=>{ const c=pc.connectionState;
    if(c==='connected'){ rtStatus('connected — talk (interrupt any time)'); addMsg('nomad','🎙️ Live voice connected — just talk. Interrupt any time; say “approve” to clear the gate. Click ▮▮ to end.','think'); }
    else if(c==='failed'||c==='disconnected'||c==='closed'){ if(rtPc===pc) rtHangup(c==='failed'?'Live voice couldn’t establish audio (WSL2 networking?). Push-to-talk still works.':null); } };
  const offer=await pc.createOffer({offerToReceiveAudio:true}); await pc.setLocalDescription(offer);
  await Promise.race([                                   // wait for ICE, but don't hang forever on STUN
    new Promise(r=>{ if(pc.iceGatheringState==='complete') return r();
      const h=()=>{ if(pc.iceGatheringState==='complete'){ pc.removeEventListener('icegatheringstatechange',h); r(); } };
      pc.addEventListener('icegatheringstatechange',h); }),
    new Promise(r=>setTimeout(r,2000))]);
  try{
    const res=await fetch(base+'/api/offer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type})});
    if(!res.ok){ rtHangup('⚠ voice service error '+res.status); return; }
    await pc.setRemoteDescription(await res.json());
  }catch(e){ rtHangup('⚠ voice service unreachable ('+base+'): '+((e&&e.message)||e)); }
}
$('#live').addEventListener('click', async ()=>{
  if(rtPc || rtStream){ await rtHangup('— live voice ended —'); return; }   // toggle off
  $('#live').classList.remove('off');
  if(wakeOn){ wakeOn=false; $('#wake').classList.add('off'); stopOWW(); }    // free the mic for the live session
  await rtConnect();
});

/* greeting + rehydrate this session's transcript from NOMAD's memory */
const GREETING = 'NOMAD online. All stations linked. State your objective, Commander.\n\nTip: name a project and I read its docs. To dispatch the Builder into a repo:\n  /plan <project>: <task>   (read-only plan)\n  /build <project>: <task>  (makes uncommitted edits)';
(async function rehydrate(){
  const data = await getJSON('/api/history?session_id='+encodeURIComponent(SESSION_ID));
  const turns = (data && data.turns) || [];
  if(turns.length){
    addMsg('nomad', '↩ Resuming our conversation — '+turns.length+' turns restored from memory.', 'think');
    for(const t of turns){
      addMsg(t.role==='user'?'you':'nomad', t.content);
      history.push({role:t.role, content:t.content});
    }
  } else {
    addMsg('nomad', GREETING);
  }
})();

/* ── poll loops ── */
function loopFast(){ pollSystem(); pollServices(); }
function loopSlow(){ pollProjects(); pollAgents(); pollActivity(); pollApprovals(); pollDevTeam(); }
loopFast(); loopSlow();
setInterval(loopFast, 4000);
setInterval(loopSlow, 12000);
/* proactive watch — NOMAD speaks up about new alerts */
pollBriefing(); setInterval(pollBriefing, 20000);
