"""Open WebUI sessions guards (#1119, docs/prd/owui-sessions.md N2, N3, §11).

- The router's LOCAL set and its local fallback lists name only models that run on this machine
  (Ollama, or vLLM on the host). The Claude/Codex bridges have local URLs but reach the cloud, so
  a URL alone doesn't make a model local.
- Every LiteLLM alias is classified, so a new one can't slip into a chat unexamined.
- `gpt` is in no router set, override or fallback list (N2, with test_no_paid_fallback.py).
- Open WebUI: image pinned by digest (the pipe is written against 0.12.3), signup off, host-only.
- The Project dropdown is a read-only mount, and both functions are pinned to the image's release
  (§6.5).

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "v2"))

import owui_router as R  # noqa: E402

LOCAL_BACKEND = re.compile(r"^(ollama|ollama_chat|hosted_vllm)/")
LOCAL_HOST = re.compile(r"^https?://(host\.docker\.internal|localhost|127\.0\.0\.1|172\.17\.0\.1)"
                        r":(11434|8240|8241)\b")
# In LiteLLM but never offered by the router: local fallback targets, and the free NIM tier.
# picked by hand in Open WebUI's model list, never by the router (#1408 added the last three)
NOT_ROUTED_LOCAL = {"code-ollama", "vision-ollama", "devstral", "glm-flash", "gpt-oss"}
NOT_ROUTED = NOT_ROUTED_LOCAL | {"nim", "nim-vision"}


def _models():
    with open(os.path.join(ROOT, "litellm", "config.yaml"), encoding="utf-8") as f:
        return {m["model_name"]: m["litellm_params"] for m in yaml.safe_load(f)["model_list"]}


def _is_local(params):
    return bool(LOCAL_BACKEND.match(params.get("model", ""))
                and LOCAL_HOST.match(params.get("api_base") or ""))


def test_every_litellm_alias_is_classified():
    known = set(R.LOCAL) | set(R.CLOUD) | set(R.PAID) | NOT_ROUTED
    assert set(_models()) <= known, f"classify in owui_router: {set(_models()) - known}"
    assert set(R.LOCAL) | set(R.CLOUD) <= set(_models())


def test_hand_picked_local_aliases_really_are_local():
    """A cloud model can't hide among the hand-picked local ones (#1413)."""
    models = _models()
    for alias in NOT_ROUTED_LOCAL:
        assert _is_local(models[alias]), f"{alias} is not on this machine: {models[alias]}"


def test_local_aliases_and_local_fallbacks_really_are_local():
    models = _models()
    for alias in set(R.LOCAL) | {t for chain in R.LOCAL_FALLBACKS.values() for t in chain}:
        assert _is_local(models[alias]), f"{alias} is not on this machine: {models[alias]}"
    assert set(R.LOCAL_FALLBACKS) == set(R.LOCAL)


def test_cloud_aliases_are_not_marked_local():
    models = _models()
    for alias in R.CLOUD + R.PAID:
        assert not _is_local(models[alias]), alias


def test_gpt_is_unreachable_from_the_router():
    assert "gpt" in R.PAID
    assert "gpt" not in R.LOCAL + R.CLOUD
    assert "gpt" not in set(R.OVERRIDES) | set(R.OVERRIDES.values())
    assert all("gpt" not in chain for chain in R.LOCAL_FALLBACKS.values())


def _openwebui():
    with open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["services"]["openwebui"]


def test_open_webui_is_pinned_signup_off_and_host_only():
    svc = _openwebui()
    assert re.search(r"@sha256:[0-9a-f]{64}$", svc["image"]), "pin Open WebUI by digest"
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert env.get("ENABLE_SIGNUP") == "false"
    assert all(p.startswith("127.0.0.1:") for p in svc["ports"])


def test_the_project_dropdown_is_read_only_and_the_functions_match_the_pin():
    svc = _openwebui()
    assert "./owui/nomad_ui.js:/app/build/static/loader.js:ro" in svc["volumes"]
    assert os.path.isfile(os.path.join(ROOT, "owui", "nomad_ui.js"))
    for name in ("nomad_pipe.py", "nomad_filter.py"):
        with open(os.path.join(ROOT, "owui", name), encoding="utf-8") as f:
            assert "required_open_webui_version: 0.12.3" in f.read(), name
