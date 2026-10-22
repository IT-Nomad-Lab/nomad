// #1137 · the Project dropdown's logic (owui/nomad_ui.js, docs/prd/owui-sessions.md §6.5).
// Run: node --test owui/test_nomad_ui.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const UI = createRequire(import.meta.url)("./nomad_ui.js");

const MODELS = [
  { id: "balanced", name: "balanced" },
  { id: "nomad.auto", name: "NOMAD Auto" },
  { id: "nomad.p-civis-poc", name: "NOMAD · Civis" },
  { id: "nomad.new-project", name: "+ New project" },
  { id: "nomad.general", name: "General (local)" },
  { id: "nomad.p-notes", name: "NOMAD · Notes" },
];

test("options: the nomad entries but Auto, General first, + New project last, labels bare", () => {
  assert.deepEqual(UI.options(MODELS), [
    { id: "general", label: "General" },
    { id: "p-civis-poc", label: "Civis" },
    { id: "p-notes", label: "Notes" },
    { id: "new-project", label: "+ New project" },
  ]);
  assert.deepEqual(UI.options(null), []);
});

test("chatIdOf reads /c/<id> only", () => {
  assert.equal(UI.chatIdOf("/c/abc-123"), "abc-123");
  assert.equal(UI.chatIdOf("/c/abc?x=1"), "abc");
  assert.equal(UI.chatIdOf("/"), "");
  assert.equal(UI.chatIdOf("/workspace/models"), "");
});

test("view: a new chat is open on the pick, a saved chat is locked to its project", () => {
  assert.deepEqual(UI.view("", null, "", "p-notes"), { entry: "p-notes", locked: false });
  assert.deepEqual(UI.view("", null, "", ""), { entry: "general", locked: false });
  assert.deepEqual(UI.view("", null, "p-notes", "x"), { entry: "p-notes", locked: true });
  assert.deepEqual(UI.view("c1", { variables: { nomad_project: "p-civis-poc" } }, "p-notes", ""),
    { entry: "p-civis-poc", locked: true });          // the chat's record wins over what was sent
  assert.deepEqual(UI.view("c1", null, "p-notes", ""), { entry: "p-notes", locked: true });
  assert.deepEqual(UI.view("c1", { variables: {} }, "", "p-notes"), { entry: "general", locked: true });
});

test("withProject adds the pick to a new chat only, keeping other variables", () => {
  assert.deepEqual(UI.withProject({ model: "m", chat_variables: { a: 1 } },
    { entry: "p-notes", locked: false }),
  { model: "m", chat_variables: { a: 1, nomad_project: "p-notes" } });
  assert.deepEqual(UI.withProject({ model: "m" }, { entry: "general", locked: false }),
    { model: "m", chat_variables: { nomad_project: "general" } });
  assert.equal(UI.withProject({ model: "m" }, { entry: "p-notes", locked: true }), null);
});

test("isCompletion matches the chat completion POST only", () => {
  const post = { method: "POST", body: "{}" };
  assert.ok(UI.isCompletion("/api/chat/completions", post));
  assert.ok(UI.isCompletion("http://h:3000/api/chat/completions", post));
  assert.ok(!UI.isCompletion("/api/chat/completions", { body: "{}" }));
  assert.ok(!UI.isCompletion("/api/chat/completed", post));
  assert.ok(!UI.isCompletion("/api/chat/completions", { method: "POST", body: new Uint8Array() }));
});

function fakeEnv(path, chats = {}) {
  const sent = [];
  const listeners = {};
  const store = { token: "tok" };
  const win = {
    location: { pathname: path },
    history: {
      pushState(_s, _t, url) { win.location.pathname = url; },
      replaceState(_s, _t, url) { win.location.pathname = url; },
    },
    addEventListener(ev, fn) { listeners[ev] = fn; },
    polls: 0,
    setTimeout(fn, ms) { if (ms) win.polls += 1; else fn(); return 1; },   // polls don't run
    clearTimeout() {},
    requestAnimationFrame(fn) { fn(); },
    MutationObserver: class { observe() {} },
  };
  const doc = { body: { contains: () => false }, getElementById: () => null };
  const fetch = async (input, init = {}) => {
    sent.push({ url: input, init });
    const json = input === "/api/models" ? { data: MODELS }
      : input.startsWith("/api/v1/chats/") ? chats[input.slice(14)] || { variables: {} } : {};
    return { ok: true, json: async () => json };
  };
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = v; },
  };
  return { env: { window: win, document: doc, fetch, localStorage }, sent, store, win };
}

const tick = () => new Promise((r) => setImmediate(r));

test("the first message of a new chat carries the pick, then the chat is locked", async () => {
  const { env, sent, store, win } = fakeEnv("/", { c9: { variables: { nomad_project: "p-notes" } } });
  store["nomad.project.pick"] = "p-notes";
  const state = UI.start(env);
  assert.equal(state.entry, "p-notes");
  assert.equal(state.locked, false);

  await win.fetch("/api/chat/completions",
    { method: "POST", body: JSON.stringify({ model: "balanced", chat_id: "c9" }) });
  const body = JSON.parse(sent.at(-1).init.body);
  assert.deepEqual(body.chat_variables, { nomad_project: "p-notes" });
  assert.equal(state.locked, true);

  win.history.replaceState(null, "", "/c/c9");
  await tick();
  assert.deepEqual([state.entry, state.locked, state.chatId], ["p-notes", true, "c9"]);

  await win.fetch("/api/chat/completions",
    { method: "POST", body: JSON.stringify({ model: "balanced", chat_id: "c9" }) });
  assert.equal(JSON.parse(sent.at(-1).init.body).chat_variables, undefined);   // saved chat: untouched
});

test("an opened chat follows its record, including a + New project chat renamed later", async () => {
  const chats = { c1: { variables: { nomad_project: "new-project" } } };
  const { env, win } = fakeEnv("/c/c1", chats);
  const state = UI.start(env);
  await tick();
  assert.deepEqual([state.entry, state.locked], ["new-project", true]);
  assert.ok(win.polls >= 1);                                          // re-reads while unnamed
  chats.c1 = { variables: { nomad_project: "p-civis-poc" } };     // M3 names it; pipe writes back
  win.history.pushState(null, "", "/c/c1");
  await tick();
  assert.deepEqual([state.entry, state.locked], ["p-civis-poc", true]);

  win.history.pushState(null, "", "/");                               // New chat: open again
  await tick();
  assert.equal(state.locked, false);
});

test("a broken localStorage still works", async () => {
  const { env } = fakeEnv("/");
  env.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("x"); } };
  const state = UI.start(env);
  assert.deepEqual([state.entry, state.locked], ["general", false]);
});

test("#1218 tabIcon points every icon link at the small, versioned logo, once", () => {
  const link = (rel, href) => {
    const a = { rel, href, type: "" };
    return { getAttribute: (k) => a[k], setAttribute: (k, v) => { a[k] = v; }, a };
  };
  const links = [link("icon", "/static/favicon.png"), link("shortcut icon", "/static/favicon.ico")];
  const doc = { querySelectorAll: (sel) => (sel === 'link[rel~="icon"]' ? links : []) };
  assert.equal(UI.tabIcon(doc), 2);
  assert.ok(links.every((l) => l.a.href === UI.TAB_ICON && l.a.type === "image/png"));
  assert.match(UI.TAB_ICON, /^\/static\/favicon-96x96\.png\?v=/);
  assert.equal(UI.tabIcon(doc), 0);                                     // idempotent: no observer loop
});

// #1441 · the Dispatch panel's logic.
test("rpcBody is a chat-less completion to NOMAD Auto carrying the call", () => {
  const b = UI.rpcBody("dispatch", "p-notes", { id: 944 });
  assert.equal(b.model, "nomad.auto");
  assert.equal(b.stream, false);
  assert.equal(b.chat_id, undefined);                 // no chat: Open WebUI saves nothing
  assert.deepEqual(b.nomad_rpc, { op: "dispatch", entry: "p-notes", id: 944 });
  assert.equal(UI.rpcBody("items", "").nomad_rpc.entry, "general");
});

test("rpcResult reads the engine's JSON out of the completion, or says what went wrong", () => {
  const ok = { choices: [{ message: { content: '{"ok":true,"items":[]}' } }] };
  assert.deepEqual(UI.rpcResult(ok), { ok: true, items: [] });
  assert.equal(UI.rpcResult({ detail: "401" }).ok, false);
  const text = UI.rpcResult({ choices: [{ message: { content: "⚠ engine down" } }] });
  assert.deepEqual(text, { ok: false, error: "⚠ engine down" });
});

test("a row offers Assign only when nothing is running for it", () => {
  for (const a of ["", undefined, "declined", "failed", "withdrawn"]) assert.ok(UI.canDispatch(a), a);
  for (const a of ["assigned", "at-gate", "building", "approved", "done"]) assert.ok(!UI.canDispatch(a), a);
});

test("badges, live ids and the button count follow the agent state", () => {
  assert.equal(UI.badge("at-gate"), "⏸ at gate");
  assert.equal(UI.badge(""), "");
  const rows = [{ id: 1, agent: "at-gate" }, { id: 2, agent: "building" }, { id: 3, agent: "done" },
    { id: 4, agent: "" }, { id: 5, agent: "at-gate" }];
  assert.deepEqual(UI.liveIds(rows), [1, 2, 5]);
  assert.equal(UI.buttonLabel(rows), "Dispatch · 2 at gate");
  assert.equal(UI.buttonLabel([]), "Dispatch");
});
