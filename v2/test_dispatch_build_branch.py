"""dispatch_build hands the operator's branch choice to the dispatcher and reports the branch back.

Run: python3 -m pytest test_dispatch_build_branch.py -q
"""
import mcp_dev


class _Resp:
    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b


def _capture(monkeypatch, reply):
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(json)
        return _Resp(reply)
    monkeypatch.setattr(mcp_dev.requests, "post", post)
    return sent


def test_no_branch_lets_the_dispatcher_choose(monkeypatch):
    sent = _capture(monkeypatch, {"ok": True, "summary": "s", "branch": "feat/add-x"})
    out = mcp_dev.dispatch_build_impl("p", "add x", "r1")
    assert "branch" not in sent
    assert out["branch"] == "feat/add-x"


def test_a_named_branch_or_the_opt_out_is_forwarded(monkeypatch):
    sent = _capture(monkeypatch, {"ok": True, "summary": "s", "branch": ""})
    mcp_dev.dispatch_build_impl("p", "tiny fix", "r1", branch="none")
    assert sent["branch"] == "none"
