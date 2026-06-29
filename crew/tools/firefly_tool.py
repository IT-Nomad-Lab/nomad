"""Adobe Firefly image generation (server-to-server OAuth).

GUARDRAIL: Firefly calls spend API credits, so by default this tool does NOT
spend. Unless NOMAD_FIREFLY_AUTOSPEND=true, it routes the request through the
approval gate and returns without generating. Flip the env var only once you've
decided automatic image spend is acceptable.

⚠ Verify the OAuth and generate endpoint paths/payloads against the current
  Firefly API docs (see CLAUDE.md open items) — they version over time.
"""
import os

import requests
from crewai.tools import tool

from .notion_tools import request_approval

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FIREFLY_GENERATE_URL = "https://firefly-api.adobe.io/v3/images/generate"  # verify


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
    resp.raise_for_status()
    return resp.json()["access_token"]


@tool("generate_image")
def generate_image(prompt: str, requested_by: str = "Writer") -> str:
    """Generate an image from a text prompt via Adobe Firefly.

    SPENDS CREDITS. Unless NOMAD_FIREFLY_AUTOSPEND=true this does not generate —
    it files a 'Spend' approval and returns. On approval, n8n (or a re-run with
    autospend on) performs the generation."""
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
