"""The local image provider runs ComfyUI only through the dispatcher's admission gate (#1462).

Run: python3 -m pytest test_mcp_image_comfy.py -q
"""
import base64

import pytest

import mcp_image


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


class _Calls(list):
    reply = None


@pytest.fixture
def sent(monkeypatch):
    calls = _Calls()

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return calls.reply

    def get(*a, **k):
        raise AssertionError("the engine must not talk to ComfyUI directly")

    calls.reply = _Resp({"ok": True, "png_b64": base64.b64encode(b"PNG").decode()})
    monkeypatch.setattr(mcp_image.requests, "post", post)
    monkeypatch.setattr(mcp_image.requests, "get", get)
    return calls


def test_comfyui_goes_through_the_dispatcher_gate_with_the_token(sent):
    assert mcp_image._comfyui("a red fox") == b"PNG"
    (call,) = sent
    assert call["url"] == f"{mcp_image.DISPATCH_URL}/comfy/generate"
    assert call["headers"] == {"X-Nomad-Token": mcp_image.DISPATCH_TOKEN}
    assert call["json"]["timeout"] == mcp_image.COMFYUI_TIMEOUT
    assert call["json"]["graph"] and "8188" not in call["url"]
    assert call["timeout"] >= 2 * mcp_image.COMFYUI_TIMEOUT + 150   # gate queue + run + settle


def test_a_style_timeout_is_passed_to_the_gate(sent):
    mcp_image._comfyui("x", {"timeout": 900})
    assert sent[0]["json"]["timeout"] == 900 and sent[0]["timeout"] >= 2 * 900 + 150


@pytest.mark.parametrize("reply,match", [
    (_Resp({"ok": False, "busy": True, "error": "gpu_guard holds admission"}), "GPU busy: gpu_guard"),
    (_Resp({"ok": False, "error": "ComfyUI did not finish within 300s"}), "did not finish"),
    (_Resp({"error": "not found"}, 404), "not found"),
    (_Resp(ValueError("html"), 502), "HTTP 502"),
])
def test_a_refusal_raises_so_the_chain_reports_it(sent, reply, match):
    sent.reply = reply
    with pytest.raises(RuntimeError, match=match):
        mcp_image._comfyui("x")


def test_busy_in_a_forced_comfyui_run_fails_without_a_paid_fallback(sent, monkeypatch):
    sent.reply = _Resp({"ok": False, "busy": True, "error": "restore is pending"})
    monkeypatch.setitem(mcp_image._PROVIDERS, "openai", lambda p: pytest.fail("paid fallback"))
    out = mcp_image.generate_image_impl("x", "direct-1", "comfyui")
    assert out["ok"] is False and "GPU busy: restore is pending" in out["error"]
