/* NOMAD Project Workspace — an interactive Claude Code session inside a selected repo.
 *
 * Opens a full-screen xterm bound to the console's /ws/terminal proxy, which bridges to the native
 * PTY daemon running `claude` in that project's repo. Keystrokes → WS; PTY output → terminal.
 * Closing the panel leaves the claude session alive (the daemon persists it); reopening re-attaches.
 */
(function () {
  const elPanel = document.getElementById('workspace');
  const elTerm = document.getElementById('ws-term');
  const elProj = document.getElementById('ws-project');
  const elStat = document.getElementById('ws-status');
  let term, fit, ws, current = null, manualClose = false;

  function setStatus(s, cls) { elStat.textContent = s; elStat.className = 'ws-status ' + (cls || ''); }

  function ensureTerm() {
    if (term) return;
    term = new window.Terminal({
      cursorBlink: true, fontFamily: '"Share Tech Mono", ui-monospace, monospace',
      fontSize: 13, theme: { background: '#05060a', foreground: '#ffb000', cursor: '#ffb000',
        selectionBackground: '#33405a' }, scrollback: 5000,
    });
    fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(elTerm);
    term.onData(d => { if (ws && ws.readyState === 1) ws.send(d); });           // keystrokes → WS
    window.addEventListener('resize', doFit);
  }

  function doFit() {
    if (!fit || elPanel.classList.contains('ws-hidden')) return;
    try { fit.fit(); } catch (e) { return; }
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
    }
  }

  function connect(project) {
    manualClose = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const cols = (term && term.cols) || 80, rows = (term && term.rows) || 24;
    const url = `${proto}://${location.host}/ws/terminal?project=${encodeURIComponent(project)}&cols=${cols}&rows=${rows}`;
    setStatus('● connecting', 'warn');
    ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { setStatus('● LIVE', 'ok'); setTimeout(doFit, 60); term.focus(); };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') term.write(ev.data);
      else term.write(new Uint8Array(ev.data));
    };
    ws.onclose = () => {
      setStatus(manualClose ? '○ closed' : '○ disconnected', manualClose ? '' : 'err');
    };
    ws.onerror = () => setStatus('○ error', 'err');
  }

  function open(project) {
    ensureTerm();
    current = project;
    elProj.textContent = project;
    elPanel.classList.remove('ws-hidden');
    setTimeout(() => { doFit(); connect(project); }, 30);
  }

  function close() {
    manualClose = true;
    if (ws) { try { ws.close(); } catch (e) {} }
    elPanel.classList.add('ws-hidden');
  }

  function reconnect() {
    if (!current) return;
    if (ws) { try { ws.close(); } catch (e) {} }
    if (term) term.reset();
    connect(current);
  }

  document.getElementById('ws-close').onclick = close;
  document.getElementById('ws-reconnect').onclick = reconnect;
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !elPanel.classList.contains('ws-hidden')) close();
  });

  // public API for app.js (clickable projects)
  window.NomadWorkspace = { open, close };
})();
