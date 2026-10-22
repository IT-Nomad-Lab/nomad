"""#1118, #1135, #1136 · the Open WebUI pipe, the project filter and the sync script
(docs/prd/owui-sessions.md §6.1, §6.5, N6).

Run: uv run -q --no-project --with pytest --with aiohttp --with pydantic \
         python -m pytest owui -q -p no:cacheprovider
"""
import asyncio
import base64
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("nomad_pipe", HERE / "nomad_pipe.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
spec = importlib.util.spec_from_file_location("nomad_filter", HERE / "nomad_filter.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
spec = importlib.util.spec_from_file_location("owui_sync", HERE.parent / "scripts" / "owui-sync-pipe.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

TOKEN = "pipe-test-token"


class Engine(BaseHTTPRequestHandler):
    seen: list = []
    models_calls = 0
    bound: dict = {}                # chat_id -> entry, the engine's lock
    context_status = 200
    override = None                 # what the fake engine answers for an override request

    def log_message(self, *a):
        pass

    def _auth(self):
        if self.headers.get("X-Nomad-Token") != TOKEN:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        Engine.models_calls += 1
        body = json.dumps({"models": [{"id": "p-civis-poc", "name": "NOMAD · Civis"},
                                      {"id": "general", "name": "General (local)"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._auth():
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Engine.seen.append(req)
        if self.path == "/owui/dispatch":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "path": self.path, "got": req}).encode())
            return
        entry = Engine.bound.setdefault(req.get("chat_id"), req.get("entry"))
        if self.path == "/owui/context":
            self.send_response(Engine.context_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            p = entry.startswith("p-")
            self.wfile.write(json.dumps({"entry": entry, "name": entry.upper(),
                                         "system": "CTX " + entry if p else "",
                                         "note": "" if entry == req["entry"]
                                         else f"this chat stays on {entry}",
                                         **({"override": Engine.override}
                                            if req.get("messages") and Engine.override else {})}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for e in ({"type": "status", "text": "routing…"}, {"type": "delta", "text": "hel"},
                  {"type": "delta", "text": "lo"}, {"type": "done", "routed": "fast", "served": "fast", "entry": entry}):
            self.wfile.write((json.dumps(e) + "\n").encode())
            self.wfile.flush()


@pytest.fixture(scope="module")
def engine():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def pipe(url, token=TOKEN):
    p = P.Pipe()
    p.valves = p.Valves(ENGINE_URL=url, NOMAD_TOKEN=token)
    return p


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def saved(monkeypatch):
    out = []

    async def save(chat_id, variables, entry):
        out.append((chat_id, {**variables, "nomad_project": entry}))
    monkeypatch.setattr(P, "save_project", save)
    monkeypatch.setattr(F, "save_project", save)
    return out


def test_pipes_lists_nomad_auto_then_the_engine_entries_and_caches_them(engine):
    p, before = pipe(engine), Engine.models_calls
    rows = run(p.pipes())
    assert rows == [{"id": "auto", "name": "NOMAD Auto"},
                    {"id": "local", "name": "NOMAD Local"},
                    {"id": "p-civis-poc", "name": "NOMAD · Civis"},
                    {"id": "general", "name": "General (local)"}]
    run(p.pipes())
    assert Engine.models_calls == before + 1


def test_pipes_with_the_engine_down_still_offers_auto_and_general():
    assert run(pipe("http://127.0.0.1:9").pipes()) == [{"id": "auto", "name": "NOMAD Auto"},
                                                       {"id": "local", "name": "NOMAD Local"},
                                                       {"id": "general", "name": "General"}]


def _stream(p, body, **kw):
    async def go():
        gen = await p.pipe({**body, "stream": True}, **kw)
        return "".join([t async for t in gen])
    return run(go())


def test_nomad_auto_answers_for_the_chat_variable_project(engine, saved):
    out = _stream(pipe(engine), {"model": "nomad.auto", "messages": []},
                  __metadata__={"chat_id": "auto1", "chat_variables": {"nomad_project": "p-notes"}})
    assert out == "hello" and Engine.seen[-1]["entry"] == "p-notes"
    assert saved == []                              # already on the bound entry: no write


def test_nomad_auto_without_a_project_is_general(engine, saved):
    _stream(pipe(engine), {"model": "nomad.auto", "messages": []}, __metadata__={"chat_id": "auto2"})
    assert Engine.seen[-1]["entry"] == "general"


def test_the_pipe_writes_the_bound_entry_back_to_the_chat(engine, saved):
    Engine.bound["auto3"] = "p-civis-poc"           # bound on an earlier turn
    _stream(pipe(engine), {"model": "nomad.auto", "messages": []},
            __metadata__={"chat_id": "auto3", "chat_variables": {"nomad_project": "p-notes", "x": 1}})
    assert saved == [("auto3", {"nomad_project": "p-civis-poc", "x": 1})]


def test_a_task_never_writes_the_chat_variable(engine, saved):
    Engine.bound["auto4"] = "p-civis-poc"
    run(pipe(engine).pipe({"model": "nomad.auto", "stream": False, "messages": []},
                          __task__="title_generation", __chat_id__="auto4",
                          __metadata__={"chat_variables": {"nomad_project": "general"}}))
    assert saved == []


def test_a_streamed_turn_forwards_the_entry_and_emits_status(engine):
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        gen = await pipe(engine).pipe(
            {"model": "nomad.p-civis-poc", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]},
            __metadata__={"chat_id": "c9"}, __event_emitter__=emit)
        return "".join([t async for t in gen])
    assert run(go()) == "hello"
    assert Engine.seen[-1] == {"entry": "p-civis-poc", "chat_id": "c9", "task": None, "files": [],
                               "messages": [{"role": "user", "content": "hi"}]}
    assert events[0]["data"] == {"description": "routing…", "done": False}
    assert events[-1]["data"] == {"description": "served by fast", "done": True}


def test_a_task_call_returns_one_string(engine):
    out = run(pipe(engine).pipe({"model": "nomad.general", "stream": False, "messages": []},
                                __task__="title_generation", __chat_id__="c1"))
    assert out == "hello" and Engine.seen[-1]["task"] == "title_generation"


def test_n6_engine_down_names_the_engine():
    async def go():
        gen = await pipe("http://127.0.0.1:9").pipe({"model": "nomad.general", "stream": True,
                                                     "messages": []})
        return "".join([t async for t in gen])
    out = run(go())
    assert "nomad-v2-engine" in out and "can't answer" in out and "plain model" in out


def test_a_wrong_token_says_which_valve(engine):
    out = run(pipe(engine, token="nope").pipe({"model": "nomad.general", "messages": []}))
    assert "NOMAD_TOKEN valve" in out and "HTTP 401" in out


def flt(url, token=TOKEN):
    f = F.Filter()
    f.valves = f.Valves(ENGINE_URL=url, NOMAD_TOKEN=token)
    return f


def _inlet(f, body, variables, chat_id="f1", model="balanced", task=None):
    events = []

    async def emit(e):
        events.append(e)
    out = run(f.inlet(body, __metadata__={"chat_id": chat_id, "chat_variables": variables,
                                          "task": task},
                      __model__={"id": model}, __event_emitter__=emit))
    return out, [e["data"]["description"] for e in events]


def test_the_filter_prepends_the_project_context_for_any_model(engine, saved):
    body = {"model": "balanced", "messages": [{"role": "user", "content": "hi"}]}
    out, status = _inlet(flt(engine), body, {"nomad_project": "p-notes"}, chat_id="f1")
    assert out["messages"][0] == {"role": "system", "content": "CTX p-notes"}
    assert out["messages"][1:] == body["messages"]
    assert Engine.seen[-1] == {"chat_id": "f1", "entry": "p-notes", "model": "balanced",
                               "messages": []}
    assert status == ["project: P-NOTES"] and saved == []


def test_the_filter_joins_an_existing_system_prompt(engine, saved):
    body = {"messages": [{"role": "system", "content": "be brief"},
                         {"role": "user", "content": "hi"}]}
    out, _ = _inlet(flt(engine), body, {"nomad_project": "p-notes"}, chat_id="f2")
    assert out["messages"][0]["content"] == "CTX p-notes\n\nbe brief" and len(out["messages"]) == 2


def test_the_filter_keeps_the_bound_project_and_writes_it_back(engine, saved):
    Engine.bound["f3"] = "p-civis-poc"
    out, status = _inlet(flt(engine), {"messages": []}, {"nomad_project": "p-notes"}, chat_id="f3")
    assert out["messages"][0]["content"] == "CTX p-civis-poc"
    assert saved == [("f3", {"nomad_project": "p-civis-poc"})]
    assert status == ["project: P-CIVIS-POC · this chat stays on p-civis-poc"]


@pytest.mark.parametrize("variables,model,task", [
    ({}, "balanced", None),                                 # General: nothing to add
    ({"nomad_project": "general"}, "balanced", None),
    ({"nomad_project": "p-notes"}, "nomad.auto", None),     # NOMAD's own models do it themselves
    ({"nomad_project": "p-notes"}, "balanced", "title_generation"),
])
def test_the_filter_leaves_general_nomad_models_and_tasks_alone(engine, saved, variables, model,
                                                                task):
    before = len(Engine.seen)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out, status = _inlet(flt(engine), body, variables, chat_id="f4", model=model, task=task)
    assert out == body and status == [] and len(Engine.seen) == before


def test_the_filter_with_the_engine_down_answers_without_context_and_says_so(saved):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out, status = _inlet(flt("http://127.0.0.1:9"), body, {"nomad_project": "p-notes"})
    assert out == body and status[0].startswith("⚠ NOMAD project context unavailable")


def test_the_filter_with_a_wrong_token_answers_without_context(engine, saved):
    out, status = _inlet(flt(engine, token="nope"), {"messages": []}, {"nomad_project": "p-notes"})
    assert out == {"messages": []} and "unavailable" in status[0]


def test_both_functions_are_pinned_and_the_sync_plan_follows_the_installed_state():
    fm = S.front_matter((HERE / "nomad_pipe.py").read_text())
    assert fm["title"] == "NOMAD" and fm["required_open_webui_version"] == "0.12.3"
    assert fm["version"]
    ff = S.front_matter((HERE / "nomad_filter.py").read_text())
    assert ff["required_open_webui_version"] == "0.12.3" and ff["version"]
    assert [(fid, path.name, g) for fid, path, g in S.FUNCTIONS] == [
        ("nomad", "nomad_pipe.py", False), ("nomad_project", "nomad_filter.py", True)]
    assert S.plan(None, "x") == ["create", "activate", "valves"]
    assert S.plan(None, "x", True) == ["create", "activate", "global", "valves"]
    assert S.plan({"content": "x", "is_active": True}, "x") == ["valves"]
    assert S.plan({"content": "x", "is_active": True}, "x", True) == ["global", "valves"]
    assert S.plan({"content": "old", "is_active": False}, "x") == ["update", "activate", "valves"]


def test_the_sync_hides_every_nomad_entry_but_auto():
    listed = [{"id": "nomad.auto", "name": "NOMAD Auto"}, {"id": "nomad.general", "name": "General"},
              {"id": "nomad.p-notes", "name": "NOMAD · Notes"}, {"id": "balanced", "name": "b"},
              {"id": "nomad.p-old", "name": "Old"}]
    records = [{"id": "nomad.p-old", "name": "Old", "base_model_id": None, "params": {},
                "is_active": True, "meta": {"hidden": True}},
               {"id": "nomad.auto", "name": "NOMAD Auto", "base_model_id": None, "params": {},
                "is_active": True, "meta": {"hidden": True, "description": "d"}}]
    got = S.hide_plan(listed, records)
    assert got == [
        ("update", {"id": "nomad.auto", "name": "NOMAD Auto", "base_model_id": None, "params": {},
                    "is_active": True, "meta": {"hidden": False, "description": "d"}}),
        ("create", {"id": "nomad.general", "name": "General", "base_model_id": None,
                    "meta": {"hidden": True}, "params": {}}),
        ("create", {"id": "nomad.p-notes", "name": "NOMAD · Notes", "base_model_id": None,
                    "meta": {"hidden": True}, "params": {}})]
    done = records[:1] + [{**f, "is_active": True} for _, f in got]
    assert S.hide_plan(listed, done) == []


def test_the_sync_script_never_prints_secrets(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(S, "CONFIG", tmp_path)
    (tmp_path / "owui-admin-key").write_text("sk-admin-secret\n")
    monkeypatch.delenv("OWUI_ADMIN_KEY", raising=False)
    assert S.secret("OWUI_ADMIN_KEY", "owui-admin-key") == "sk-admin-secret"
    assert "secret" not in capsys.readouterr().out
    src = (HERE.parent / "scripts" / "owui-sync-pipe.py").read_text()
    assert "print(key" not in src and "print(token" not in src and os.path.basename(src) != ""


def _override_body(text="/deep why?"):
    return {"model": "codex", "messages": [{"role": "user", "content": "earlier"},
                                           {"role": "assistant", "content": "a"},
                                           {"role": "user", "content": text}]}


def test_the_filter_switches_the_model_and_strips_the_prefix(engine, saved):
    Engine.override = {"model": "deep", "cmd": "/deep", "text": "why?", "refused": False, "note": ""}
    f = flt(engine)
    out, _ = _inlet(f, _override_body(), {"nomad_project": "general"}, chat_id="o1", model="codex")
    assert out["model"] == "deep" and out["messages"][-1] == {"role": "user", "content": "why?"}
    assert out["messages"][0]["content"] == "earlier"
    sent = Engine.seen[-1]
    assert sent["entry"] == "general" and [m["content"] for m in sent["messages"]] == ["earlier", "/deep why?"]
    done = run(f.outlet({"messages": [{"role": "user", "content": "/deep why?"},
                                      {"role": "assistant", "content": "because"}]},
                        __metadata__={"chat_id": "o1"}))
    assert done["messages"][-1]["content"] == "because\n\n---\noverride: deep (/deep)"
    again = run(f.outlet(done, __metadata__={"chat_id": "o1"}))
    assert again == done


def test_the_filter_keeps_multimodal_parts_when_it_strips_the_prefix(engine, saved):
    Engine.override = {"model": "vision", "cmd": "!vision", "text": "what is this", "refused": False,
                       "note": ""}
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "!vision what is this"},
        {"type": "image_url", "image_url": {"url": "x"}}]}]}
    out, _ = _inlet(flt(engine), body, {}, chat_id="o2")
    assert out["model"] == "vision"
    assert out["messages"][0]["content"] == [{"type": "text", "text": "what is this"},
                                             {"type": "image_url", "image_url": {"url": "x"}}]


def test_a_refused_override_goes_to_private_and_the_reply_says_so(engine, saved):
    note = "/deep is a cloud model; this chat stays local"
    Engine.override = {"model": "private", "cmd": "/deep", "text": "why?", "refused": True, "note": note}
    f = flt(engine)
    out, status = _inlet(f, _override_body(), {"nomad_project": "p-notes"}, chat_id="o3")
    assert out["model"] == "private" and status[-1].endswith(note)
    done = run(f.outlet({"messages": [{"role": "assistant", "content": "ok"}]},
                        __metadata__={"chat_id": "o3"}))
    assert done["messages"][-1]["content"].endswith(f"override: private (/deep) · {note}")


def test_a_bare_override_with_nothing_to_re_ask_leaves_the_model(engine, saved):
    Engine.override = {"model": "deep", "cmd": "!deep", "text": "", "refused": False, "note": ""}
    body = {"model": "codex", "messages": [{"role": "user", "content": "!deep"}]}
    out, _ = _inlet(flt(engine), body, {"nomad_project": "general"}, chat_id="o4", model="codex")
    assert out["model"] == "codex" and out["messages"] == body["messages"]


def test_the_outlet_adds_nothing_without_an_override(engine):
    body = {"messages": [{"role": "assistant", "content": "hi"}]}
    assert run(flt(engine).outlet(body, __metadata__={"chat_id": "none"})) == body


@pytest.fixture(autouse=True)
def _no_override():
    Engine.override = None


# ── NOMAD Agent entry (agent-bridge/), behind the AGENT_ENABLED valve ───────────────────────────
class Agent(BaseHTTPRequestHandler):
    seen: list = []
    gate = True
    files: list = []                # file events the fake bridge sends after the text (#1332)

    def log_message(self, *a):
        pass

    state = {"session": False, "pending": 0, "eligible": True}
    asks: list = []                 # what GET /agent/pending lists (#1362)
    gate_extra: dict = {}           # merged into the gate event
    attach: list = []               # what GET /agent/attach streams (#1406)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.path.startswith("/agent/pending"):
            st = (parse_qs(urlparse(self.path).query).get("state") or [""])[0]
            rows = [a for a in Agent.asks if not st or a["state"] == st]
            self.wfile.write(json.dumps({"pending": rows}).encode())
            return
        if self.path.startswith("/agent/attach"):          # the rest of a run (#1406)
            Agent.seen.append((self.path, None))
            for e in Agent.attach:
                self.wfile.write((json.dumps(e) + "\n").encode())
            return
        self.wfile.write(json.dumps(Agent.state).encode())

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Agent.seen.append((self.path, req))
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path == "/agent/decide"
                         else "application/x-ndjson")
        self.end_headers()
        if self.path == "/agent/decide":
            self.wfile.write(json.dumps({"ok": True}).encode())
            return
        evs = [{"type": "delta", "text": "working\n"}]
        if Agent.gate and "decision" not in req:
            evs.append({"type": "gate", "run_id": "ab12cd34", "kind": "permission", "tool": "Bash",
                        "summary": "git push", "reason": "", **Agent.gate_extra})
        evs.extend(Agent.files)
        evs.append({"type": "delta", "text": "after"})
        evs.append({"type": "done", "run_id": "ab12cd34", "status": "done"})
        try:
            for e in evs:
                self.wfile.write((json.dumps(e) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):   # the pipe hangs up after a gate
            pass


@pytest.fixture
def agent_url():
    Agent.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def agent_pipe(url, enabled=True):
    p = P.Pipe()
    p.valves = p.Valves(NOMAD_TOKEN=TOKEN, AGENT_ENABLED=enabled, AGENT_URL=url)
    return p


def test_agent_flag_off_falls_back_to_the_engine_path(engine):
    Engine.seen = []
    out = run(pipe(engine).pipe({"model": "nomad.agent", "stream": False,
                                  "messages": [{"role": "user", "content": "hi"}]},
                                 __metadata__={}, __chat_id__="c9"))
    assert out == "hello" and Engine.seen[-1]["entry"] == "general"


def test_agent_gate_without_a_card_says_how_to_answer(agent_url):
    p = agent_pipe(agent_url)

    async def go():
        gen = await p.pipe({"model": "nomad.agent", "stream": True,
                            "messages": [{"role": "user", "content": "push"}]},
                           __metadata__={"chat_variables": {"nomad_project": "p-civis-poc"}},
                           __chat_id__="c1")
        return "".join([t async for t in gen])
    out = run(go())
    assert Agent.seen[0][1]["project"] == "p-civis-poc"
    assert "working" in out and "approve ab12cd34" in out and "after" not in out
    assert not any(path == "/agent/decide" for path, _ in Agent.seen)


def auto(p, text, chat="c7", variables=None):
    async def go():
        out = await p.pipe({"model": "nomad.auto", "stream": False,
                            "messages": [{"role": "user", "content": text}]},
                           __metadata__={"chat_variables": variables or {}}, __chat_id__=chat)
        return out
    return run(go())


def test_auto_hands_work_requests_to_the_agent_and_questions_to_the_engine(agent_url, engine):
    Agent.state = {"session": False, "pending": 0, "eligible": True}
    Agent.gate = False
    p = agent_pipe(agent_url)
    p.valves.ENGINE_URL = engine
    Engine.seen = []
    assert "working" in auto(p, "fix the bug in the parser file and run the tests")
    assert Agent.seen[-1][0] == "/agent/chat"
    Agent.seen = []
    assert auto(p, "what is a gate in nomad?") == "hello" and not Agent.seen
    Agent.gate = True


def test_auto_stays_with_the_agent_once_a_session_exists_and_obeys_bang_chat(agent_url, engine):
    Agent.state = {"session": True, "pending": 0, "eligible": True}
    Agent.gate = False
    p = agent_pipe(agent_url)
    p.valves.ENGINE_URL = engine
    assert "working" in auto(p, "and now?")
    Agent.seen = []
    assert auto(p, "!chat what time is it") == "hello" and not Agent.seen
    assert Engine.seen[-1]["messages"][-1]["content"] == "what time is it"
    Agent.gate = True


def test_auto_keeps_local_only_and_flag_off_on_the_engine(agent_url, engine):
    Agent.state = {"session": True, "pending": 0, "eligible": False}
    p = agent_pipe(agent_url)
    p.valves.ENGINE_URL = engine
    assert auto(p, "edit the file") == "hello"
    Agent.state = {"session": True, "pending": 0, "eligible": True}
    off = agent_pipe(agent_url, enabled=False)
    off.valves.ENGINE_URL = engine
    assert auto(off, "edit the file") == "hello"


def test_auto_sends_a_named_backend_to_the_agent_and_shows_who_ran_it(agent_url, engine):
    Agent.state = {"session": False, "pending": 0, "eligible": True}
    Agent.gate = False
    p = agent_pipe(agent_url)
    p.valves.ENGINE_URL = engine
    assert "working" in auto(p, "!codex what changed?")
    assert Agent.seen[-1][1]["message"].startswith("!codex")
    Agent.gate = True


def _local(p, text, chat="c9", variables=None, task=None):
    async def go():
        return await p.pipe({"model": "nomad.local", "stream": False,
                             "messages": [{"role": "user", "content": text}]},
                            __metadata__={"chat_variables": variables or {}}, __chat_id__=chat,
                            __task__=task)
    return run(go())


@pytest.fixture
def local(agent_url, engine):
    Agent.gate = False
    Agent.seen, Engine.seen = [], []
    p = agent_pipe(agent_url)
    p.valves.ENGINE_URL = engine
    before = Agent.state
    yield p
    Agent.gate, Agent.state = True, before


def test_nomad_local_talk_goes_to_the_engine_local_only(local):          # #1436
    Agent.state = {"session": False, "pending": 0, "eligible": True, "local_up": False}
    assert _local(local, "what is a gate in nomad?", variables={"nomad_project": "p-civis-poc"}) == "hello"
    assert not Agent.seen
    req = Engine.seen[-1]
    assert (req["entry"], req["local_only"], req["local_loaded"]) == ("p-civis-poc", True, "")


def test_nomad_local_talk_stays_on_code_while_it_is_loaded(local):
    Agent.state = {"session": False, "pending": 0, "eligible": True, "local_up": True}
    _local(local, "thanks, and why?")
    assert Engine.seen[-1]["local_loaded"] == "code" and Engine.seen[-1]["entry"] == "general"


def test_nomad_local_work_goes_to_the_agent_with_the_local_backend(local):
    Agent.state = {"session": False, "pending": 0, "eligible": True, "local_up": False}
    assert "working" in _local(local, "fix the failing test in the bridge")
    path, payload = Agent.seen[-1][0], Agent.seen[-1][1]
    assert path == "/agent/chat" and payload["backend"] == "local"
    assert "working" in _local(local, "!agent what is a gate?")
    assert Agent.seen[-1][1]["backend"] == "local"


def test_nomad_local_is_not_sticky_but_a_waiting_gate_is(local):
    Agent.state = {"session": True, "pending": 0, "eligible": True, "local_up": False}
    assert _local(local, "what did that change?") == "hello" and not Agent.seen
    Agent.state = {"session": True, "pending": 1, "eligible": True, "local_up": False}
    assert "working" in _local(local, "yes go on")


def test_nomad_local_chat_forces_the_engine(local):
    Agent.state = {"session": False, "pending": 0, "eligible": True, "local_up": False}
    assert _local(local, "!chat edit the file please") == "hello" and not Agent.seen
    assert Engine.seen[-1]["messages"][-1]["content"] == "edit the file please"


def test_nomad_local_on_a_project_the_agent_cant_take_stays_on_the_engine_local_only(local):
    Agent.state = {"session": False, "pending": 0, "eligible": False, "local_up": False}
    assert _local(local, "edit the file") == "hello" and not Agent.seen
    assert Engine.seen[-1]["local_only"] is True


def test_nomad_local_tasks_go_to_the_engine_local_only(local):
    Agent.state = {"session": False, "pending": 0, "eligible": True, "local_up": False}
    _local(local, "fix the bug", task="title_generation")
    assert not Agent.seen and Engine.seen[-1]["local_only"] is True and Engine.seen[-1]["task"]


# ── files in the chat (#1332, docs/prd/owui-inline-files.md) ───────────────────────────────────
import base64  # noqa: E402


def _file_ev(name, mime, data=b"data", path="/home/x/out/"):
    return {"type": "file", "name": name, "mime": mime, "size": len(data), "path": path + name,
            "b64": base64.b64encode(data).decode()}


def _files_turn(url, events, enabled=True, upload=None, user=None, request="req"):
    Agent.gate = False
    Agent.files = events
    p = agent_pipe(url)
    p.valves = p.Valves(NOMAD_TOKEN=TOKEN, AGENT_ENABLED=True, AGENT_URL=url, FILES_ENABLED=enabled)
    uploads, emitted = [], []

    async def fake_upload(req, usr, name, mime, data):
        uploads.append((req, usr, name, mime, data))
        if upload == "fail":
            raise RuntimeError("disk full")
        return f"id{len(uploads)}"

    async def emit(ev):
        emitted.append(ev)
    p._upload = fake_upload

    async def go():
        gen = await p.pipe({"model": "nomad.agent", "stream": True,
                            "messages": [{"role": "user", "content": "make a file"}]},
                           __metadata__={}, __chat_id__="cf", __event_emitter__=emit,
                           __request__=request, __user__=user or {"id": "u1"})
        return "".join([t async for t in gen])
    try:
        return run(go()), uploads, emitted
    finally:
        Agent.gate, Agent.files = True, []


def test_files_valve_off_ignores_file_events(agent_url):
    out, uploads, emitted = _files_turn(agent_url, [_file_ev("a.png", "image/png")], enabled=False)
    assert uploads == [] and "📎" not in out and "working" in out and "after" in out
    assert not any(e["type"] == "embeds" for e in emitted)


def test_an_image_is_uploaded_as_the_chat_user_and_shown_inline(agent_url):
    out, uploads, _ = _files_turn(agent_url, [_file_ev("chart.png", "image/png", b"PNGDATA")])
    assert uploads == [("req", {"id": "u1"}, "chart.png", "image/png", b"PNGDATA")]
    assert "![chart.png](/api/v1/files/id1/content)" in out
    assert "[chart.png](/api/v1/files/id1/content)" in out         # and a link, always


def test_a_pdf_becomes_one_embed_sent_when_the_run_is_done(agent_url):
    out, _, emitted = _files_turn(agent_url, [_file_ev("r.pdf", "application/pdf"),
                                              _file_ev("s.mp3", "audio/mpeg")])
    embeds = [e for e in emitted if e["type"] == "embeds"]
    assert len(embeds) == 1 and len(embeds[0]["data"]["embeds"]) == 2
    assert "pdf.min.js" in embeds[0]["data"]["embeds"][0] and "<audio" in embeds[0]["data"]["embeds"][1]
    assert emitted.index(embeds[0]) < emitted.index(next(e for e in emitted if e["type"] == "status"
                                                         and e["data"].get("done")))
    assert "iframe" not in out                                      # the answer text has links only


def test_a_file_event_over_aiohttps_line_limit_still_arrives(agent_url):
    # a 1.3 MB PNG broke the run: "Got more than 524288 bytes when reading" (live test 2026-10-16)
    big = os.urandom(2 * 1024 * 1024)
    out, uploads, _ = _files_turn(agent_url, [_file_ev("vase.png", "image/png", big)])
    assert uploads and uploads[0][4] == big
    assert "![vase.png](/api/v1/files/id1/content)" in out and "after" in out


def test_ndjson_splits_lines_across_chunks_and_keeps_a_last_line_without_newline():
    class Body:
        async def iter_any(self):
            for c in (b'{"a":1}\n{"b":', b'2}\n\nnot json\n', b'{"c":3}'):
                yield c

    async def run():
        return [e async for e in P._ndjson(Body())]
    assert asyncio.run(run()) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_media_and_pdf_embeds_carry_their_bytes_not_a_files_url():
    # an embed is an opaque-origin frame: /api/v1/files from it is 401 (live test 2026-10-16)
    for name, mime, tag in (("g.mp3", "audio/mpeg", "<audio"), ("h.mp4", "video/mp4", "<video"),
                            ("f.pdf", "application/pdf", "pdf.min.js")):
        md, emb = P.render_file(name, mime, 4, "/api/v1/files/x/content", b"DATA")
        assert tag in emb and base64.b64encode(b"DATA").decode() in emb
        assert "/api/v1/files" not in emb and "/api/v1/files/x/content" in md       # link still there
    assert "data:audio/mpeg;base64," in P.render_file("g.mp3", "audio/mpeg", 4, "/f", b"DATA")[1]


def test_media_over_the_embed_limit_keeps_the_link_and_says_why():
    big = b"x" * (P.EMBED_MAX_BYTES + 1)
    md, emb = P.render_file("long.mp4", "video/mp4", len(big), "/f", big)
    assert emb == "" and "[long.mp4](/f)" in md and "too big to play here" in md
    md, emb = P.render_file("big.pdf", "application/pdf", len(big), "/f", big)
    assert emb == "" and "too big to view here" in md


def test_svg_is_only_ever_an_image_never_an_embed(agent_url):
    out, _, emitted = _files_turn(agent_url, [_file_ev("v.svg", "image/svg+xml", b"<svg><script/></svg>")])
    assert "![v.svg](/api/v1/files/id1/content)" in out
    assert not any(e["type"] == "embeds" for e in emitted)
    md, emb = P.render_file("v.svg", "image/svg+xml", 10, "/f", b"<svg onload=x>")
    assert emb == "" and "<svg" not in md


def test_csv_gets_a_table_preview_and_office_files_a_download_link(agent_url):
    out, _, _ = _files_turn(agent_url, [_file_ev("t.csv", "text/csv", b"a,b\n1,2\n"),
                                        _file_ev("d.docx", "application/vnd.openxmlformats-officedocument."
                                                 "wordprocessingml.document")])
    assert "| a | b |" in out and "| 1 | 2 |" in out
    assert "[d.docx](/api/v1/files/id2/content)" in out and "![d.docx]" not in out


def test_an_upload_failure_says_where_the_file_is_and_the_turn_goes_on(agent_url):
    out, _, emitted = _files_turn(agent_url, [_file_ev("a.png", "image/png")], upload="fail")
    assert "Couldn't show `a.png`" in out and "/home/x/out/a.png" in out and "disk full" in out
    assert "after" in out and any(e["type"] == "status" for e in emitted)


def test_no_chat_user_is_a_visible_failure_not_a_silent_drop(agent_url):
    out, uploads, _ = _files_turn(agent_url, [_file_ev("a.png", "image/png")], request=None)
    assert uploads == [] and "Couldn't show `a.png`" in out


def test_names_cannot_break_out_of_the_markdown(agent_url):
    out, uploads, _ = _files_turn(agent_url, [_file_ev("a](http://evil)<b>.png", "image/png")])
    assert "http://evil" not in out and "<b>" not in out and uploads[0][2] == "a__http___evil__b_.png"


# ── files the user drops into the chat (#1333) ─────────────────────────────────────────────────
def _uploads_turn(url, files, reader, enabled=True):
    Agent.gate = False
    p = agent_pipe(url)
    p.valves = p.Valves(NOMAD_TOKEN=TOKEN, AGENT_ENABLED=True, AGENT_URL=url, FILES_ENABLED=enabled)
    calls = []

    async def fake_read(f, user):
        calls.append((f, user))
        return reader

    p._read_uploads = fake_read
    Agent.seen.clear()

    async def go():
        gen = await p.pipe({"model": "nomad.agent", "stream": True, "files": files,
                            "messages": [{"role": "user", "content": "read it"}]},
                           __metadata__={}, __chat_id__="cu", __event_emitter__=None,
                           __request__="req", __user__={"id": "u1"})
        return "".join([t async for t in gen])
    try:
        run(go())
    finally:
        Agent.gate = True
    return calls


def _chat_payload():
    return next(b for path, b in Agent.seen if path == "/agent/chat")


def test_uploads_go_to_the_bridge_as_attachments(agent_url):
    att = [{"id": "f1", "name": "a.txt", "b64": "aGk="}]
    calls = _uploads_turn(agent_url, [{"id": "f1"}], att)
    assert calls and calls[0][1] == {"id": "u1"}
    assert _chat_payload()["attachments"] == att


def test_uploads_are_ignored_with_the_valve_off(agent_url):
    calls = _uploads_turn(agent_url, [{"id": "f1"}], [{"id": "f1", "name": "a", "b64": "aGk="}], enabled=False)
    assert calls == [] and "attachments" not in _chat_payload()


def test_no_uploads_no_attachments_key(agent_url):
    _uploads_turn(agent_url, [], [])
    assert "attachments" not in _chat_payload()


# --- #1399: requests that make images or documents go to the agent ---------------------------------
@pytest.mark.parametrize("text", [
    "Create an image of a blue vase, realistic", "write a pdf report", "make me a docx",
    "Generate a picture of a cat", "can you draw a diagram of the pipeline", "produce an xlsx with the totals",
    "make a chart of sales by month", "Create a photo of a sunset", "render an image of the logo"])
def test_make_requests_for_images_and_documents_are_work(text):
    assert P.is_work(text), text


@pytest.mark.parametrize("text", [
    "what is a pdf", "how do I make a chart in excel", "explain the difference between docx and pdf",
    "show me the images", "the image was generated yesterday", "is this picture a photo of a vase",
    "who made the diagram", "hello", "summarise this pdf"])
def test_questions_about_images_and_documents_are_not_work(text):
    assert not P.is_work(text), text


def test_auto_sends_an_image_request_to_the_agent_only_when_the_agent_is_on(agent_url, engine):
    Agent.state = {"session": False, "pending": 0, "eligible": True}
    Agent.gate = False
    p = agent_pipe(agent_url)
    p.valves.ENGINE_URL = engine
    Agent.seen = []
    assert "working" in auto(p, "Create an image of a blue vase, realistic")
    assert Agent.seen[-1][0] == "/agent/chat"
    off = agent_pipe(agent_url, enabled=False)
    off.valves.ENGINE_URL = engine
    Agent.seen = []
    assert auto(off, "Create an image of a blue vase, realistic") == "hello" and not Agent.seen
    Agent.gate = True


# --- #1362: inline asks ----------------------------------------------------------------------------
QASK = {"ask_id": "ask-0123456789", "kind": "question", "questions": [
    {"header": "DB", "question": "Which database?", "multiSelect": False,
     "options": [{"label": "Postgres", "description": ""}, {"label": "SQLite", "description": ""}]},
    {"header": "Extras", "question": "Which extras?", "multiSelect": True,
     "options": [{"label": "Auth", "description": ""}, {"label": "Cache", "description": ""},
                 {"label": "Queue", "description": ""}]}]}


def embedded(card: str) -> dict:
    import re as _re
    return json.loads(_re.search(r'<script id=ask type=application/json>(.*?)</script>', card, _re.S).group(1))


def inline_pipe(url):
    return agent_pipe(url)


def drive(p, text="push", chat="c1"):
    events = []

    async def emit(ev):
        events.append(ev)

    async def go():
        gen = await p.pipe({"model": "nomad.agent", "stream": True,
                            "messages": [{"role": "user", "content": text}]},
                           __metadata__={}, __chat_id__=chat, __event_emitter__=emit)
        return "".join([t async for t in gen])
    return run(go()), events


def last_card(events) -> dict:
    return embedded(next(e for e in events if e["type"] == "embeds")["data"]["embeds"][-1])


def test_the_valve_and_the_popup_are_gone():
    assert not hasattr(P.Pipe.Valves(), "INLINE_ASKS") and not hasattr(P.Pipe, "_gate")
    assert P.build_card is not None
    assert "__event_call__" not in P.Pipe.pipe.__code__.co_varnames


def test_inline_permission_gate_is_a_card(agent_url):
    Agent.gate_extra = {"ask_id": "ask-0123456789"}
    Agent.seen = []
    out, events = drive(inline_pipe(agent_url))
    Agent.gate_extra = {}
    d = last_card(events)
    assert d["id"] == "ask-0123456789" and d["kind"] == "approve" and d["tool"] == "Bash"
    assert "answered" not in d and "after" not in out and "approve" in out
    assert not any(path == "/agent/decide" for path, _ in Agent.seen)


def test_inline_question_gate_shows_the_questions_and_numbered_options(agent_url):
    Agent.gate_extra = QASK
    out, events = drive(inline_pipe(agent_url))
    Agent.gate_extra = {}
    d = last_card(events)
    assert d["kind"] == "questions" and [q["header"] for q in d["questions"]] == ["DB", "Extras"]
    assert "1. Postgres" in out and "2. SQLite" in out and "ask-0123456789" in out


def test_a_gate_without_an_ask_id_says_how_to_answer_by_message(agent_url):
    out, events = drive(inline_pipe(agent_url))                      # old bridge: no ask_id
    assert "approve ab12cd34" in out and not any(e["type"] == "embeds" for e in events)


def test_a_card_answer_goes_to_the_bridge_and_an_answered_card_is_shown(agent_url):
    Agent.asks = [{"ask_id": "ask-0123456789", "state": "pending", "created": 1, **QASK}]
    msg = 'answer ask-0123456789 {"answers":[{"question":"Which database?","answer":"SQLite"}]}'
    Agent.gate = False
    Agent.seen = []
    out, events = drive(inline_pipe(agent_url), text=msg)
    Agent.gate = True
    Agent.asks = []
    assert Agent.seen[-1][0] == "/agent/chat" and Agent.seen[-1][1]["message"] == msg
    assert last_card(events)["answered"]["answers"][0]["answer"] == "SQLite"


# --- #1406: an ask answered in the cockpit shows in the chat ---------------------------------------
def drive_and_watch(p, settle, text="push", chat="c1"):
    """Drive a turn that ends on a card, then `settle()` the ask (as the cockpit would) and let the
    watcher finish, all in one loop: OWUI keeps the turn's emitter alive the same way."""
    events = []
    p.valves.ASK_POLL_S = 0.01

    async def emit(ev):
        events.append(ev)

    async def go():
        gen = await p.pipe({"model": "nomad.agent", "stream": True,
                            "messages": [{"role": "user", "content": text}]},
                           __metadata__={}, __chat_id__=chat, __event_emitter__=emit)
        out = "".join([t async for t in gen])
        n = len(events)
        settle()
        await asyncio.wait_for(asyncio.gather(*list(p._watches.values())), 5)
        return out, n
    out, n = run(go())
    return out, events[:n], events[n:]


def _settled(state, via="api", **kw):
    def settle():
        Agent.asks = [{"ask_id": "ask-0123456789", "run_id": "ab12cd34", "chat_id": "c1",
                       "state": state, "created": 1, "via": via, **QASK, **kw}]
    return settle


@pytest.fixture
def cockpit(agent_url):
    Agent.gate_extra = QASK
    Agent.asks = [{"ask_id": "ask-0123456789", "state": "pending", "created": 1, **QASK}]
    Agent.attach = [{"type": "status", "text": "Write"}, {"type": "delta", "text": "Using SQLite."},
                    {"type": "done", "run_id": "ab12cd34", "status": "done"}]
    yield agent_url
    Agent.gate_extra, Agent.asks, Agent.attach = {}, [], []


def test_a_cockpit_answer_marks_the_card_and_streams_the_rest_of_the_run(cockpit):
    p = inline_pipe(cockpit)
    out, turn, later = drive_and_watch(p, _settled("answered", answers={"Which database?": "SQLite"}))
    assert "after" not in out                                   # the turn ended on the card
    card = embedded([e for e in later if e["type"] == "embeds"][0]["data"]["embeds"][-1])
    assert card["answered"]["answers"][0]["answer"] == "SQLite"
    text = "".join(e["data"]["content"] for e in later if e["type"] == "message")
    assert "Answered in the cockpit: SQLite" in text and text.endswith("Using SQLite.")
    assert any(path.startswith("/agent/attach?run_id=ab12cd34&cursor=delivered") for path, _ in Agent.seen)
    assert later[-1]["type"] == "status" and later[-1]["data"]["done"] and not p._watches


def test_a_chat_answer_leaves_the_rest_to_that_turn(cockpit):
    p = inline_pipe(cockpit)
    _, _, later = drive_and_watch(p, _settled("answered", via="chat", answers={"Which database?": "SQLite"}))
    assert later == [] and not any(path.startswith("/agent/attach") for path, _ in Agent.seen)


def test_an_expired_ask_is_said_in_the_chat(cockpit):
    p = inline_pipe(cockpit)
    _, _, later = drive_and_watch(p, _settled("expired"))
    text = "".join(e["data"]["content"] for e in later if e["type"] == "message")
    assert "expired" in text and not any(path.startswith("/agent/attach") for path, _ in Agent.seen)
    assert any(e["type"] == "embeds" for e in later)


def test_one_watcher_per_ask_and_none_without_a_chat(cockpit):
    p = inline_pipe(cockpit)

    async def go():
        async def emit(ev):
            pass
        g = {"ask_id": "ask-0123456789", "run_id": "ab12cd34", **QASK}
        p._watch(g, "c1", emit, [])
        p._watch(g, "c1", emit, [])
        p._watch({**g, "ask_id": "ask-other"}, "api-x", emit, [])
        n = len(p._watches)
        for t in p._watches.values():
            t.cancel()
        return n
    assert run(go()) == 1


@pytest.mark.parametrize("state,words", [("answered", "already answered"), ("expired", "expired"),
                                          ("timeout", "timed out"), ("cancelled", "cancelled")])
def test_a_second_answer_to_a_settled_ask_is_refused_politely(agent_url, state, words):
    Agent.asks = [{"ask_id": "ask-0123456789", "state": state, "created": 1, "decision": "approve",
                   "answers": {"Which database?": "Postgres"}, **QASK}]
    Agent.seen = []
    out, events = drive(inline_pipe(agent_url), text='answer ask-0123456789 {"decision":"approve"}')
    Agent.asks = []
    assert words in out and "Nothing was changed" in out
    assert not any(path in ("/agent/chat", "/agent/decide") for path, _ in Agent.seen)
    assert "answered" in last_card(events)                            # read-only card


def test_typed_reply_answers_the_oldest_pending_ask(agent_url):
    old = {"ask_id": "ask-0000000001", "state": "pending", "created": 1, "kind": "permission",
           "tool": "Bash", "summary": "git push", "reason": ""}
    new = {"ask_id": "ask-0000000002", "state": "pending", "created": 2, **QASK}
    Agent.asks = [new, old]
    Agent.gate = False
    Agent.seen = []
    drive(inline_pipe(agent_url), text="approve")
    sent = Agent.seen[-1][1]["message"]
    Agent.gate = True
    Agent.asks = []
    assert sent == 'answer ask-0000000001 {"decision": "approve"}'


@pytest.mark.parametrize("text,want", [
    ("2; 1,3", {"answers": [{"question": "Which database?", "answer": "SQLite"},
                            {"question": "Which extras?", "answer": ["Auth", "Queue"]}]}),
    ("postgres; Cache", {"answers": [{"question": "Which database?", "answer": "Postgres"},
                                     {"question": "Which extras?", "answer": ["Cache"]}]}),
    ("deny not now", {"decision": "deny", "note": "not now"}),
    ("approve", None)])
def test_typed_answer_maps_numbers_labels_and_free_text(text, want):
    assert P.typed_answer({"kind": "question", **QASK}, text) == want


def test_typed_answer_free_text_and_permission_words():
    free = {"kind": "question", "questions": [{"question": "Notes?", "options": []}]}
    assert P.typed_answer(free, "use the blue one") == {"answers": [{"question": "Notes?", "answer": "use the blue one"}]}
    perm = {"kind": "permission"}
    assert P.typed_answer(perm, "Yes") == {"decision": "approve"}
    assert P.typed_answer(perm, "no") == {"decision": "deny"}
    assert P.typed_answer(perm, "maybe later") is None
    assert P.typed_answer({"kind": "question", **QASK}, "no") != {"decision": "deny"}   # "no" can be an answer


def test_an_answer_message_for_a_closed_ask_reaches_the_pipe_through_auto(agent_url, engine):
    Agent.state = {"session": False, "pending": 0, "eligible": True}
    p = inline_pipe(agent_url)
    p.valves.ENGINE_URL = engine
    Agent.asks = [{"ask_id": "ask-0123456789", "state": "answered", "created": 1, **QASK}]
    out = auto(p, 'answer ask-0123456789 {"decision":"approve"}')
    Agent.asks = []
    assert "already answered" in out


# --- #1401 / #1400: file logging and the bridge's new flags -----------------------------------------
def test_file_events_and_failures_are_logged(agent_url, caplog):
    import logging
    Agent.gate = False
    Agent.files = [{"type": "file", "name": "a.png", "mime": "image/png", "size": 3, "b64": "!!notb64",
                    "source": "scan", "reason": "new file in the run's folder"}]
    p = agent_pipe(agent_url)
    p.valves.FILES_ENABLED = True
    with caplog.at_level(logging.INFO, logger="nomad_pipe"):
        out, _ = drive(p)
    Agent.gate, Agent.files = True, []
    assert "Couldn't show" in out
    assert any(r.levelno == logging.INFO and "a.png" in r.getMessage() and "source=scan" in r.getMessage()
               and "new file in the run" in r.getMessage() for r in caplog.records)
    assert any(r.levelno == logging.WARNING and "a.png" in r.getMessage() for r in caplog.records)


def test_too_large_file_is_not_decoded_or_uploaded_and_says_where_it_is():
    p = P.Pipe()

    async def boom(*a):
        raise AssertionError("must not upload")
    p._upload = boom
    embeds = []
    md = run(p._show_file({"type": "file", "name": "big.mp4", "mime": "video/mp4", "size": 12 * 1024 * 1024,
                           "b64": "", "path": "/work/big.mp4", "too_large": True, "download_only": True},
                          object(), {"id": "u"}, embeds))
    assert "`big.mp4` (12.0 MB) is too large to show" in md and "`/work/big.mp4`" in md and not embeds


def test_download_only_file_is_uploaded_and_shown_as_a_link_only():
    import base64
    p = P.Pipe()
    up = []

    async def fake(request, user, name, mime, data):
        up.append((name, mime, data))
        return "fid1"
    p._upload = fake
    embeds = []
    for name, mime in (("pic.bmp", "image/bmp"), ("doc.pdf", "application/pdf"), ("t.csv", "text/csv")):
        md = run(p._show_file({"type": "file", "name": name, "mime": mime, "size": 2048,
                               "b64": base64.b64encode(b"a,b\n1,2\n").decode(), "path": "/w/" + name,
                               "download_only": True}, object(), {"id": "u"}, embeds))
        assert f"[{name}](/api/v1/files/fid1/content)" in md and "![" not in md and "|" not in md
    assert len(up) == 3 and embeds == []


def test_sync_inlines_the_card_builder_into_the_single_file_function():
    text = (HERE / "nomad_pipe.py").read_text()
    out = S.bundle(text)
    assert "# @@ASK_CARD@@" not in out and "def build_card(" in out and "SAMPLE_Q" not in out
    ns = {"__name__": "owui_installed"}               # no __file__: like Open WebUI's exec of the function
    exec(compile(out, "installed.py", "exec"), ns)
    assert "answer " in ns["build_card"]({"id": "x", "kind": "approve", "tool": "Bash"})
    assert S.bundle("no marker here\n") == "no marker here\n"


# --- #1441 the Dispatch panel's calls --------------------------------------------------------

def _rpc(p, rpc, **kw):
    return run(p.pipe({"model": "nomad.auto", "stream": False, "nomad_rpc": rpc,
                       "messages": [{"role": "user", "content": "nomad dispatch"}]}, **kw))


def test_a_dispatch_call_goes_to_the_engine_and_comes_back_as_json(engine):
    out = json.loads(_rpc(pipe(engine), {"op": "dispatch", "entry": "p-civis-poc", "id": 944,
                                         "extra": "dropped"}))
    assert out["ok"] and out["path"] == "/owui/dispatch"
    assert out["got"] == {"op": "dispatch", "entry": "p-civis-poc", "id": 944}


def test_an_unknown_op_never_reaches_the_engine(engine):
    Engine.seen.clear()
    out = json.loads(_rpc(pipe(engine), {"op": "approve", "id": 944}))
    assert out["ok"] is False and "op must be" in out["error"]
    assert Engine.seen == []


def test_a_dispatch_key_inside_a_chat_is_a_normal_turn(engine, saved):
    """Only a chat-less call is the panel's: a chat message carrying the key is answered as usual."""
    Engine.seen.clear()
    out = _rpc(pipe(engine), {"op": "dispatch", "id": 944},
               __metadata__={"chat_id": "c-rpc", "chat_variables": {}}, __chat_id__="c-rpc")
    assert out == "hello"
    assert Engine.seen and "op" not in Engine.seen[0]


def test_a_dispatch_call_with_the_engine_down_says_so():
    out = json.loads(_rpc(pipe("http://127.0.0.1:9"), {"op": "items"}))
    assert out["ok"] is False and "can't answer" in out["error"]
