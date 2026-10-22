// NOMAD's Project dropdown for Open WebUI (docs/prd/owui-sessions.md §6.5, #1137), and the Dispatch
// button beside it (#1441).
//
// Served as /static/loader.js: Open WebUI's index.html loads that file and ships it empty, and
// docker-compose.yml mounts this one over it read-only. No build step; plain browser JS.
//
// It draws a Project select beside the model selector: left of it in the message box (0.11.4 and 0.12.0's
// default), right of it when the selector sits in the top bar. The options are the `nomad.*`
// entries the NOMAD pipe lists (hidden from the model dropdown by scripts/owui-sync-pipe.py):
// General, each project, + New project. The pick rides on the first message of a new chat as the
// chat variable `nomad_project`; NOMAD Auto answers for it, and the global filter adds its context
// for any other model. From that message on, the select is locked to the chat's project. The
// engine holds the real lock, and the pipe or filter writes the bound entry back to the chat, so
// a + New project chat shows its project's name once M3 has set it (the select re-reads the chat
// while it's on + New project).
(function (root) {
  "use strict";

  var VAR = "nomad_project";
  var PREFIX = "nomad.";
  var AUTO = "nomad.auto";
  var GENERAL = "general";
  var NEW = "new-project";
  var PICK_KEY = "nomad.project.pick";
  var POLL_MS = 10000;
  // #1218: the tab icon. Open WebUI points it at the 512 px favicon.png, which blurs at 16 px and
  // which browsers keep cached under the old URL; the 96 px cut is drawn for small sizes, and the
  // version query makes browsers fetch it once. Bump ?v= when the logo changes.
  var TAB_ICON = "/static/favicon-96x96.png?v=1218";

  // Dropdown options from /api/models: [{id, label}], General first and + New project last.
  function options(models) {
    var out = [];
    (models || []).forEach(function (m) {
      var id = String((m && m.id) || "");
      if (id.indexOf(PREFIX) !== 0 || id === AUTO) return;
      var entry = id.slice(PREFIX.length);
      var label = String(m.name || entry).replace(/^NOMAD\s*·\s*/, "").replace(/ \(local\)$/, "");
      out.push({ id: entry, label: label });
    });
    var rank = function (o) { return o.id === GENERAL ? 0 : o.id === NEW ? 2 : 1; };
    return out.sort(function (a, b) { return rank(a) - rank(b); });
  }

  // The chat id in an Open WebUI path (/c/<id>), or "".
  function chatIdOf(path) {
    var m = /^\/c\/([^/?#]+)/.exec(path || "");
    return m ? decodeURIComponent(m[1]) : "";
  }

  // What the select shows: {entry, locked}. A saved chat is locked to its variable, or to General
  // when it has none (it went out without a project). `sent` is the entry of a message this page
  // sent before the chat's own record says so.
  function view(chatId, chat, sent, pick) {
    if (!chatId) return sent ? { entry: sent, locked: true } : { entry: pick || GENERAL, locked: false };
    var vars = (chat && chat.variables) || {};
    return { entry: vars[VAR] || sent || GENERAL, locked: true };
  }

  // The completion body with the pick added, or null to send it untouched. Only an unlocked
  // (new) chat gets it: a saved chat keeps the variables Open WebUI already has.
  function withProject(body, state) {
    if (state.locked) return null;
    var vars = body.chat_variables && typeof body.chat_variables === "object" ? body.chat_variables : {};
    var out = {};
    for (var k in body) out[k] = body[k];
    out.chat_variables = {};
    for (var v in vars) out.chat_variables[v] = vars[v];
    out.chat_variables[VAR] = state.entry;
    return out;
  }

  function isCompletion(input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var method = (init && init.method) || (input && input.method) || "GET";
    return method.toUpperCase() === "POST" && /\/api\/chat\/completions(\?|$)/.test(url)
      && init && typeof init.body === "string";
  }

  function store(ls) {
    return {
      get: function () { try { return ls.getItem(PICK_KEY) || ""; } catch (e) { return ""; } },
      set: function (v) { try { ls.setItem(PICK_KEY, v); } catch (e) { /* private window */ } },
    };
  }


  // ---- Dispatch (#1441, docs/prd/owui-dispatch.md): send a work package to the agent. ----------
  // The panel's calls go to the NOMAD pipe as a completion with no chat id and a `nomad_rpc` key:
  // Open WebUI checks the login and saves nothing, and the pipe forwards them to the engine's
  // /owui/dispatch. Sending starts the item's gate run; nothing here approves it.
  var RPC_MODEL = AUTO;
  var SEEN_KEY = "nomad.dispatch.ids";
  var PANEL_POLL_MS = 10000;
  var BG_POLL_MS = 60000;
  var LIVE = { assigned: 1, "at-gate": 1, building: 1, approved: 1 };
  var BADGES = {
    assigned: "🤖 assigned", "at-gate": "⏸ at gate", building: "🔨 building",
    approved: "✓ approved", done: "✔ done", declined: "✕ declined", failed: "⚠ failed",
    withdrawn: "↩ withdrawn",
  };

  function rpcBody(op, entry, extra) {
    var rpc = { op: op, entry: entry || GENERAL };
    for (var k in extra || {}) rpc[k] = extra[k];
    return { model: RPC_MODEL, stream: false, messages: [{ role: "user", content: "nomad dispatch " + op }],
             nomad_rpc: rpc };
  }

  // The engine's JSON out of the completion, or {ok:false, error}.
  function rpcResult(res) {
    var text = res && res.choices && res.choices[0] && res.choices[0].message
      && res.choices[0].message.content;
    if (typeof text !== "string") return { ok: false, error: "no answer from the NOMAD pipe" };
    try { return JSON.parse(text); } catch (e) { return { ok: false, error: text.slice(0, 200) }; }
  }

  function badge(agent) { return BADGES[agent] || ""; }

  // Whether the row offers "Assign to agent": never started, or its last run ended without acting.
  function canDispatch(agent) {
    return !agent || agent === "declined" || agent === "failed" || agent === "withdrawn";
  }

  // The ids worth polling in the background: the ones still with the agent.
  function liveIds(rows) {
    return (rows || []).filter(function (r) { return LIVE[r.agent]; }).map(function (r) { return r.id; });
  }

  // The Dispatch button's label: how many dispatched items wait at the gate.
  function buttonLabel(rows) {
    var gate = (rows || []).filter(function (r) { return r.agent === "at-gate"; }).length;
    return gate ? "Dispatch · " + gate + " at gate" : "Dispatch";
  }

  // The live part. `env` is {window, document, fetch, localStorage}.
  function start(env) {
    var win = env.window, doc = env.document, realFetch = env.fetch;
    var picks = store(env.localStorage);
    var state = { entry: picks.get() || GENERAL, locked: false, chatId: "", opts: [], sent: "" };
    var sentFor = {};                   // chat id -> entry sent from this page
    var poll = null, select = null, wrap = null;

    function token() {
      try { return env.localStorage.getItem("token") || ""; } catch (e) { return ""; }
    }
    function api(path) {
      return realFetch.call(win, path, { headers: { Authorization: "Bearer " + token() } })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
    }

    function loadOptions() {
      return api("/api/models").then(function (res) {
        state.opts = options((res && res.data) || res);
        render();
      }).catch(function () { /* not signed in yet; the observer retries on the next change */ });
    }

    function refresh() {
      var chatId = chatIdOf(win.location.pathname);
      if (chatId !== state.chatId) state.sent = "";
      state.chatId = chatId;
      if (poll) { win.clearTimeout(poll); poll = null; }
      if (!chatId) { apply(view("", null, state.sent, picks.get())); return; }
      apply(view(chatId, null, sentFor[chatId], ""));
      api("/api/v1/chats/" + encodeURIComponent(chatId)).then(function (chat) {
        if (state.chatId !== chatId) return;
        apply(view(chatId, chat, sentFor[chatId], ""));
        if (state.entry === NEW) poll = win.setTimeout(refresh, POLL_MS);
      }).catch(function () { /* keep the locked view */ });
    }

    function apply(v) {
      state.entry = v.entry;
      state.locked = v.locked;
      var known = state.opts.some(function (o) { return o.id === v.entry; });
      if (!known && state.opts.length && v.entry.indexOf("p-") === 0) loadOptions();
      render();
    }

    function render() {
      if (!select) return;
      var opts = state.opts.length ? state.opts.slice() : [{ id: GENERAL, label: "General" }];
      if (!opts.some(function (o) { return o.id === state.entry; })) {
        opts.push({ id: state.entry, label: state.entry.replace(/^p-/, "") });
      }
      var html = opts.map(function (o) {
        return '<option value="' + esc(o.id) + '">' + esc(o.label) + "</option>";
      }).join("");
      if (select.getAttribute("data-html") !== html) {
        select.innerHTML = html;
        select.setAttribute("data-html", html);
      }
      select.value = state.entry;
      select.disabled = state.locked;
      select.style.opacity = state.locked ? "0.7" : "1";
      select.style.cursor = state.locked ? "not-allowed" : "pointer";
      wrap.firstChild.textContent = state.locked ? "Project 🔒" : "Project";
      wrap.title = state.locked ? "This chat stays on its project. Start a new chat to pick another."
        : "Project for this chat. Fixed once you send the first message.";
      wrap.setAttribute("data-locked", state.locked ? "1" : "0");
    }

    function esc(s) {
      return String(s).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }

    function mount() {
      var inBox = doc.getElementById("model-selector-model-button");
      var anchor = inBox || doc.getElementById("model-selector-0-button");
      if (!anchor) return;
      var h = host(anchor);
      var placed = inBox ? wrap && wrap.nextElementSibling === h : wrap && wrap.previousElementSibling === h;
      if (placed && doc.body.contains(wrap)) { mountDispatch(); return; }
      if (!wrap) {
        wrap = doc.createElement("label");
        wrap.id = "nomad-project";
        wrap.style.cssText = "display:inline-flex;align-items:center;gap:4px;margin-left:8px;"
          + "margin-right:8px;font-size:0.875rem;opacity:0.9;white-space:nowrap;flex:none";
        wrap.innerHTML = '<span aria-hidden="true">Project</span>';
        select = doc.createElement("select");
        select.setAttribute("aria-label", "NOMAD project");
        select.style.cssText = "background:transparent;color:inherit;border:1px solid "
          + "rgba(128,128,128,0.35);border-radius:8px;padding:2px 6px;max-width:12rem;"
          + "font:inherit;cursor:pointer;appearance:auto;-webkit-appearance:menulist";
        select.addEventListener("change", function () {
          if (state.locked) { render(); return; }
          state.entry = select.value;
          picks.set(select.value);
        });
        wrap.appendChild(select);
        loadOptions();
      }
      h.parentNode.insertBefore(wrap, inBox ? h : h.nextSibling);
      mountDispatch();
      render();
    }


    // ---- Dispatch panel
    var seen = {                                       // ids sent from this browser, still live
      get: function () {
        try { return JSON.parse(env.localStorage.getItem(SEEN_KEY) || "[]"); } catch (e) { return []; }
      },
      set: function (ids) {
        try { env.localStorage.setItem(SEEN_KEY, JSON.stringify(ids.slice(0, 50))); } catch (e) { /* private window */ }
      },
    };
    var dp = { btn: null, panel: null, rows: [], live: [], timer: null, msg: "" };

    function rpc(op, extra) {
      return realFetch.call(win, "/api/chat/completions", {
        method: "POST",
        headers: { Authorization: "Bearer " + token(), "Content-Type": "application/json" },
        body: JSON.stringify(rpcBody(op, state.entry, extra)),
      }).then(function (r) { return r.json(); }).then(rpcResult)
        .catch(function (e) { return { ok: false, error: String(e && e.message || e) }; });
    }

    function remember(rows) {
      var byId = {};
      dp.live.forEach(function (r) { byId[r.id] = r; });
      rows.forEach(function (r) { byId[r.id] = r; });
      dp.live = Object.keys(byId).map(function (k) { return byId[k]; })
        .filter(function (r) { return LIVE[r.agent]; });
      seen.set(liveIds(dp.live));
      if (dp.btn) dp.btn.textContent = buttonLabel(dp.live);
    }

    function poll() {
      var ids = dp.panel ? dp.rows.filter(function (r) { return LIVE[r.agent]; }).map(function (r) { return r.id; })
        : seen.get();
      if (!ids.length) return;
      rpc("status", { ids: ids }).then(function (res) {
        if (!res.ok) return;
        var fresh = {};
        (res.items || []).forEach(function (r) { fresh[r.id] = r; });
        dp.rows = dp.rows.map(function (r) { return fresh[r.id] || r; });
        remember(res.items || []);
        drawPanel();
      });
    }

    // Polls run while the panel is open, or while something sent from here is still live.
    function schedule() {
      if (dp.timer) { win.clearTimeout(dp.timer); dp.timer = null; }
      if (!dp.panel && !seen.get().length) return;
      dp.timer = win.setTimeout(function () { dp.timer = null; poll(); schedule(); },
                                dp.panel ? PANEL_POLL_MS : BG_POLL_MS);
    }

    function openPanel() {
      if (dp.panel) { closePanel(); return; }
      dp.panel = doc.createElement("div");
      dp.panel.id = "nomad-dispatch-panel";
      dp.panel.setAttribute("role", "dialog");
      dp.panel.setAttribute("aria-label", "Dispatch a work package to the agent");
      dp.panel.style.cssText = "position:fixed;right:16px;bottom:96px;z-index:9999;width:min(30rem,calc(100vw - 32px));"
        + "max-height:60vh;overflow:auto;background:var(--color-gray-50,#fff);color:inherit;"
        + "border:1px solid rgba(128,128,128,0.35);border-radius:12px;padding:10px;"
        + "box-shadow:0 8px 24px rgba(0,0,0,0.25);font-size:0.875rem";
      if (doc.documentElement.classList.contains("dark")) dp.panel.style.background = "#1f1f1f";
      dp.panel.addEventListener("click", onPanelClick);
      doc.body.appendChild(dp.panel);
      dp.msg = "Loading open work…";
      drawPanel();
      rpc("items").then(function (res) {
        if (!dp.panel) return;
        dp.msg = res.ok ? (res.items.length ? "" : "Nothing open on " + res.board + ".") : "⚠ " + res.error;
        dp.rows = res.ok ? res.items : [];
        dp.board = res.board || "";
        remember(dp.rows);
        drawPanel();
      });
      schedule();
    }

    function closePanel() {
      if (dp.panel && dp.panel.parentNode) dp.panel.parentNode.removeChild(dp.panel);
      dp.panel = null;
      schedule();
    }

    function onPanelClick(ev) {
      var t = ev.target;
      if (t && t.getAttribute("data-close")) { closePanel(); return; }
      var id = t && t.getAttribute("data-dispatch");
      if (!id) return;
      t.disabled = true;
      t.textContent = "Sending…";
      rpc("dispatch", { id: Number(id) }).then(function (res) {
        if (res.ok && res.item) {
          dp.rows = dp.rows.map(function (r) { return r.id === res.item.id ? res.item : r; });
          remember([res.item]);
          dp.msg = "#" + id + " is at the gate (run " + (res.run_id || "?") + "). " + (res.note || "");
        } else {
          dp.msg = "⚠ #" + id + ": " + (res.error || "not sent");
        }
        drawPanel();
      });
    }

    function drawPanel() {
      if (!dp.panel) return;
      var head = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
        + "<strong>Dispatch to agent" + (dp.board ? " · " + esc(dp.board) : "") + "</strong>"
        + '<button type="button" data-close="1" aria-label="Close" style="padding:0 6px">✕</button></div>';
      var note = dp.msg ? '<div style="margin:4px 0 8px;opacity:0.85">' + esc(dp.msg) + "</div>" : "";
      var list = dp.rows.map(function (r) {
        var b = badge(r.agent);
        var tip = r.type + " · " + r.status + (r.run_id ? " · run " + r.run_id : "")
          + (r.parent ? " · under " + r.parent : "");
        var action = canDispatch(r.agent)
          ? '<button type="button" data-dispatch="' + esc(r.id) + '" style="flex:none;border:1px solid '
            + 'rgba(128,128,128,0.45);border-radius:6px;padding:1px 8px">Assign to agent</button>'
          : "";
        return '<div class="nomad-wp" data-id="' + esc(r.id) + '" data-agent="' + esc(r.agent || "")
          + '" title="' + esc(tip) + '" style="display:flex;gap:8px;align-items:center;padding:4px 0;'
          + 'border-top:1px solid rgba(128,128,128,0.2)">'
          + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
          + "#" + esc(r.id) + " " + esc(r.subject) + ' <span style="opacity:0.6">' + esc(r.type) + "</span></span>"
          + (b ? '<span class="nomad-agent-badge" style="flex:none;opacity:0.9">' + esc(b) + "</span>" : "")
          + action + "</div>";
      }).join("");
      dp.panel.innerHTML = head + note + list;
    }

    function mountDispatch() {
      if (!wrap || !wrap.parentNode) return;
      if (!dp.btn) {
        dp.btn = doc.createElement("button");
        dp.btn.type = "button";
        dp.btn.id = "nomad-dispatch";
        dp.btn.title = "Send an open work package to the agent. Its run stops at your gate.";
        dp.btn.style.cssText = "flex:none;margin-right:8px;font-size:0.875rem;border:1px solid "
          + "rgba(128,128,128,0.35);border-radius:8px;padding:2px 8px;white-space:nowrap";
        dp.btn.textContent = buttonLabel(dp.live);
        dp.btn.addEventListener("click", openPanel);
      }
      if (wrap.nextSibling !== dp.btn) wrap.parentNode.insertBefore(dp.btn, wrap.nextSibling);
    }

    // The element to sit beside: the button's outermost wrapper that holds nothing else.
    function host(anchor) {
      var h = anchor;
      while (h.parentElement && h.parentElement.children.length === 1 && h.parentElement.parentElement
             && h.parentElement !== doc.body) h = h.parentElement;
      return h;
    }

    win.fetch = function (input, init) {
      if (isCompletion(input, init)) {
        try {
          var body = JSON.parse(init.body);
          var next = withProject(body, state);
          if (next) {
            var copy = {};
            for (var k in init) copy[k] = init[k];
            copy.body = JSON.stringify(next);
            init = copy;
            if (body.chat_id) sentFor[body.chat_id] = state.entry;
            state.sent = state.entry;
            apply({ entry: state.entry, locked: true });
          }
        } catch (e) { /* not JSON: send it as it is */ }
      }
      return realFetch.call(win, input, init);
    };

    ["pushState", "replaceState"].forEach(function (name) {
      var orig = win.history[name];
      win.history[name] = function () {
        var r = orig.apply(this, arguments);
        win.setTimeout(refresh, 0);
        return r;
      };
    });
    win.addEventListener("popstate", refresh);

    var queued = false;
    new win.MutationObserver(function () {
      if (queued) return;
      queued = true;
      win.requestAnimationFrame(function () { queued = false; mount(); });
    }).observe(doc.body || doc.documentElement, { childList: true, subtree: true });
    refresh();
    mount();
    if (seen.get().length) { poll(); schedule(); }
    return state;
  }

  // Point every rel=icon link at TAB_ICON. Returns how many it changed; Open WebUI's layout may
  // reset the href, so the bootstrap calls this again whenever <head> changes.
  function tabIcon(doc) {
    var n = 0;
    var links = doc.querySelectorAll('link[rel~="icon"]');
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("href") !== TAB_ICON) {
        links[i].setAttribute("type", "image/png");
        links[i].setAttribute("href", TAB_ICON);
        n++;
      }
    }
    return n;
  }

  var api = { options: options, chatIdOf: chatIdOf, view: view, withProject: withProject,
              isCompletion: isCompletion, start: start, tabIcon: tabIcon, VAR: VAR,
              TAB_ICON: TAB_ICON, rpcBody: rpcBody, rpcResult: rpcResult, badge: badge,
              canDispatch: canDispatch, liveIds: liveIds, buttonLabel: buttonLabel };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (root && root.document && !root.__nomadUI) {
    root.__nomadUI = api;
    tabIcon(root.document);
    new root.MutationObserver(function () { tabIcon(root.document); })
      .observe(root.document.head || root.document.documentElement,
               { childList: true, subtree: true, attributes: true, attributeFilter: ["href"] });
    var go = function () {
      start({ window: root, document: root.document, fetch: root.fetch,
              localStorage: root.localStorage });
    };
    if (root.document.body) go(); else root.document.addEventListener("DOMContentLoaded", go);
  }
})(typeof window !== "undefined" ? window : null);
