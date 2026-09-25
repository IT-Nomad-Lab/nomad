"""nomad-social: Instagram links in, a report out.

  POST /analyze   {urls:[...], focus?, name?}  → {job_id}   (runs in the background)
  GET  /jobs/{id}                              → status, progress log, result (report path)
  GET  /jobs                                   → recent jobs
  GET  /health                                 → config + whether cookies (stories) are loaded

Jobs run one at a time on purpose: Instagram rate-limits anonymous clients per IP, and Gemini's
free tier is per-minute. A second batch queues behind the first.

Host-native, localhost only, alongside nomad-detect (8214). Port 8216.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import analyze
import fetch
import pipeline

HOST = os.environ.get("NOMAD_SOCIAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOMAD_SOCIAL_PORT", "8216"))

app = FastAPI(title="nomad-social", version="1.0")
JOBS: dict[str, dict] = {}
_q: "queue.Queue[str]" = queue.Queue()


class AnalyzeIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    focus: str = ""
    name: str = ""


def _worker():
    while True:
        jid = _q.get()
        job = JOBS[jid]
        job["status"] = "running"
        job["started"] = time.time()

        def say(msg, job=job):
            job["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")

        try:
            job["result"] = pipeline.run(job["urls"], job["focus"], job["name"], progress=say)
            job["status"] = "done"
        except Exception as e:
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        job["finished"] = time.time()


threading.Thread(target=_worker, daemon=True).start()


@app.post("/analyze")
def analyze_ep(req: AnalyzeIn):
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "status": "queued", "urls": req.urls, "focus": req.focus,
                 "name": req.name, "log": [], "queued": time.time(),
                 "position": _q.qsize() + 1}
    _q.put(jid)
    return {"job_id": jid, "status": "queued", "urls": len(req.urls)}


@app.get("/jobs/{jid}")
def job_ep(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "unknown job")
    j = JOBS[jid]
    return {k: v for k, v in j.items() if k != "urls"} | {"urls": len(j["urls"])}


@app.get("/jobs")
def jobs_ep():
    return [{"id": j["id"], "status": j["status"], "urls": len(j["urls"]), "name": j["name"],
             "report": (j.get("result") or {}).get("report")}
            for j in sorted(JOBS.values(), key=lambda j: -j["queued"])[:20]]


@app.get("/health")
def health():
    return {"ok": True, "service": "nomad-social", "cookies": fetch.has_cookies(),
            "vision_models": analyze.VISION_MODELS, "report_model": analyze.REPORT_MODEL,
            "out": str(pipeline.OUT), "queued": _q.qsize()}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
