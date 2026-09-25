"""NOMAD social bridge (MCP): hand over Instagram links, get a report back.

Wraps the local nomad-social service (:8216). A batch runs in the background there, one item
at a time (download → frames + Whisper transcript → vision analysis), then a cross-item report.
Roughly 45-90 s per reel, so this tool waits up to `wait_seconds` and otherwise hands back a job
id for instagram_job.

Tools:
  analyze_instagram(urls, focus, name, wait_seconds)   start a batch, wait, return the report
  instagram_job(job_id)                                status / report of a running batch
"""
import os
import time
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("NOMAD_SOCIAL_URL", "http://127.0.0.1:8216").rstrip("/")

mcp = FastMCP("nomad-social")


def _finish(job: dict) -> str:
    res = job.get("result") or {}
    if job["status"] == "failed":
        return f"[batch failed: {job.get('error')}]\n" + "\n".join(job.get("log", [])[-10:])
    report = res.get("report")
    head = (f"DONE: {res.get('analysed')}/{res.get('total')} analysed · report: {report}\n"
            f"folder: {res.get('dir')} (items.json, metrics.csv, per-item media/frames/"
            f"transcript/raw.json)\n")
    for f in res.get("failed") or []:
        head += f"  not analysed: {f['url']} ({f['why'][:200]})\n"
    try:
        return head + "\n" + Path(report).read_text(encoding="utf-8")
    except Exception:
        return head


@mcp.tool()
def analyze_instagram(urls: list[str], focus: str = "", name: str = "",
                      wait_seconds: int = 900) -> str:
    """Analyse Instagram posts, reels and carousels from their links. For every link it pulls
    the full public metadata (caption, hashtags, likes, comments, followers, engagement rate,
    location, tags, audio, paid-partnership flag, a comment sample), downloads the media,
    transcribes speech locally with Whisper (any language), extracts keyframes, and has a vision
    model describe hook, structure, on-screen text, visual style, CTA, claims and what makes it
    work. Then it writes a cross-item report answering `focus` (e.g. "competitor hooks in
    fitness reels"). Stories need a logged-in cookies file on the service; profiles only get
    follower counts. Returns the full Markdown report, or a job id if it is still running."""
    try:
        r = requests.post(f"{BASE}/analyze", json={"urls": urls, "focus": focus, "name": name},
                          timeout=30)
        r.raise_for_status()
        jid = r.json()["job_id"]
    except Exception as e:
        return f"[nomad-social unreachable at {BASE}: {str(e)[:200]}]"
    deadline = time.time() + max(0, wait_seconds)
    while time.time() < deadline:
        time.sleep(5)
        try:
            job = requests.get(f"{BASE}/jobs/{jid}", timeout=15).json()
        except Exception:
            continue
        if job["status"] in ("done", "failed"):
            return _finish(job)
    return (f"still running (job {jid}); call instagram_job('{jid}') to collect the report.")


@mcp.tool()
def instagram_job(job_id: str) -> str:
    """Status of an analyze_instagram batch: progress log while running, the report when done."""
    try:
        job = requests.get(f"{BASE}/jobs/{job_id}", timeout=15).json()
    except Exception as e:
        return f"[nomad-social unreachable: {str(e)[:200]}]"
    if "status" not in job:
        return f"[unknown job {job_id}]"
    if job["status"] in ("done", "failed"):
        return _finish(job)
    return f"{job['status']}:\n" + "\n".join(job.get("log", [])[-15:])


if __name__ == "__main__":
    mcp.run()
