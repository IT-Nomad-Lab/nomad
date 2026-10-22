"""Model calls: one multimodal pass per item, one text pass for the batch report.

Vision goes through LiteLLM with fallbacks DISABLED and an explicit chain of image-capable
aliases. The gateway's normal fallbacks are wrong here: on 2026-09-22 a `gpt` image call failed and
LiteLLM quietly fell through to `balanced` (Sonnet via claude-bridge, text only), which answered
"no image was attached". A silent text-only fallback would produce a confident analysis of frames
it never saw, so every vision reply records the alias that actually served it.

Default chain: longdoc (Gemini 2.5 Flash, free tier, ~4 s for an image) → gpt (gpt-4o, metered).
`private` (qwen3.6, local, vision-capable) is left out by default: at 22 GB it cannot load while
ComfyUI + nomad-detect + nomad-stt hold the card, and the call just times out. Add it via
NOMAD_SOCIAL_VISION_MODELS when the GPU is free.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

LITELLM = os.environ.get("NOMAD_LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
KEY = os.environ.get("LITELLM_MASTER_KEY", "")
if not KEY:   # CLI runs outside systemd's EnvironmentFile: read the one key we need from .env
    try:
        for line in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                KEY = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
VISION_MODELS = [m.strip() for m in
                 os.environ.get("NOMAD_SOCIAL_VISION_MODELS", "longdoc,gpt").split(",") if m.strip()]
REPORT_MODEL = os.environ.get("NOMAD_SOCIAL_REPORT_MODEL", "balanced")
MAX_IMAGES = int(os.environ.get("NOMAD_SOCIAL_MAX_IMAGES", "20"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from prompts.registry import get_prompt
    JDE = get_prompt("JDE_STYLE")
except Exception:
    JDE = ""


def _chat(model: str, messages: list, max_tokens: int, json_mode: bool, no_fallback: bool,
          timeout: float = 300) -> tuple[str, str]:
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if no_fallback:
        body["disable_fallbacks"] = True
    r = httpx.post(f"{LITELLM}/v1/chat/completions", json=body, timeout=timeout,
                   headers={"Authorization": f"Bearer {KEY}"})
    r.raise_for_status()
    d = r.json()
    return d["choices"][0]["message"]["content"] or "", d.get("model", model)


def _json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# Local vision (qwen3.6 on Ollama) runs at num_ctx 8192, and a 720 px frame costs ~900 tokens:
# 12 frames measured 11,086 tokens and Ollama refused the request (2026-09-22). Raising num_ctx
# grows the KV cache past what the 5090 holds next to 22 GB of weights, so local calls get
# smaller frames, fewer of them, and trimmed text instead.
# `vision` (qwen3-vl-8b on llama.cpp :8241, #1046) has a 32k context and ~1 token per 32x32 px:
# 12 frames at 720 px are ~11k tokens, which leaves room for the full text and the reply. On
# overlay text it needs the pixels more than `private` does.
#   alias: (frame width, max frames, trim the text, max reply tokens)
LOCAL = {"private": (448, 8, True, 2500), "vision": (720, 12, False, 4000)}


def _img(path: str, width: int | None = None) -> dict:
    raw = Path(path).read_bytes()
    if width:
        from media import FFMPEG
        r = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-vf", f"scale={width}:-2",
                            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"],
                           capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            raw = r.stdout
    b = base64.b64encode(raw).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}}


ITEM_SCHEMA = """{
  "summary": "3-5 sentences: what this piece of content is and says",
  "content_format": "talking head | voiceover b-roll | tutorial | meme | carousel explainer | product showcase | vlog | ad | other",
  "topic": "short", "niche": "short",
  "language": "ISO code(s)",
  "hook": {"first_seconds_or_slide": "what the viewer sees and hears first", "technique": "question | bold claim | pattern interrupt | visual shock | curiosity gap | none", "strength_1_10": 0},
  "structure": [{"at": "0-3s or slide 1", "beat": "what happens"}],
  "on_screen_text": ["every legible overlay / slide text, verbatim"],
  "spoken_summary": "what is said (from the transcript), or null",
  "visual_style": "editing pace, framing, colour, typography, production level",
  "audio": "speech / trending music / original audio / silence, and its role",
  "people": ["who appears (role, not identity guesses)"],
  "brands_products": ["brands, products, apps, places named or shown"],
  "claims": [{"claim": "factual claim made", "checkable": true}],
  "cta": "explicit call to action, or null",
  "tone": "short", "target_audience": "short",
  "commercial_intent": "none | soft promo | affiliate | paid partnership | direct ad",
  "comment_sentiment": "positive / mixed / negative + what commenters react to (from the sample)",
  "why_it_works_or_not": "2-3 sentences, specific",
  "reusable_ideas": ["concrete tactics someone could reuse"],
  "red_flags": ["misleading claims, undisclosed ads, sensitive content; empty list if none"]
}"""


def analyse_item(item, processed: dict) -> dict:
    m = item.meta
    context = {
        "url": item.url, "kind": item.kind,
        "account": m.get("owner_username"), "account_name": m.get("owner_full_name"),
        "followers": (m.get("profile") or {}).get("followers"),
        "posted": m.get("date_utc"), "likes": m.get("likes"),
        "comments_count": m.get("comments_count"),
        "views_or_plays": m.get("video_plays") or m.get("video_views"),
        "engagement_rate_pct": m.get("engagement_rate_pct"),
        "caption": (m.get("caption") or "")[:4000], "hashtags": m.get("hashtags"),
        "mentions": m.get("mentions"), "tagged": m.get("tagged_users"),
        "coauthors": m.get("coauthors"), "location": m.get("location"),
        "paid_partnership": m.get("is_paid_partnership"), "sponsors": m.get("sponsors"),
        "audio_track": m.get("audio"), "alt_text": m.get("accessibility_caption"),
        "videos": processed["videos"],
    }
    transcript = "\n".join(
        f"[media {t['index']}, lang {t.get('language')}] " +
        " ".join(f"({s['start']}s) {s['text']}" for s in t.get("segments", []))
        for t in processed["transcripts"]) or "(no speech detected)"
    comments = "\n".join(f"- @{c.get('author')}: {c.get('text')}"
                         for c in item.comments[:40]) or "(none retrieved)"

    def build(model: str) -> tuple[list, int]:
        width, max_images, trim, _ = LOCAL.get(model, (None, MAX_IMAGES, False, 0))
        vis = processed["visuals"][:MAX_IMAGES]
        if len(vis) > max_images:   # keep the first frame, spread the rest
            step = (len(vis) - 1) / (max_images - 1)
            vis = [vis[round(i * step)] for i in range(max_images)]
        ctx = dict(context, caption=context["caption"][:1500]) if trim else context
        content = [{"type": "text", "text": (
            "You are analysing one Instagram " + item.kind + " for a content strategist. The "
            "images below are, in order: " + "; ".join(v["label"] for v in vis) + ".\n\n"
            "METADATA:\n" + json.dumps(ctx, ensure_ascii=False, default=str) + "\n\n"
            "TRANSCRIPT (local Whisper, timestamps in seconds):\n"
            + transcript[:2500 if trim else 12000] + "\n\n"
            "COMMENT SAMPLE:\n" + comments[:800 if trim else 6000] + "\n\n"
            "Describe only what is in the images, transcript, caption and comments. Never guess "
            "who a person is from their face. Write in English whatever the source language. "
            "No em-dashes. Return ONE JSON object with exactly this shape:\n" + ITEM_SCHEMA)}]
        content += [_img(v["path"], width) for v in vis]
        return content, len(vis)

    visuals = processed["visuals"]
    if not visuals:
        return {"error": "no media could be downloaded, nothing to look at", "model": None}
    errors = []
    for model in VISION_MODELS:
        # Gemini occasionally emits invalid JSON on long outputs (seen 2026-09-22: an unescaped
        # quote at char 5290, same reel parsed fine a run earlier). One cheap text-only repair
        # pass is far cheaper than re-sending 12-20 images.
        local = model in LOCAL
        content, n_sent = build(model)
        try:
            text, served = _chat(model, [{"role": "user", "content": content}],
                                 LOCAL[model][3] if local else 8000, json_mode=True,
                                 no_fallback=True, timeout=600 if local else 300)
        except Exception as e:
            errors.append(f"{model}: {str(e)[:200]}")
            continue
        for attempt in range(2):
            try:
                out = _json(text)
                out["model"] = served
                out["images_sent"] = n_sent
                return out
            except Exception as e:
                if attempt:
                    errors.append(f"{model}: unparseable JSON after repair ({str(e)[:120]})")
                    break
                try:
                    text, _ = _chat(model, [{"role": "user", "content":
                                    "Fix this so it is ONE valid JSON object. Change nothing "
                                    "else. Return only the JSON.\n\n" + text}],
                                    8000, json_mode=True, no_fallback=True)
                except Exception as e2:
                    errors.append(f"{model}: repair failed ({str(e2)[:120]})")
                    break
    return {"error": "every vision model failed: " + " | ".join(errors), "model": None}


def report(items: list[dict], focus: str) -> tuple[str, str]:
    """Cross-item synthesis. Input is the per-item analyses + metrics (text only, no images)."""
    compact = []
    for it in items:
        m, a = it["meta"], it.get("analysis", {})
        compact.append({
            "url": it["url"], "kind": it["kind"], "account": m.get("owner_username"),
            "followers": (m.get("profile") or {}).get("followers"), "posted": m.get("date_utc"),
            "likes": m.get("likes"), "comments": m.get("comments_count"),
            "plays": m.get("video_plays") or m.get("video_views"),
            "engagement_rate_pct": m.get("engagement_rate_pct"),
            "duration_s": m.get("video_duration_s"), "errors": it.get("errors"),
            "analysis": {k: v for k, v in a.items() if k not in ("images_sent",)},
        })
    prompt = (
        "Below are structured analyses of " + str(len(items)) + " Instagram items. Write the "
        "cross-item section of a report in Markdown.\n\n"
        "OPERATOR FOCUS: " + (focus or "general content analysis: what these are, what works, "
                              "what to learn from them") + "\n\n"
        "Sections, in order:\n"
        "## Bottom line  (3-5 sentences, the answer first)\n"
        "## Patterns across the set  (formats, hooks, topics, audio, posting, what repeats)\n"
        "## What performs  (tie claims to the numbers given; say when the sample is too small "
        "or metrics are missing, never invent a number)\n"
        "## Stand-outs  (best and weakest item, with the URL and why)\n"
        "## Recommendations  (concrete, tied to the focus)\n"
        "## Gaps  (what could not be retrieved or verified)\n\n"
        "Rules: use only the data given. Cite items by account + URL. No em-dashes.\n\n"
        + ("HOUSE STYLE (apply to the prose):\n" + JDE[:6000] + "\n\n" if JDE else "")
        + "DATA:\n" + json.dumps(compact, ensure_ascii=False, default=str)[:120000])
    text, served = _chat(REPORT_MODEL, [{"role": "user", "content": prompt}], 6000,
                         json_mode=False, no_fallback=False, timeout=600)
    return text.strip(), served
