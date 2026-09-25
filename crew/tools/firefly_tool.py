"""Adobe Firefly image generation (server-to-server OAuth).

STATUS: never run. No Adobe credentials have ever existed on this machine, so the OAuth and
generate endpoint paths below are unverified against the live API and the response shape is a
guess. The tool is kept because the operator intends to add a key; until then it must fail in a
way that says so. An agent that gets `KeyError: 'ADOBE_CLIENT_ID'` back reports "the image tool
crashed", which is the wrong problem to go looking for.

Image generation in NOMAD normally goes local through ComfyUI. This is the commercially-safe
licensing path, for work that needs it.

GUARDRAIL: Firefly calls spend API credits, so by default this tool does NOT spend. Unless
NOMAD_FIREFLY_AUTOSPEND=true, it routes the request through the approval gate and returns without
generating. Flip the env var only once you've decided automatic image spend is acceptable.

⚠ Verify the OAuth and generate endpoint paths/payloads against the current Firefly API docs
  (see CLAUDE.md open items) the first time this runs with a real key — they version over time.
"""
import os

import requests
from crewai.tools import tool

from .notion_tools import request_approval

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FIREFLY_GENERATE_URL = "https://firefly-api.adobe.io/v3/images/generate"  # verify

REQUIRED_ENV = ("ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET")


def missing_credentials() -> str:
    """'' when Firefly could run, otherwise a sentence an agent can repeat verbatim to the
    operator. Checked before anything else so the answer is 'not set up', never a stack trace."""
    missing = [n for n in REQUIRED_ENV if not os.environ.get(n)]
    if not missing:
        return ""
    verb = "is" if len(missing) == 1 else "are"
    return (f"Adobe Firefly is not configured: {' and '.join(missing)} {verb} absent from .env. "
            f"No image was generated and nothing was spent. Use the local ComfyUI image tool "
            f"instead, or ask the operator to add an Adobe key.")


def _access_token() -> str:
    cid = os.environ["ADOBE_CLIENT_ID"]
    secret = os.environ["ADOBE_CLIENT_SECRET"]
    scopes = os.environ.get("ADOBE_FIREFLY_SCOPES", "openid,AdobeID,firefly_api,ff_apis")
    resp = requests.post(IMS_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
        "scope": scopes,
    }, timeout=30)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Adobe rejected the credentials ({resp.status_code}) — the key is set "
                           f"but wrong, or lacks the firefly_api scope")
    resp.raise_for_status()
    return resp.json()["access_token"]


@tool("generate_image")
def generate_image(prompt: str, requested_by: str = "Writer") -> str:
    """Generate an image from a text prompt via Adobe Firefly.

    SPENDS CREDITS. Unless NOMAD_FIREFLY_AUTOSPEND=true this does not generate —
    it files a 'Spend' approval and returns. On approval, n8n (or a re-run with
    autospend on) performs the generation."""
    # Before the gate, not after: filing a Spend approval for a tool that cannot possibly run
    # wastes the operator's attention on a decision with no outcome.
    blocked = missing_credentials()
    if blocked:
        return blocked

    autospend = os.environ.get("NOMAD_FIREFLY_AUTOSPEND", "false").lower() == "true"
    if not autospend:
        return request_approval.func(
            action=f"Generate Firefly image: {prompt[:120]}",
            action_type="Spend",
            context=f"Adobe Firefly image generation. Prompt: {prompt}",
            requested_by=requested_by,
        )

    token = _access_token()
    resp = requests.post(
        FIREFLY_GENERATE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "x-api-key": os.environ["ADOBE_CLIENT_ID"],
            "Content-Type": "application/json",
        },
        json={"prompt": prompt, "numVariations": 1},  # verify payload shape
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    # Response shape varies by API version — adjust to current docs.
    try:
        url = data["outputs"][0]["image"]["url"]
    except (KeyError, IndexError):
        return f"Generated, but could not parse image URL from response: {data}"
    return url
