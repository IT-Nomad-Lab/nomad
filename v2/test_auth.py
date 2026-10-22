"""V2c · the login that has to exist before the engine is ever reachable from outside.

The last V2c row is `publish.sh`: a Cloudflare quick tunnel that puts a public URL in front of
NOMAD. The console had one and a password to go with it. The engine had no authentication of any
kind, and it is a much worse thing to expose: /ws/terminal is a shell on this laptop, /api/dispatch
runs Claude Code in the repos, /resume approves the actions the gate exists to hold.

Almost every test here is about refusing. A login that works is easy and is not the interesting
claim; the claims worth checking are that it refuses when it is half-configured, that it refuses the
two routes no password should stand in front of, and that the websocket path refuses too, since
Starlette's HTTP middleware never sees a websocket and the console's answer to that was a second
copy of the rule.

`_auth_check` reads module-level config, so each test sets it and the fixture puts it back. That is
deliberate: the alternative is passing config in, and a checker whose behaviour depends on an
argument the caller chooses is one call site away from being told it is fine.

Run: python3 -m pytest v2/test_auth.py -q   (engine deps live in the container)
"""
import base64
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402


@pytest.fixture(autouse=True)
def restore():
    was = (server.AUTH_MODE, server.AUTH_USER, server.AUTH_PASS)
    yield
    server.AUTH_MODE, server.AUTH_USER, server.AUTH_PASS = was


def mode(m, user="jde", password="hunter2"):
    server.AUTH_MODE, server.AUTH_USER, server.AUTH_PASS = m, user, password


def basic(user, password):
    return {"authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


PROXIED = {"cf-connecting-ip": "203.0.113.7"}


# ── local, the default ──────────────────────────────────────────────────────────────────────────

def test_a_local_request_needs_no_login():
    """The engine's port is bound to 127.0.0.1 and this is how it is used every day."""
    mode("off")
    assert server._auth_check({}, "/runs") == (0, "")


def test_the_default_mode_is_off():
    """Read out of the source, not out of the live environment: this suite runs inside the engine,
    and while publish.sh has the tunnel open that environment legitimately says `on`. A test that
    fails whenever the tunnel is up is a test that gets deleted.

    The compose file carries the same default and is not checked here, because it is not in the
    image and this module skips on the host for want of fastapi, so the assertion would never once
    have run. A checker with a hole reads like coverage and is worse than no checker."""
    assert 'os.environ.get("NOMAD_AUTH_MODE", "off")' in inspect.getsource(server)


# ── the backstop: proxied traffic arriving at an engine that is not expecting it ─────────────────

def test_a_proxied_request_is_refused_when_the_engine_is_not_expecting_one():
    """Not prompted for a password. Refused. If the engine was never configured for exposure and
    something proxied reaches it anyway, the configuration is wrong, and the safe answer to a
    request you cannot explain is no."""
    mode("off")
    status, msg = server._auth_check(PROXIED, "/runs")
    assert status == 403
    assert "NOMAD_AUTH_MODE=on" in msg


@pytest.mark.parametrize("header", ["cf-connecting-ip", "cf-ray", "x-forwarded-for",
                                    "x-forwarded-host", "x-real-ip"])
def test_every_proxy_header_trips_the_backstop(header):
    mode("off")
    assert server._auth_check({header: "x"}, "/runs")[0] == 403


def test_the_backstop_is_not_the_boundary():
    """The console's whole gate was this sniff, so a proxy setting none of those headers was served
    with no password. Here the mode is what decides, and the sniff only catches a mistake."""
    mode("on")
    assert server._auth_check(basic("jde", "hunter2"), "/runs") == (0, "")   # no proxy header at all
    mode("off")
    assert server._auth_check({}, "/runs") == (0, "")


# ── exposed ─────────────────────────────────────────────────────────────────────────────────────

def test_exposed_and_unauthenticated_is_a_401():
    mode("on")
    status, _ = server._auth_check({}, "/runs")
    assert status == 401


def test_the_right_password_gets_in():
    mode("on")
    assert server._auth_check(basic("jde", "hunter2"), "/runs") == (0, "")


@pytest.mark.parametrize("user,password", [("jde", "wrong"), ("wrong", "hunter2"),
                                           ("", ""), ("jde", "")])
def test_a_wrong_credential_does_not(user, password):
    mode("on")
    assert server._auth_check(basic(user, password), "/runs")[0] == 401


def test_a_malformed_header_is_refused_rather_than_crashing():
    """A 500 here would be a refusal too, but an unhandled exception in an auth path is a thing to
    fix rather than to rely on."""
    mode("on")
    for hdr in [{"authorization": "Basic not-base64!!"},
                {"authorization": "Basic " + base64.b64encode(b"\xff\xfe").decode()},
                {"authorization": "Bearer abc"},
                {"authorization": "Basic "}]:
        assert server._auth_check(hdr, "/runs")[0] == 401


def test_exposed_with_no_credentials_configured_serves_nothing():
    """Fails closed. Asking for a login and having none set must lock the door, not leave it open
    with a note on it. This is the half-configured case, which is the one that actually happens."""
    mode("on", user="", password="")
    status, msg = server._auth_check({}, "/runs")
    assert status == 503
    assert "not set" in msg
    # and not merely for the unauthenticated: a correct-looking header gets nowhere either
    assert server._auth_check(basic("jde", "hunter2"), "/runs")[0] == 503


def test_a_password_with_no_user_is_still_half_configured():
    mode("on", user="", password="hunter2")
    assert server._auth_check({}, "/runs")[0] == 503


# ── the two routes a password is not good enough for ─────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/dispatch", "/ws/terminal"])
def test_the_dangerous_routes_are_closed_when_exposed_not_protected(path):
    """The point of the tunnel is showing NOMAD to someone, which means handing them the URL and
    the password. A shared password in front of a shell is not a boundary. These two stay on the
    tailnet."""
    mode("on")
    status, msg = server._auth_check(basic("jde", "hunter2"), path)
    assert status == 403
    assert "Tailscale" in msg


@pytest.mark.parametrize("path", ["/api/dispatch", "/ws/terminal"])
def test_the_dangerous_routes_still_work_locally(path):
    """Closing them locally too would be the security theatre version of this: it would move the
    terminal out of the cockpit for no attacker's benefit."""
    mode("off")
    assert server._auth_check({}, path) == (0, "")


# ── exposed means read-only ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/resume", "/chat", "/api/chat", "/retry", "/task-action",
                                  "/run-backlog", "/plan-project", "/capture", "/api/tts",
                                  "/withdraw"])
def test_nothing_writes_while_the_engine_is_exposed(path):
    """Every one of these is a real route that does something, and the password holder is a visitor
    being shown NOMAD, not the operator."""
    mode("on")
    status, msg = server._auth_check(basic("jde", "hunter2"), path, "POST")
    assert status == 403
    assert "read-only" in msg


def test_the_second_door_to_the_gate_is_shut():
    """The first version of this closed /resume and called the gate protected. It was not. The
    intent router's gate handler calls resume_run directly, so typing `approve` into the chat box
    approves a run without /resume being touched at all. Named on its own because the parametrised
    test above would keep passing if someone re-narrowed the rule to a path list containing /chat
    and forgot why /chat was on it."""
    mode("on")
    # The premise, checked rather than remembered: the chat path really can approve. If this stops
    # being true the comment in server.py is wrong and someone should find out from a failure here.
    assert "resume_run" in inspect.getsource(server._handle_gate_decision), (
        "the chat gate handler no longer approves; re-read why /chat is closed when exposed")
    mode("on")
    for path in ("/resume", "/chat", "/api/chat"):
        assert server._auth_check(basic("jde", "hunter2"), path, "POST")[0] == 403


def test_the_rule_is_the_method_not_a_list_of_known_routes():
    """A route added next month has to be closed by omission rather than open by it. This posts to
    a path that does not exist and expects the refusal anyway."""
    mode("on")
    assert server._auth_check(basic("jde", "hunter2"), "/a-route-invented-in-2027", "POST")[0] == 403
    assert server._auth_check(basic("jde", "hunter2"), "/whatever", "DELETE")[0] == 403


def test_reading_still_works_while_exposed():
    """Read-only has to mean readable. A tunnel that shows nothing is not worth opening."""
    mode("on")
    for path in ("/", "/runs", "/api/services", "/api/knowledge"):
        assert server._auth_check(basic("jde", "hunter2"), path, "GET") == (0, "")
        assert server._auth_check(basic("jde", "hunter2"), path, "HEAD") == (0, "")


@pytest.mark.parametrize("path", ["/resume", "/chat", "/capture", "/api/tts"])
def test_writing_still_works_locally(path):
    """The read-only rule is about being exposed. Locally the engine is the thing the operator
    drives all day and none of this applies."""
    mode("off")
    assert server._auth_check({}, path, "POST") == (0, "")


def test_both_websockets_close_when_exposed_not_only_the_shell():
    """/ws/wake proxies a caller's audio into nomad-voice on localhost, which is not something to
    hand a stranger either."""
    mode("on")
    for path in ("/ws/terminal", "/ws/wake"):
        assert server._auth_check(basic("jde", "hunter2"), path, "WEBSOCKET")[0] == 403


def test_a_shell_cannot_be_reached_by_knowing_the_password():
    """Stated as its own test because it is the property, and the two above are the mechanism."""
    mode("on")
    assert server._auth_check(basic("jde", "hunter2"), "/ws/terminal")[0] != 0


# ── health stays readable ───────────────────────────────────────────────────────────────────────

def test_health_answers_in_every_mode():
    """nomad-watch probes it, and a watchdog that starts reporting the engine down because the
    engine grew a login would be a self-inflicted outage."""
    for m in ("off", "on"):
        mode(m)
        assert server._auth_check({}, "/health") == (0, "")
    mode("on", user="", password="")
    assert server._auth_check({}, "/health") == (0, "")


def test_health_is_the_only_thing_open_when_exposed():
    """A list that grows quietly is how this stops being true."""
    assert server.AUTH_OPEN_PATHS == ("/health",)


# ── the script that turns the login on ──────────────────────────────────────────────────────────

def _publish_sh():
    """publish.sh is COPYed into the image for exactly this. If it is missing, fail rather than
    skip: a check that quietly stops running is the failure mode this whole file is about."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "publish.sh"), os.path.join(here, "..", "v2", "publish.sh")):
        if os.path.exists(path):
            return open(path, encoding="utf-8").read()
    raise AssertionError("publish.sh not found; the Dockerfile is supposed to COPY it")


def test_the_teardown_is_a_trap_not_a_last_line():
    """The first real run of publish.sh died between turning the login on and printing anything,
    and left the engine exposed on the public internet with no output saying so. `set -e` plus
    `url=$(docker logs … | grep …)` with nothing to match yet is an exit, not an empty string.

    So the rollback cannot live on the happy path. Checked here rather than remembered, because the
    tunnel-opening lines are the part of this system that gets run once every few months."""
    sh = _publish_sh()
    assert "trap teardown EXIT" in sh, "nothing restores local mode when the script dies early"
    assert "docker rm -f \"$TUNNEL\"" in sh.split("teardown()")[1].split("}")[0], \
        "the teardown does not remove the tunnel"


def test_the_grep_that_killed_it_is_guarded():
    """Named for the exact line. A later edit that drops `|| true` reintroduces a failure whose
    symptom is a public URL nobody knows is open."""
    sh = _publish_sh()
    for line in sh.splitlines():
        if "trycloudflare" in line and "url=$(" in line:
            assert "|| true" in line, f"unguarded under set -e: {line.strip()}"
            break
    else:
        raise AssertionError("the URL-scraping line is gone; re-read why it needed guarding")


def test_the_script_proves_the_login_before_it_opens_anything():
    """Order, not presence. Opening the tunnel first and checking after is the version of this
    script that publishes an unauthenticated engine for however long the check takes."""
    sh = _publish_sh()
    assert sh.index("Checking the login actually took") < sh.index("cloudflare/cloudflared")


def test_the_script_refuses_without_credentials():
    sh = _publish_sh()
    assert "NOMAD_AUTH_USER is missing or empty" in sh
    assert "NOMAD_AUTH_PASS is missing or empty" in sh


# ── the wiring, not just the rule ────────────────────────────────────────────────────────────────

def test_the_middleware_is_actually_installed():
    """The rule being correct and the rule being applied are different claims, and this file would
    pass every test above with the middleware never registered."""
    assert any("_auth_middleware" in repr(getattr(m, "kwargs", {}) or m)
               for m in server.app.user_middleware), "no auth middleware on the app"


@pytest.mark.parametrize("name", ["ws_terminal", "ws_wake"])
def test_every_websocket_handler_checks_before_accepting(name):
    """Starlette's HTTP middleware never sees a websocket. A handler that calls accept() first has
    already opened the socket, and this is the path with the shell on it."""
    import inspect
    src = inspect.getsource(getattr(server, name))
    body = src.split('"""')[-1]
    assert "_ws_refused" in body, f"{name} does not check"
    assert body.index("_ws_refused") < body.index("accept()"), f"{name} accepts before checking"


def test_there_is_exactly_one_copy_of_the_rule():
    """The console has two, and the second one guards the terminal. One implementation, two call
    sites, is the only version of this that stays true."""
    import inspect
    src = inspect.getsource(server)
    assert src.count("def _auth_check") == 1
    assert src.count("compare_digest") == 2      # one pair, in _auth_check, and nowhere else
