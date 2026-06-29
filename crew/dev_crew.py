"""NOMAD Engineering Crew — the autonomous team that develops & tests NOMAD itself.

Given a feature goal + target project, the crew runs:
    Lead Architect (design + work packages)
      → Engineers (dispatch_build into the repo — UNCOMMITTED edits)
      → QA (prove it works)
      → Reviewer (check the diff vs. conventions; approve for human commit)

Agents reason via LiteLLM role aliases; actual file edits go through the dispatcher
(Claude Code in the repo). Nothing is committed or pushed — the human reviews the
working tree. Triggered via crew/server.py  POST /run-dev.
"""
import os
import re

import requests
import yaml
from crewai import Agent, Crew, LLM, Process, Task

from tools.dev_tools import DISPATCH

from tools import (
    dispatch_plan,
    dispatch_build,
    list_dev_projects,
    run_check,
    log_activity,
    save_knowledge,
    request_approval,
    write_activity,
    write_knowledge,
    fetch_backlog,
    set_task_status_by_id,
)

HERE = os.path.dirname(__file__)
LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

# Tools per engineering role.
DEV_TOOLS = {
    "lead_architect": [list_dev_projects, dispatch_plan, run_check, log_activity, save_knowledge, request_approval],
    "backend_engineer": [dispatch_plan, dispatch_build, run_check, log_activity],
    "frontend_engineer": [dispatch_plan, dispatch_build, run_check, log_activity],
    "integration_engineer": [dispatch_plan, dispatch_build, run_check, log_activity],
    "qa_engineer": [dispatch_plan, run_check, log_activity],
    "reviewer": [run_check, log_activity, save_knowledge],
}


def _llm(alias: str) -> LLM:
    return LLM(model=f"openai/{alias}", base_url=f"{LITELLM_BASE}/v1", api_key=LITELLM_KEY)


def _load(name):
    with open(os.path.join(HERE, name)) as f:
        return yaml.safe_load(f)


def build_dev_agents():
    specs = _load("dev_team.yaml")
    agents = {}
    for key, spec in specs.items():
        agents[key] = Agent(
            role=spec["role"], goal=spec["goal"], backstory=spec["backstory"],
            llm=_llm(spec["llm"]), tools=DEV_TOOLS.get(key, []),
            allow_delegation=spec.get("allow_delegation", False), verbose=True,
        )
    return agents


def _build_tasks(agents, ctx):
    design = Task(
        description=(
            "Feature goal: \"{goal}\"\nTarget project/repo: \"{project}\"\n\n"
            "Design the change and break it into an ordered list of concrete work "
            "packages, each assigned to backend / frontend / integration. Note "
            "dependencies and what 'done' means for each. Keep changes small and "
            "reversible. If a design decision is genuinely the operator's, say so."
        ).format(**ctx),
        expected_output="A short technical design + an ordered, owner-tagged work-package list.",
        agent=agents["lead_architect"],
    )
    implement = Task(
        description=(
            "Implement the work packages for \"{goal}\" in project \"{project}\". For "
            "each package, call dispatch_build(project=\"{project}\", task=<a precise, "
            "self-contained instruction>) and confirm the returned diff matches intent. "
            "Edits are UNCOMMITTED on purpose. Log each build with log_activity. Do NOT "
            "attempt to commit or push."
        ).format(**ctx),
        expected_output="Each package built (with changed-file lists), or a clear blocker per package.",
        agent=agents["backend_engineer"],
    )
    return [design, implement]


def _review_tasks(agents, ctx):
    # ctx carries {goal, project, evidence} — the evidence is REAL, pre-run by the system.
    test = Task(
        description=(
            "Assess whether the change for \"{goal}\" in \"{project}\" works, using the "
            "AUTHORITATIVE VERIFICATION EVIDENCE below (already executed by the system via the "
            "dispatcher — you do NOT need to re-run it, though you may via run_check):\n\n"
            "=== VERIFICATION EVIDENCE (real, deterministic) ===\n{evidence}\n=== END EVIDENCE ===\n\n"
            "From this evidence, judge per acceptance point: is the change additive/scoped "
            "(git diff), does it parse/compile, and is the intended behavior present? This is a "
            "PRE-COMMIT gate — static + structural proof is the bar; live HTTP smoke is post-deploy. "
            "Report a concrete pass/fail per point, citing the evidence."
        ).format(**ctx),
        expected_output="A pass/fail per acceptance point, each citing a line of the verification evidence.",
        agent=agents["qa_engineer"],
    )
    review = Task(
        description=(
            "Decide the verdict for \"{goal}\" in \"{project}\" using QA's assessment and the "
            "AUTHORITATIVE VERIFICATION EVIDENCE below (real, already executed):\n\n"
            "=== VERIFICATION EVIDENCE ===\n{evidence}\n=== END EVIDENCE ===\n\n"
            "Check NOMAD conventions (secrets only in .env, role aliases not raw models, "
            "localhost-first, fail-open). Decide:\n"
            "  • APPROVE-FOR-COMMIT when the evidence shows the change is additive/scoped "
            "(git diff shows only intended files), compiles clean (PY_COMPILE_OK / rc=0), and the "
            "intended behavior is present in the diff. Live-HTTP smoke is NOT required pre-commit.\n"
            "  • CHANGES-REQUESTED ONLY when the evidence shows a concrete failure (cite the exact "
            "diff line or non-zero rc). Do NOT block for missing proof — the evidence above is "
            "authoritative; a tool error in your own session is not grounds to block.\n"
            "The human makes the final commit decision on the uncommitted tree. State the verdict "
            "literally as 'APPROVE-FOR-COMMIT' or 'CHANGES-REQUESTED' and save a one-paragraph "
            "summary to Knowledge."
        ).format(**ctx),
        expected_output="The literal verdict APPROVE-FOR-COMMIT or CHANGES-REQUESTED + a Knowledge summary.",
        agent=agents["reviewer"],
    )
    return [test, review]


def resolve_project(name):
    """Resolve a backlog item's [Project] tag against the dispatcher's Nomad.md registry.
    Returns {name, lane, repo} for the matched project, or a bare {name} fallback if the
    dispatcher is unreachable or the tag matches nothing (run_feature will surface a clear
    'Unknown project' error in that case). Lets operators tag tasks loosely — `[MyApp]`,
    `[myapp-poc]`, `[myapp]` all map to the right repo."""
    try:
        detail = requests.get(f"{DISPATCH}/projects", timeout=10).json().get("detail", [])
    except requests.RequestException:
        return {"name": name, "lane": "", "repo": ""}
    q = (name or "").strip().lower()
    for r in detail:
        slug = r["repo"].split("/")[-1].lower() if r.get("repo") else ""
        cands = {r["name"].lower(), slug, slug.split("-")[0]} - {""}
        if q == r["name"].lower() or q in cands or any(c and c in q for c in cands):
            return r
    return {"name": name, "lane": "", "repo": ""}


def _verify(project, command):
    """Run a verification command via the dispatcher and return a compact transcript."""
    try:
        r = requests.post(f"{DISPATCH}/verify",
                          json={"project": project, "command": command}, timeout=160)
        d = r.json()
    except requests.RequestException as e:
        return f"$ {command}\n[dispatcher error] {e}"
    if not d.get("ok"):
        return f"$ {command}\n[refused] {d.get('error')}"
    out = (d.get("stdout") or "").strip()
    err = (d.get("stderr") or "").strip()
    return f"$ {command}\n[rc={d.get('rc')}] {out[:1400]}" + (f"\n[stderr] {err[:300]}" if err else "")


def gather_evidence(project):
    """Deterministically collect the proof the reviewer needs — independent of whether the
    agents successfully call run_check. This is the reliability fix for the QA gate."""
    return "\n\n".join(_verify(project, c) for c in (
        "git diff --stat",
        "git diff",
        "git diff --name-only | grep '[.]py$' | xargs -r python3 -m py_compile && echo PY_COMPILE_OK",
    ))


def run_feature(goal: str, project: str) -> str:
    """Design → build → [deterministic verify] → test → review one feature in a repo.
    Edits land uncommitted; commit/push remain human-gated."""
    ctx = {"goal": goal, "project": project}
    write_activity("Engineering Crew", f"Dev cycle started: {goal}", f"Target repo: {project}")
    agents = build_dev_agents()

    # Phase 1 — design + build (agents edit the repo, uncommitted).
    build_crew = Crew(agents=[agents["lead_architect"], agents["backend_engineer"],
                              agents["frontend_engineer"], agents["integration_engineer"]],
                      tasks=_build_tasks(agents, ctx), process=Process.sequential, verbose=True)
    build_out = str(build_crew.kickoff())

    # Phase 1.5 — gather REAL verification evidence (not dependent on agent tool-calls).
    evidence = gather_evidence(project)

    # Phase 2 — test + review against the authoritative evidence.
    rctx = {**ctx, "evidence": evidence}
    review_crew = Crew(agents=[agents["qa_engineer"], agents["reviewer"]],
                       tasks=_review_tasks(agents, rctx), process=Process.sequential, verbose=True)
    review_out = str(review_crew.kickoff())

    result = f"{build_out}\n\n=== VERIFICATION EVIDENCE ===\n{evidence}\n\n=== REVIEW ===\n{review_out}"
    write_activity("Engineering Crew", f"Dev cycle complete: {goal}",
                   f"Target repo: {project}. See Knowledge for the review verdict.")
    write_knowledge(f"Dev cycle verdict — {goal}", result, kind="Decision",
                    source="crew/dev_crew.py run_feature")
    return result


def run_backlog(limit: int = 3, agent: str = "Engineering Crew") -> dict:
    """Self-development loop: pull queued tasks from Notion and run a full cycle on each.

    A backlog item is a Tasks row (Status=Backlog, Assigned Agent=Engineering Crew) whose
    title optionally carries a `[Project]` prefix (defaults to NOMAD). For each item:
    Backlog → In Progress → run_feature(goal, project) → Review (if APPROVE-FOR-COMMIT) or
    Blocked. Edits land UNCOMMITTED; the human reviews + commits. Never commits or pushes.
    """
    items = fetch_backlog(agent, limit)
    if not items:
        write_activity("Engineering Crew", "Backlog check: nothing queued")
        return {"processed": 0, "results": []}
    write_activity("Engineering Crew", f"Backlog run starting: {len(items)} task(s)")
    results = []
    for it in items:
        title = it["title"]
        m = re.match(r"^\s*\[(.+?)\]\s*(.+)$", title)
        tag = m.group(1).strip() if m else "NOMAD"
        goal = (m.group(2) if m else title).strip()
        proj = resolve_project(tag)                  # loose tag → real repo (+ lane)
        project, lane = proj["name"], proj.get("lane", "")
        set_task_status_by_id(it["id"], "In Progress")
        try:
            out = run_feature(goal, project)
            approved = "APPROVE-FOR-COMMIT" in out
            set_task_status_by_id(it["id"], "Review" if approved else "Blocked")
            results.append({"task": title, "project": project, "lane": lane,
                            "verdict": "APPROVED" if approved else "CHANGES-REQUESTED"})
        except Exception as e:
            set_task_status_by_id(it["id"], "Blocked")
            write_activity("Engineering Crew", f"Backlog ERROR: {title}", str(e)[:300])
            results.append({"task": title, "project": project, "lane": lane,
                            "verdict": f"ERROR: {str(e)[:120]}"})
    approved = sum(1 for r in results if r["verdict"] == "APPROVED")
    write_activity("Engineering Crew",
                   f"Backlog run complete: {approved}/{len(results)} approved (queued for human commit)")
    return {"processed": len(results), "approved": approved, "results": results}


if __name__ == "__main__":
    print(run_feature(goal="Add a /version endpoint that returns the build date",
                      project="nomad"))
