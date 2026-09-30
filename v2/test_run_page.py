"""#799 · GET /cockpit/run/<id>: one run on one page, for the phone (PRD R3, R4, R5).

The Zulip gate message and the OpenProject card link here, so this is the page the operator reads
before approving from the phone. The claims checked are what it shows (every argument, whole and
escaped), what it approves (the hash it drew, through the existing /resume and nothing else), that
it reads without JavaScript, and that it stays reachable through the tailnet proxy while its writes
keep the same limits as the cockpit's.

The 390 px check itself is a browser measurement (Playwright, outside the container). What can be
checked here is the markup that measurement depends on.

Run: python3 -m pytest v2/test_run_page.py -q   (engine deps live in the container)
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
from test_cockpit_icons import _pictographs  # noqa: E402
from test_cockpit_layout import _scan_js  # noqa: E402

PLAN = "1. read the gate code\n2. pin the hash\n" + "step detail " * 150        # ~1,800 chars
ARGS = {"project": "nomad", "task": "pin the approval hash", "plan": PLAN, "branch": "fix/778"}
HASH = "f" * 64


def detail(**over):
    d = {"run_id": "1674da08a5aa", "lane": "dev", "status": "awaiting-approval",
         "created_at": "2026-09-28T10:02:11+00:00", "goal": "Fix the gate", "intent": "fix it",
         "action": "dispatch_build", "args": ARGS, "arg_fields": server.arg_fields(ARGS),
         "project": {"wp_id": 778, "wp_url": "https://pm.example/work_packages/778",
                     "wp_subject": "Pinned hash", "project_name": "NOMAD work", "board": "nomad-work"},
         "lookups": [{"tool": "read_file", "args": {"path": "v2/server.py"}}],
         "detector": {"checked": True, "verdict": "human", "style_score": 91},
         "log": [], "proposal_hash": HASH}
    d.update(over)
    return d


def page(**over):
    return server.run_page(detail(**over))


def inline_js(p):
    return "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", p, re.S))


# ── what it shows (R3) ──────────────────────────────────────────────────────────────────────────

def test_the_whole_plan_is_on_the_page_not_cut():
    p = page()
    assert server.html.escape(PLAN) in p
    for k in ARGS:
        assert f"<div class=k>{k}</div>" in p


def test_the_run_is_placed_status_project_action_goal_then_args():
    p = page().split("</h1>", 1)[1]           # the body, past the data attributes and the title
    order = ["AWAITING-APPROVAL", "nomad-work", "#778 Pinned hash", "dev · dispatch_build",
             "Fix the gate", "<div class=k>plan</div>", "AI-TEXT CHECK", "LOOKUPS (1)", "<span>APPROVE</span>"]
    at = [p.index(s) for s in order]
    assert at == sorted(at)


def test_the_task_links_to_openproject():
    assert 'href="https://pm.example/work_packages/778"' in page()


def test_lookups_are_collapsed():
    assert "<details><summary>LOOKUPS (1)</summary>" in page()


def test_untrusted_text_is_escaped_everywhere():
    """The goal and the args come from mail and web pages, and this page has the approve button."""
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    args = {"body": evil, "to": evil}
    p = page(goal=evil, args=args, arg_fields=server.arg_fields(args), error=evil,
             project={"board": evil, "wp_id": 1, "wp_subject": evil, "wp_url": "https://pm/x' onclick='y"},
             lookups=[{"q": evil}], action=evil)
    assert "<script>alert" not in p and "<img src=x" not in p
    assert "' onclick='y" not in p


def test_a_run_with_no_project_says_so():
    assert "no project" in page(project=None)


# ── what it approves (R4, R5) ───────────────────────────────────────────────────────────────────

def test_the_page_carries_the_hash_it_drew():
    assert f"data-hash='{HASH}'" in page()


def test_approve_sends_the_drawn_hash_and_never_reads_it_again():
    js = inline_js(page())
    assert "body.expect_hash=HASH" in js
    # /run is read only to notice a change, never to pick the hash that gets sent.
    assert "HASH=B.hash" in js and "expect_hash=d." not in js


def test_a_changed_proposal_is_reported_and_approve_turns_off():
    js = inline_js(page())
    assert "This proposal changed since you opened it. Reload to read the new one." in js
    assert "/changed since/.test(res.error)" in js
    assert "d.proposal_hash!==HASH" in js


def test_approve_asks_once_naming_action_and_project_and_reject_does_not():
    p = page()
    js = inline_js(p)
    assert "confirm('Approve '+B.action+' on '+B.project+'?')" in js
    assert js.count("confirm(") == 1 and "decision==='approved' && !confirm(" in js
    assert "data-action='dispatch_build' data-project='nomad-work'" in p


def test_it_adds_no_write_route():
    """Approve and Reject go to /resume, which is already on the tailnet allowlist."""
    js = inline_js(page())
    posts = re.findall(r"fetch\('([^']+)',\{method:'POST'", js)
    assert posts == ["/resume"]
    assert set(posts) <= set(server.TAILNET_WRITES)
    writes = [r.path for r in server.app.routes
              if getattr(r, "path", "").startswith("/cockpit") and set(getattr(r, "methods", ())) - {"GET", "HEAD"}]
    assert writes == []


@pytest.mark.parametrize("status", ["executing", "executed", "failed", "declined", "rejected"])
def test_a_decided_run_shows_no_buttons(status):
    p = page(status=status)
    assert "data-decide=" not in p and "Decided:" in p


def test_a_waiting_run_without_a_hash_cannot_be_approved_here():
    p = page(proposal_hash=None)
    assert "data-decide=approved" not in p and "no hash to pin" in p


def test_an_unresolved_project_is_warned_above_approve():
    """#851 on the run page: the same line the other gate surfaces show, before the buttons."""
    warn = server.project_warning({"action": "dispatch_build", "args": {"project": "claude-bridge"},
                                   "project_check": {"ok": False, "reason": "unknown",
                                                     "candidates": ["nomad", "nomad-os"]}})
    p = page(project_warning=warn)
    assert "PROJECT CHECK" in p and "claude-bridge" in p and "Candidates: nomad, nomad-os" in p
    assert p.index("PROJECT CHECK") < p.index("data-decide=approved")
    assert "PROJECT CHECK" not in page(project_warning="")
    assert "PROJECT CHECK" not in page(project_warning=warn, status="failed")
    assert "<script>x" not in page(project_warning="<script>x")


# ── phone, no-JS and icons ──────────────────────────────────────────────────────────────────────

def test_it_is_built_for_a_phone():
    p = page()
    assert "<meta name=viewport content='width=device-width,initial-scale=1'>" in p
    assert "overflow-wrap:anywhere" in p and "white-space:pre-wrap" in p
    assert ".bar{position:sticky;bottom:0" in p
    assert "min-height:48px" in p


def test_it_reads_without_javascript_and_loads_nothing_else():
    """Drawn on the server: the plan is in the HTML, not fetched. No external script, font or sheet."""
    p = page()
    assert server.html.escape(PLAN) in p.split("<script>")[0]
    assert not re.search(r"<script[^>]*\bsrc=|<link\b", p)
    assert "<noscript>" in p


def test_no_emoji_or_pictographs():
    assert _pictographs(page()) == []
    assert _pictographs(page(status="executed")) == []


def test_every_icon_is_in_the_page_sprite():
    p = page()
    defined = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', p))
    used = set(re.findall(r'href="#i-([a-z0-9-]+)"', p))
    assert used and used <= defined
    assert "Lucide" in p and "ISC License" in p


def test_no_string_literal_spans_a_line():
    assert _scan_js(inline_js(page())) == []


# ── the route ───────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from starlette.testclient import TestClient
    runs = {"1674da08a5aa": detail()}
    monkeypatch.setattr(server, "run_detail", lambda rid: runs.get(rid, {"run_id": rid, "status": None}))
    return TestClient(server.app)


def test_the_route_serves_the_page(client):
    r = client.get("/cockpit/run/1674da08a5aa")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "RUN 1674da08a5aa" in r.text


@pytest.mark.parametrize("rid", ["nope", "has space", "x" * 65])
def test_an_unknown_or_malformed_id_is_a_404(client, rid):
    r = client.get("/cockpit/run/" + rid)
    assert r.status_code == 404 and "No run with the id" in r.text


def test_the_json_run_stays_for_api_callers(client):
    assert client.get("/run?id=1674da08a5aa").json()["proposal_hash"] == HASH


# ── through the tailnet proxy ───────────────────────────────────────────────────────────────────

OP = "jde@example.com"
ORIGIN = "https://nomad.tail1234.ts.net:8449"


@pytest.fixture
def tailnet():
    was = (server.AUTH_MODE, server.TAILNET_OPERATOR, server.TAILNET_ORIGIN)
    server.AUTH_MODE, server.TAILNET_OPERATOR, server.TAILNET_ORIGIN = "tailnet", OP, ORIGIN
    yield
    server.AUTH_MODE, server.TAILNET_OPERATOR, server.TAILNET_ORIGIN = was


def via(login=OP, origin=ORIGIN):
    h = {"x-forwarded-for": "100.64.0.7", "tailscale-user-login": login}
    if origin:
        h["origin"] = origin
    return h


def test_the_operator_opens_the_page_through_the_proxy(client, tailnet):
    assert client.get("/cockpit/run/1674da08a5aa", headers=via()).status_code == 200


def test_another_login_cannot_open_it(client, tailnet):
    assert client.get("/cockpit/run/1674da08a5aa", headers=via(login="x@example.com")).status_code == 403


def test_the_decision_it_posts_is_allowed_from_the_cockpit_origin_only(tailnet):
    assert server._auth_check(via(), "/resume", "POST") == (0, "")
    assert server._auth_check(via(origin="https://evil.example"), "/resume", "POST")[0] == 403


# ── the runs table on the main cockpit, at phone width ──────────────────────────────────────────

def test_the_cockpit_runs_table_drops_two_columns_on_a_phone():
    """It was 554 px wide at 390, which put Approve off-screen. FROM and TYPE go under 600 px."""
    c = server.COCKPIT
    assert "<table id=runs>" in c
    assert "@media (max-width:600px)" in c
    assert "#runs th:nth-child(3),#runs td:nth-child(3),#runs th:nth-child(4),#runs td:nth-child(4){display:none}" in c


def test_each_cockpit_row_links_to_its_run_page():
    assert 'href="/cockpit/run/${encodeURIComponent(rid)}"' in server.COCKPIT
