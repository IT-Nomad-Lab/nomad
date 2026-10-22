"""#1397 · the cockpit's Policies page: what the page calls, that it never holds a token, that the
phone gets a read-only editor, and the routes that serve it. #1398 · the simulator, the tag flow and
the guardrail save behind a typed-id dialog.

The form's YAML writer is tested against every policy file in nomad_policy/test_editor_form.py
(host, needs node).

Run: python3 -m pytest v2/test_cockpit_policies.py -q   (route tests need fastapi: the engine image)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cockpit_policies as P  # noqa: E402


def test_page_calls_only_the_engine_policy_routes():
    page = P.PAGE
    for call in ("'/get?id='", "'/effective?project='", "'/validate'", "'/save'", "'/attach'",
                 "'/simulate'", "'/tag-dry-run'", "'/tag-save'", "'/tag-accept'", "'/guardrail-save'"):
        assert call in page
    assert "fetch('/api/policies'+path" in page
    assert "X-Nomad-Token" not in page and "X-Nomad-Cockpit" not in page and "8090" not in page


def test_page_writes_text_never_markup():
    assert "innerHTML" not in P.PAGE and "outerHTML" not in P.PAGE and "insertAdjacentHTML" not in P.PAGE


def test_effective_view_shows_the_deciding_statement():
    for col in ("Item", "Kind", "Effect", "Decided by", "Why"):
        assert f"<th>{col}</th>" in P.PAGE
    assert "i.layer+' · '+i.policy+(i.sid?' · '+i.sid:'')" in P.PAGE


def test_read_only_from_the_phone_and_no_attach_for_a_guardrail():
    assert "body.remote .write{display:none !important}" in P.PAGE
    assert "lockEditor(REMOTE);" in P.PAGE
    assert "$('attachbox').hidden=j.guardrail" in P.PAGE
    assert "$('gsave').disabled=REMOTE||" in P.PAGE


def test_a_guardrail_save_shows_the_diff_and_needs_the_typed_id():
    page = P.PAGE
    assert "<dialog id=gdlg" in page and "dl.showModal()" in page
    assert "if(CUR.guardrail){" in page and "return guardDialog(t,v.j.diff)" in page
    assert "$('gdlgok').disabled=$('gdlgin').value!==CUR.id" in page
    assert "{id:CUR.id,yaml:t,confirm:typed}" in page
    # the only route that carries confirm is the guardrail one
    assert page.count("confirm:typed") == 1


def test_simulator_and_tag_views():
    page = P.PAGE
    assert "const VIEWS=['list','edit','eff','sim','tag']" in page
    for sec in ("v-sim", "v-tag"):
        assert f"<section id={sec} hidden>" in page
    assert "'Empty diff: '" in page and "PROPOSED BY THE TYPE" in page
    assert "{project:p,change:id}" in page and "{project:TAG.project,type:TAG.type}" in page


def test_a_form_save_asks_before_dropping_comments_and_defaults_are_hidden_first():
    assert "drops its '+lost+' comment line(s)" in P.PAGE
    assert "<option value=stmt>decided by a statement</option>" in P.PAGE


def test_phone_layout():
    assert 'name=viewport content="width=device-width,initial-scale=1"' in P.PAGE
    assert "@media(max-width:640px)" in P.PAGE and "td::before{content:attr(data-h)" in P.PAGE


def _client():
    pytest.importorskip("fastapi", reason="engine deps live in the container")
    pytest.importorskip("httpx", reason="TestClient needs httpx")
    import server
    from fastapi.testclient import TestClient
    return server, TestClient(server.app)


def test_the_page_is_served_and_read_only_through_a_proxy(monkeypatch):
    server, client = _client()
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    r = client.get("/cockpit/policies")
    assert r.status_code == 200 and "<h1>POLICIES</h1>" in r.text and "<body class=remote>" not in r.text
    op, origin = "jde@example.com", "https://nomad.tail1234.ts.net:8449"
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", op)
    monkeypatch.setattr(server, "TAILNET_ORIGIN", origin)
    h = {"x-forwarded-for": "100.64.0.7", "tailscale-user-login": op, "origin": origin}
    r = client.get("/cockpit/policies", headers=h)
    assert r.status_code == 200 and "<body class=remote>" in r.text


def test_the_cockpit_more_section_links_the_page():
    server, _ = _client()
    more = server.COCKPIT.split("<section id=dest-more", 1)[1].split("</section>", 1)[0]
    assert 'href="/cockpit/policies"' in more
