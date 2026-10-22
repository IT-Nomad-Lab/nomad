"""nomad-detect — does this text read as machine-written? Answered on your own hardware.

Two independent detectors behind one HTTP call, plus an opt-in third:

  binoculars   a real statistical detector (two local LMs on the GPU, no training, no network).
               Says how machine-shaped the text is. See binoculars.py for the method.
  style        a deterministic linter (regex + distribution stats, no model). Says WHICH lines
               give it away and how to fix each one. Enforces prompts/JDE_STYLE.md at the same
               time, so passing means in-voice, not just undetected.
  external     GPTZero / Originality / Sapling, only when a key is set and the caller asks.

The service is the gate for anything written in JDE_STYLE: draft, detect, rewrite the findings,
detect again. Nothing ships on "looks fine to me".

  POST /detect       {text, external?, providers?}  → full verdict (GPU, ~1 s)
  POST /lint         {text}                         → style only (no GPU, instant)
  POST /calibrate    {samples:[{text,label,source?}], save?} → fit + persist thresholds for this pair
  GET  /thresholds   current thresholds and whether they were calibrated
  GET  /health

Host-native on the RTX GPU, alongside nomad-voice (8200), nomad-scraper (8210), nomad-stt (8212).
"""
import os
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

import external
import style

HOST = os.environ.get("NOMAD_DETECT_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOMAD_DETECT_PORT", "8214"))
EAGER = os.environ.get("NOMAD_DETECT_EAGER_LOAD", "0") == "1"

app = FastAPI(title="nomad-detect", version="1.0")


class DetectIn(BaseModel):
    text: str
    external: bool = False
    providers: list[str] | None = None


class LintIn(BaseModel):
    text: str


class Sample(BaseModel):
    text: str
    label: Literal["human", "ai"]
    # Which model wrote it ("opus", "gpt", "fast", …). Optional, but without it the fit cannot
    # report auc_by_source — the per-model breakdown every verdict's caveat is built from.
    source: str | None = None


class CalibrateIn(BaseModel):
    samples: list[Sample] = Field(min_length=4)
    save: bool = True


def _thresholds() -> dict[str, Any]:
    import binoculars
    return binoculars.thresholds()


def _verdict(bino: dict[str, Any], sty: dict[str, Any]) -> dict[str, Any]:
    """One answer from two detectors. They disagree often, and the disagreement is the signal:
    clean statistics with a filthy style report means the writing is human-shaped but reads
    like a template, which is still what gets a piece flagged by a human editor."""
    b = bino.get("band") if bino.get("ok") else None
    s = sty["band"]
    if b == "machine" or s == "machine":
        v, why = "fail", "rewrite before shipping"
    elif b == "borderline" or s == "borderline":
        v, why = "revise", "fix the findings, then run it again"
    elif b is None:
        v, why = "revise", "too short for a statistical read; style only"
    else:
        v, why = "pass", "reads human on both detectors"
    out = {"verdict": v, "guidance": why, "statistical_band": b, "style_band": s,
           "passes": v == "pass"}
    # A text too short to score carries no thresholds of its own; read them from disk rather than
    # telling the caller the service is uncalibrated when it is not.
    t = bino.get("thresholds") or _thresholds()
    if not t.get("calibrated"):
        out["caveat"] = ("statistical thresholds are uncalibrated defaults — run calibrate.py on "
                         "your own writing before trusting the band")
    elif t.get("blind_to"):
        both = t.get("blind_to_both") or []
        covered = [b for b in t["blind_to"] if b not in both]
        parts = []
        if covered:
            parts.append("this model pair could not separate " + ", ".join(covered) +
                         " from human writing at calibration; trust the style channel for those")
        if both:
            # Neither channel separates these. Saying "trust the style channel" here would be
            # worse than silence: text drafted to pass this linter cannot be judged by it.
            parts.append("NEITHER channel separates " + ", ".join(both) + " from human writing; "
                         "a pass on that kind of text means nothing, only a human read does")
        out["caveat"] = ". ".join(parts)
    return out


@app.get("/health")
def health():
    import binoculars
    loaded = binoculars._engine is not None
    return {"ok": True, "service": "nomad-detect", "models_loaded": loaded,
            "pair": f"{binoculars.OBSERVER}|{binoculars.PERFORMER}",
            "device": binoculars.DEVICE, "thresholds": binoculars.thresholds(),
            "external_providers": external.available()}


@app.get("/thresholds")
def get_thresholds():
    import binoculars
    return binoculars.thresholds()


@app.post("/lint")
def lint(inp: LintIn):
    """Style only. No model touched, so it is safe to call on every keystroke if you want."""
    return {"ok": True, "style": style.lint(inp.text)}


@app.post("/detect")
def detect(inp: DetectIn):
    import binoculars
    text = inp.text.strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    sty = style.lint(text)
    try:
        with binoculars.admitted():
            bino = binoculars.engine().score(text)
    except Exception as e:                       # a dead GPU must not block the style report
        bino = {"ok": False, "error": str(e)[:300]}
    ext = external.check(text, inp.providers) if inp.external else {}
    top = [f for f in sty["findings"] if f["severity"] == "high"][:8] or sty["findings"][:5]
    return {"ok": True, **_verdict(bino, sty), "binoculars": bino, "style": sty,
            "external": ext,
            "rewrite_hints": [{"line": f["line"], "rule": f["rule"], "excerpt": f["excerpt"],
                               "fix": f["fix"]} for f in top]}


@app.post("/calibrate")
def calibrate(inp: CalibrateIn):
    """Fit the two thresholds to THIS model pair using labeled samples you supply.

    The published Binoculars thresholds belong to the Falcon-7B pair and to the paper's test
    corpus. Yours are different on both counts. Feed it your own writing (label human) and the
    same prompts run through the models you actually use (label ai) and the numbers stop being
    a guess. Twenty of each is enough to be useful; more is better."""
    import calibrate as cal
    return cal.fit([s.model_dump() for s in inp.samples], save=inp.save)


if __name__ == "__main__":
    if EAGER:
        import binoculars
        if binoculars.DEVICE.startswith("cuda"):     # loads per request under admission (#1462)
            print("[nomad-detect] eager load skipped on cuda", flush=True)
        else:
            binoculars.engine()
    import importlib.util   # dispatcher/bridge_bind.py, by path: loopback + NOMAD_BRIDGE_IP (#383)
    spec = importlib.util.spec_from_file_location("bridge_bind", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "dispatcher", "bridge_bind.py"))
    bridge_bind = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_bind)
    bridge_bind.serve_uvicorn(app, HOST, PORT, log_level="info")
