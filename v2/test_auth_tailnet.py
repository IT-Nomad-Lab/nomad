"""#795, #796 · the cockpit behind `tailscale serve` on 8449 (PRD R2, R2a).

NOMAD_AUTH_MODE=tailnet lets the operator's phone reach the cockpit over the tailnet and approve
from it, without handing the tailnet the shell, the dispatcher or anything else that writes. The
claims checked here are the refusals: another tailnet login, a Cloudflare header pretending to be
Tailscale, a write from a foreign Origin, a write off the allowlist, a websocket, and a chat
message that would start, approve or delete something. Local traffic has to keep working as it does
with `off`, because the relay, nomad-watch and the laptop all use it.

Run: python3 -m pytest v2/test_auth_tailnet.py -q   (engine deps live in the container)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

OP = "jde@example.com"
ORIGIN = "https://nomad.tail1234.ts.net:8449"


@pytest.fixture(autouse=True)
def tailnet():
    was = (server.AUTH_MODE, server.TAILNET_OPERATOR, server.TAILNET_ORIGIN)
    server.AUTH_MODE, server.TAILNET_OPERATOR, server.TAILNET_ORIGIN = "tailnet", OP, ORIGIN
    yield
    server.AUTH_MODE, server.TAILNET_OPERATOR, server.TAILNET_ORIGIN = was


def via(login=OP, origin=ORIGIN, **extra):
    """Headers the way `tailscale serve` sends them, plus what a browser adds on a POST."""
    h = {"x-forwarded-for": "100.64.0.7", "tailscale-user-login": login}
    if origin:
        h["origin"] = origin
    h.update(extra)
    return {k: v for k, v in h.items() if v is not None}


# ── local traffic is untouched ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,method", [("/runs", "GET"), ("/capture", "POST"),
                                         ("/api/dispatch", "POST"), ("/ws/terminal", "WEBSOCKET")])
def test_local_requests_work_as_they_do_with_off(path, method):
    assert server._auth_check({}, path, method) == (0, "")


# ── who ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_operator_can_read():
    assert server._auth_check(via(), "/runs") == (0, "")


def test_the_login_match_ignores_case():
    assert server._auth_check(via(login="JDE@Example.com"), "/runs") == (0, "")


@pytest.mark.parametrize("login", ["someone@example.com", "", None])
def test_any_other_login_or_none_is_refused(login):
    """A tagged device sends no login at all, and a shared node sends its owner's. Neither is the
    operator."""
    assert server._auth_check(via(login=login), "/runs")[0] == 403


def test_a_cloudflare_request_claiming_to_be_the_operator_is_refused():
    """A tunnel passes whatever header the client sends, so the identity header means something
    only when Tailscale set it."""
    h = via(**{"cf-connecting-ip": "203.0.113.7"})
    assert server._auth_check(h, "/runs")[0] == 403
    assert server._auth_check(h, "/resume", "POST")[0] == 403


@pytest.mark.parametrize("unset", ["TAILNET_OPERATOR", "TAILNET_ORIGIN"])
def test_half_configured_serves_nothing_through_the_proxy(unset):
    setattr(server, unset, "")
    assert server._auth_check(via(), "/runs")[0] == 503
    assert server._auth_check({}, "/runs") == (0, "")          # the laptop still works


# ── what ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/resume", "/retry", "/run-next-task", "/chat"])
def test_the_operator_can_approve_retry_and_ask(path):
    assert server._auth_check(via(), path, "POST") == (0, "")


@pytest.mark.parametrize("path", ["/api/autopilot/pass", "/api/autopilot/config",
                                  "/api/autopilot/projects/runematch/start",
                                  "/api/autopilot/projects/civis-poc/stop",
                                  "/api/autopilot/projects/nomad.v2/stop",
                                  "/api/autopilot/projects/runematch/autopilot",
                                  "/api/autopilot/projects/civis-poc/autopilot"])
def test_the_operator_can_run_and_stop_the_autopilot_from_the_phone(path):
    """#883, operator decision 2026-09-28: Run and Stop and the budget strip work over the
    tailnet, with the same login and Origin checks as an approval."""
    assert server._auth_check(via(), path, "POST") == (0, "")
    assert server._auth_check(via(origin="https://evil.example"), path, "POST")[0] == 403
    assert server._auth_check(via(login="someone@example.com"), path, "POST")[0] == 403


@pytest.mark.parametrize("path", ["/api/autopilot/projects/runematch/delete",
                                  "/api/autopilot/projects/runematch/start/now",
                                  "/api/autopilot/projects/../start",
                                  "/api/autopilot/projects//start",
                                  "/api/autopilot/projects/runematch/start?x=1",
                                  "/api/autopilot/projects/start",
                                  "/api/autopilot/other", "/api/autopilot/config/x",
                                  "/xapi/autopilot/pass", "/api/autopilot/pass/",
                                  # #890: the switch route, near misses
                                  "/api/autopilot/projects/runematch/autopilot/",
                                  "/api/autopilot/projects/runematch/autopilot/on",
                                  "/api/autopilot/projects/runematch/autopilots",
                                  "/api/autopilot/projects/runematch/autopilot?on=1",
                                  "/api/autopilot/projects/../autopilot",
                                  "/api/autopilot/projects//autopilot",
                                  "/api/autopilot/projects/-x/autopilot",
                                  "/api/autopilot/projects/" + "a" * 65 + "/autopilot",
                                  "/api/autopilot/projects/a/b/autopilot",
                                  "/api/autopilot/projects/autopilot",
                                  "/api/autopilot/autopilot", "/api/autopilot/projects/x/Autopilot"])
def test_only_those_autopilot_routes_open(path):
    """Nothing broader than the four writes: a near miss is still closed."""
    assert server._auth_check(via(), path, "POST")[0] == 403


@pytest.mark.parametrize("path", ["/capture", "/api/chat", "/plan-project", "/run-backlog",
                                  "/pm-hook", "/a-route-invented-in-2027"])
def test_every_other_write_is_closed(path):
    """An allowlist, so a route added later is closed through the proxy until someone opens it."""
    assert server._auth_check(via(), path, "POST")[0] == 403


@pytest.mark.parametrize("path,method", [("/api/dispatch", "POST"), ("/api/dispatch", "GET"),
                                         ("/ws/terminal", "WEBSOCKET"), ("/ws/wake", "WEBSOCKET")])
def test_the_shell_the_dispatcher_and_the_sockets_stay_closed(path, method):
    assert server._auth_check(via(), path, method)[0] == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "https://nomad.tail1234.ts.net:8448",
                                    "http://nomad.tail1234.ts.net:8449", None])
def test_a_write_from_another_origin_is_refused(origin):
    """CSRF. Any page open in the phone's browser can POST to a tailnet address."""
    assert server._auth_check(via(origin=origin), "/resume", "POST")[0] == 403


def test_the_origin_is_not_needed_to_read():
    assert server._auth_check(via(origin=None), "/runs") == (0, "")


def test_off_mode_still_refuses_a_tailscale_request():
    """With the mode left at `off`, `tailscale serve` pointing at the engine is a mistake, and the
    identity headers alone are enough to tell."""
    server.AUTH_MODE = "off"
    assert server._auth_check({"tailscale-user-login": OP}, "/runs")[0] == 403


# ── chat answers questions only (R2a) ───────────────────────────────────────────────────────────

@pytest.fixture
def no_model(monkeypatch):
    """Nothing that starts, approves or forgets may run. Each of these fails the test if reached."""
    def boom(*a, **k):
        raise AssertionError("reached a handler that acts")
    for name in ("_handle_gate_decision", "_handle_pipeline_capture", "_handle_start_project",
                 "_handle_self_develop", "_handle_research"):
        monkeypatch.setattr(server, name, boom)
    monkeypatch.setattr(server.eng, "resume_run", boom)
    monkeypatch.setattr(server.eng, "start_run", boom)
    monkeypatch.setattr(server.eng, "plan_project", boom)
    import memory_chat
    monkeypatch.setattr(memory_chat, "forget", boom)


@pytest.mark.parametrize("msg", ["approve", "approve all", "reject", "research local STT engines",
                                 "work on your backlog", "forget that", "forget everything"])
def test_remote_chat_refuses_anything_that_acts(msg, no_model):
    r = server.chat_reply(msg, remote=True)
    assert r.get("refused") and r["reply"] == server.REMOTE_REFUSAL


def test_the_allowed_intents_are_a_short_list():
    """A list of what is allowed, so an intent added later is refused from the phone by default."""
    assert server.REMOTE_CHAT_INTENTS == ("chat", "diagnostics")


def test_remote_diagnostics_is_answered(monkeypatch, no_model):
    monkeypatch.setitem(server.ACTION_INTENTS, "diagnostics", lambda p: "all quiet")
    import memory_chat
    monkeypatch.setattr(memory_chat, "remember", lambda *a, **k: None)
    monkeypatch.setattr(server.intents, "classify", lambda m: ("diagnostics", {}))
    assert server.chat_reply("run diagnostics", remote=True)["reply"] == "all quiet"


def test_the_same_approve_still_works_on_the_laptop(monkeypatch):
    monkeypatch.setitem(server.ACTION_INTENTS, "gate_decision", lambda p: "shown")
    import memory_chat
    monkeypatch.setattr(memory_chat, "remember", lambda *a, **k: None)
    assert server.chat_reply("approve")["reply"] == "shown"


def _post_chat(body, headers):
    from starlette.testclient import TestClient
    return TestClient(server.app).post("/chat", json=body, headers=headers)


@pytest.mark.parametrize("body", [{"message": "project: a new thing"},
                                  {"message": "capture: fix the relay"},
                                  {"message": "fix the relay", "capture": True},
                                  {"message": "⚑ fix the relay"}])
def test_the_chat_shortcuts_that_start_work_are_refused_through_the_proxy(body, no_model):
    r = _post_chat(body, via())
    assert r.status_code == 200 and r.json().get("refused")


def test_a_chat_post_from_another_login_never_reaches_the_router(no_model):
    assert _post_chat({"message": "approve"}, via(login="someone@example.com")).status_code == 403


# ── the page ────────────────────────────────────────────────────────────────────────────────────

def test_the_page_through_the_proxy_hides_capture_and_voice():
    from starlette.testclient import TestClient
    c = TestClient(server.app)
    remote, local = c.get("/", headers=via()).text, c.get("/").text
    assert "<body class=remote>" in remote and "<body class=remote>" not in local
    # !important, because #voicebar sets display:flex inline and an inline style beats a rule.
    assert "body.remote #voicebar,body.remote #ccap{display:none!important}" in remote
