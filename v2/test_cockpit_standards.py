"""#1378, #1379 · the cockpit's Standards page: what the page calls, that it never holds a token,
that Save goes to a branch with its diff, that the phone gets a read-only editor with master's dry
run, and the routes that serve it.

The form's YAML writer is tested against every profile in nomad_projects/test_standards_form.py
(host, needs node).

Run: python3 -m pytest v2/test_cockpit_standards.py -q   (route tests need fastapi: the engine image)
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cockpit_standards as S  # noqa: E402


def test_page_calls_only_the_profile_routes():
    page = S.PAGE
    for call in ("'/list'", "'/get?id='", "'/validate'", "'/dry-run'", "'/dry-run?id='"):
        assert call in page
    assert "fetch('/api/profiles'+path" in page
    assert set(re.findall(r"api\('POST','(/[a-z-]+)'", page)) == {"/validate", "/dry-run", "/save"}
    assert "X-Nomad-Token" not in page and "X-Nomad-Cockpit" not in page and "8090" not in page


def test_save_goes_to_a_branch_and_shows_the_diff():
    page = S.PAGE
    assert "<button id=save class=write>Save to branch</button>" in page   # hidden from the phone
    assert "never on master" in page and "'Saved on branch '+j.branch" in page
    assert "'Not saved: '+err(res)" in page                               # the refusal names the field
    assert "d.textContent=j.diff" in page


def test_page_writes_text_never_markup():
    assert "innerHTML" not in S.PAGE and "outerHTML" not in S.PAGE and "insertAdjacentHTML" not in S.PAGE


def test_every_link_opens_a_new_tab():
    for tag in re.findall(r"<a [^>]*href=[^>]*>", S.PAGE):
        if 'href="/cockpit"' in tag:                 # the back link stays in the tab
            continue
        assert "target=_blank" in tag and "rel=noopener" in tag, tag


def test_the_form_covers_every_profile_field():
    page = S.PAGE
    for fid in ("f-title", "f-desc", "f-pols", "f-ask", "f-skip", "f-agents", "f-pre", "f-mile",
                "f-stack", "f-files", "f-cap", "f-model", "f-budget", "f-lanes"):
        assert f"id={fid}" in page
    assert "<textarea id=yaml" in page and "id=m-yaml" in page


def test_the_picker_shows_what_each_policy_grants_and_filters_on_it():
    page = S.PAGE
    assert "grantText(p.grants)" in page and "[id,p.description,gt].join(' ')" in page
    assert "the filter finds the policy that grants a server or skill" in page


def test_the_dry_run_shows_the_changes_and_the_projects_it_does_not_reach():
    page = S.PAGE
    assert "WHAT THE EDIT CHANGES" in page and "The edit changes nothing a new project gets." in page
    assert "registered projects keep their configuration until re-tagged or recompiled" in page


def test_read_only_from_the_phone():
    page = S.PAGE
    assert "body.remote .write{display:none !important}" in page
    assert "lockEditor(REMOTE);" in page and "x.disabled=REMOTE" in page
    assert "if(REMOTE)res=await api('GET','/dry-run?id='" in page


def test_phone_layout():
    assert 'name=viewport content="width=device-width,initial-scale=1"' in S.PAGE
    assert "@media(max-width:640px)" in S.PAGE and "td::before{content:attr(data-h)" in S.PAGE


def _client():
    pytest.importorskip("fastapi", reason="engine deps live in the container")
    pytest.importorskip("httpx", reason="TestClient needs httpx")
    import server
    from fastapi.testclient import TestClient
    return server, TestClient(server.app)


def test_the_page_is_served_and_read_only_through_a_proxy(monkeypatch):
    server, client = _client()
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    r = client.get("/cockpit/standards")
    assert r.status_code == 200 and "<h1>STANDARDS</h1>" in r.text and "<body class=remote>" not in r.text
    op, origin = "jde@example.com", "https://nomad.tail1234.ts.net:8449"
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", op)
    monkeypatch.setattr(server, "TAILNET_ORIGIN", origin)
    h = {"x-forwarded-for": "100.64.0.7", "tailscale-user-login": op, "origin": origin}
    r = client.get("/cockpit/standards", headers=h)
    assert r.status_code == 200 and "<body class=remote>" in r.text


def test_the_cockpit_more_section_links_the_page():
    server, _ = _client()
    more = server.COCKPIT.split("<section id=dest-more", 1)[1].split("</section>", 1)[0]
    assert 'href="/cockpit/standards"' in more
