"""Build and run the NOMAD crew.

Every agent points at the LiteLLM gateway and asks for a ROLE alias (deep,
balanced, fast, code, longdoc, private) — never a raw model name. Swap models
in litellm/config.yaml only.
"""
import os

import yaml
from crewai import Agent, Crew, LLM, Process, Task

from tools import (
    create_task,
    generate_image,
    log_activity,
    request_approval,
    request_send_email,
    request_github_merge,
    request_calendar_event,
    request_drive_share,
    save_knowledge,
    update_task_status,
)

HERE = os.path.dirname(__file__)
LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

# Which tools each agent may use.
TOOLS = {
    "orchestrator": [create_task, update_task_status, log_activity, request_approval, save_knowledge],
    "planner": [create_task, log_activity],
    "researcher": [save_knowledge, log_activity],
    "builder": [update_task_status, request_approval, request_github_merge, log_activity],
    "writer": [generate_image, request_approval, log_activity],
    "comms": [request_send_email, request_calendar_event, request_drive_share, request_approval, log_activity],
    "reviewer": [update_task_status, log_activity],
}


def _llm(alias: str) -> LLM:
    """An LLM bound to a LiteLLM role alias via the gateway's OpenAI API."""
    return LLM(
        model=f"openai/{alias}",      # LiteLLM serves aliases on the OpenAI route
        base_url=f"{LITELLM_BASE}/v1",
        api_key=LITELLM_KEY,
    )


def _load(name):
    with open(os.path.join(HERE, name)) as f:
        return yaml.safe_load(f)


def build_agents():
    specs = _load("agents.yaml")
    agents = {}
    for key, spec in specs.items():
        agents[key] = Agent(
            role=spec["role"],
            goal=spec["goal"],
            backstory=spec["backstory"],
            llm=_llm(spec["llm"]),
            tools=TOOLS.get(key, []),
            allow_delegation=spec.get("allow_delegation", False),
            verbose=True,
        )
    return agents


def build_tasks(agents, ctx):
    specs = _load("tasks.yaml")
    tasks = []
    for spec in specs.values():
        tasks.append(Task(
            description=spec["description"].format(**ctx),
            expected_output=spec["expected_output"].format(**ctx),
            agent=agents[spec["agent"]],
        ))
    return tasks


def run_milestone(goal: str, criteria: str, milestone: str) -> str:
    """Plan → execute → review one milestone. Returns the crew's final output."""
    ctx = {"goal": goal, "criteria": criteria, "milestone": milestone}
    agents = build_agents()
    tasks = build_tasks(agents, ctx)
    crew = Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,
        manager_agent=agents["orchestrator"],
        verbose=True,
    )
    result = crew.kickoff()
    return str(result)


if __name__ == "__main__":
    print(run_milestone(
        goal="Launch the landing page",
        criteria="Live, mobile-friendly, passes SEO check",
        milestone="Draft copy and hero image",
    ))
