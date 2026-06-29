"""HTTP trigger for the crew so n8n (or curl) can launch a milestone run.

  POST /run-milestone  {"goal": "...", "criteria": "...", "milestone": "..."}
"""
from fastapi import FastAPI
from pydantic import BaseModel

import threading

from crew import run_milestone
from dev_crew import run_feature, run_backlog
from tools import fetch_backlog

app = FastAPI(title="NOMAD Crew API")


class MilestoneRequest(BaseModel):
    goal: str
    criteria: str
    milestone: str


class FeatureRequest(BaseModel):
    goal: str
    project: str


class BacklogRequest(BaseModel):
    limit: int = 3


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-milestone")
def run(req: MilestoneRequest):
    output = run_milestone(req.goal, req.criteria, req.milestone)
    return {"result": output}


@app.post("/run-dev")
def run_dev(req: FeatureRequest):
    """Engineering crew: design → build (uncommitted) → test → review one feature."""
    output = run_feature(req.goal, req.project)
    return {"result": output}


@app.post("/run-backlog")
def run_backlog_ep(req: BacklogRequest):
    """Self-development loop: process up to `limit` queued Tasks in the background.
    Returns immediately with how many are queued; progress shows in the Notion Mission
    Log + task statuses. Approved work lands uncommitted for the human to commit."""
    queued = len(fetch_backlog(limit=req.limit))
    if queued == 0:
        return {"started": False, "queued": 0,
                "hint": "Add Tasks rows: Assigned Agent='Engineering Crew', Status='Backlog', "
                        "title like '[NOMAD] <goal>'."}
    threading.Thread(target=run_backlog, args=(req.limit,), daemon=True).start()
    return {"started": True, "queued": queued, "limit": req.limit}
