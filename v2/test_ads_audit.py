"""T3.1 · "Claude Ads": the ads lane's audit-only mode.

The audit reads ad-account data and reports on it. It must never be able to change an ad platform,
and every figure it reports must come from what the operator supplied. These tests hold the parts of
that which live in code: which Skill runs, which tools it can reach, what it proposes at the gate,
and that the operator's pasted data reaches it instead of the Manager's one-line paraphrase.

Run: python3 -m pytest v2/test_ads_audit.py -q   (engine tests need the container's deps)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drafting  # noqa: E402
import specialists  # noqa: E402

ADS = specialists.SPECIALISTS["ads"]


# ── selection ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ask", [
    "Audit our Google Ads account",
    "audit the Meta Ads campaigns for September",
    "Run an ads health check on LinkedIn",
    "review my ad account and tell me what's wasting money",
])
def test_an_audit_ask_selects_the_audit_mode(ask):
    assert ADS.mode_for(ask) == "audit"


@pytest.mark.parametrize("ask", [
    "write a LinkedIn ad for the autumn offer",
    "a banner image for the Meta campaign",
    "three headlines for Google Ads",
])
def test_a_copy_or_visual_ask_keeps_the_copywriter(ask):
    assert ADS.mode_for(ask) is None


def test_ad_account_audits_are_routable_and_other_audits_are_not():
    assert specialists.is_ad_account_audit("audit our Google Ads account")
    assert specialists.is_ad_account_audit("audit the ad sets on Meta")
    # An audit with no ad words is somebody else's job (a repo, a security review).
    assert not specialists.is_ad_account_audit("audit the nomad repo for secrets")
    assert not specialists.is_ad_account_audit("audit the email campaign copy")


# ── the boundary: read-only, grounded ────────────────────────────────────────────────────────

def test_the_audit_mode_has_no_write_and_no_web_tool():
    """No ad-platform tool exists anywhere NOMAD drafts from, and the audit may not search the web
    either: a benchmark pulled from a page is a number the operator did not supply."""
    m = ADS.modes["audit"]
    assert m["read_tools"] == ["recall"]
    assert all(t in drafting.READ_TOOLS for t in m["read_tools"])
    assert not any(k.startswith(("google", "meta", "linkedin", "ads")) for k in drafting.READ_TOOLS)


def test_the_skill_states_the_hard_rules():
    skill = ADS.modes["audit"]["skill"]
    assert "Read-only" in skill and "not available" in skill
    for platform in ("Google Ads", "Meta Ads", "LinkedIn Ads"):
        assert platform in skill
    for area in ("Account structure", "Targeting", "Budget and bidding", "Creative and copy",
                 "Conversion tracking", "Performance versus the stated goal"):
        assert area in skill
    assert "Changes made: none (audit-only)" in skill


def test_the_audit_proposes_a_filed_report_and_skips_the_style_rewrite(monkeypatch):
    """The only action is filing the report to NOMAD's own content table, behind the gate. The
    detector rewrite is skipped because it rewrites prose, and an audit's prose is its numbers."""
    seen = {}

    def draft(system, user, alias, max_tokens, allowed=()):
        seen.update(system=system, user=user, allowed=list(allowed), max_tokens=max_tokens)
        return "# Ads audit: Acme\nChanges made: none (audit-only)", []
    monkeypatch.setattr(specialists.drafting, "draft", draft)
    monkeypatch.setattr(specialists.detector, "enforce",
                        lambda *a, **k: pytest.fail("the detector must not rewrite an audit"))
    prop = ADS.propose("audit Acme's Google Ads: spend 1200, 14 conversions", "Acme", "r1")
    assert prop["action"] == "save_content" and prop["mode"] == "audit"
    assert prop["args"]["topic"].startswith("Ads audit · ")
    assert prop["args"]["content"].startswith("# Ads audit")
    assert seen["system"] == ADS.modes["audit"]["skill"]
    assert seen["allowed"] == ["recall"]
    assert "spend 1200, 14 conversions" in seen["user"]


def test_copy_asks_still_get_the_copywriter_skill(monkeypatch):
    seen = {}

    def draft(system, user, alias, max_tokens, allowed=()):
        seen["system"] = system
        return "copy", []
    monkeypatch.setattr(specialists.drafting, "draft", draft)
    monkeypatch.setattr(ADS, "style", None)
    prop = ADS.propose("write a headline for the autumn offer", "autumn offer", "r2")
    assert prop["action"] == "save_content" and "mode" not in prop
    assert seen["system"] == ADS.skill


# ── the engine: routing and the supplied data ────────────────────────────────────────────────

class _AdsSpec:
    lane = name = "ads"

    def __init__(self):
        self.intents = []

    def propose(self, intent, target, run_id):
        self.intents.append(intent)
        return {"action": "save_content", "args": {"topic": target, "content": "c", "run_id": run_id}}


def _eng(monkeypatch, lane, intent):
    pytest.importorskip("fastapi", reason="engine deps live in the container")
    import engine as engine_mod
    from test_engine_states import FakeDB
    e = engine_mod.Engine(db=FakeDB())
    ads, other = _AdsSpec(), _AdsSpec()
    e.specialists = {"ads": ads, "dev": other, "research": other}
    e.check_project = lambda p: {"ok": True, "id": p, "name": p}
    e.project_ids = lambda: ["nomad"]
    monkeypatch.setattr(engine_mod.llm, "chat_json",
                        lambda m, s, u: {"lane": lane, "intent": intent, "target": "Acme",
                                         "confidence": 0.9})
    monkeypatch.setattr(engine_mod.memory, "recall", lambda db, q: "")
    return e, ads, other


GOAL = "Audit the Acme Google Ads account. Campaign Brand-UK: spend 1,200 EUR, 14 conversions."


def test_an_ad_audit_routed_elsewhere_lands_in_the_ads_lane(monkeypatch):
    e, ads, other = _eng(monkeypatch, "research", "review Acme's ads")
    r = e.start_run(GOAL)
    assert r["status"] == "awaiting-approval"
    assert ads.intents and not other.intents


def test_the_auditor_sees_the_data_the_operator_pasted(monkeypatch):
    """The Manager's intent is one line; the figures live in the goal. Grounding needs the goal."""
    e, ads, _ = _eng(monkeypatch, "ads", "audit Acme's Google Ads account")
    e.start_run(GOAL)
    assert ads.intents == [GOAL]


def test_a_copy_ask_keeps_the_managers_intent(monkeypatch):
    e, ads, _ = _eng(monkeypatch, "ads", "headline for Acme's autumn offer")
    e.start_run("write a headline for Acme's autumn offer")
    assert ads.intents == ["headline for Acme's autumn offer"]
