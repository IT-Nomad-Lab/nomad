"""NOMAD v2 · 2A-3 — the Researcher's MCP tool: `research.save_brief`.

FastMCP server exposing the single gated Execute action for the research lane: file an
approved brief to the `knowledge` table. The engine's Execute calls `save_brief_impl`; agents
call it over MCP. Runs only after the human gate approves.
"""
import datetime

from mcp.server.fastmcp import FastMCP
from nocodb import NocoDB

_db = NocoDB()
mcp = FastMCP("nomad-research")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save_brief_impl(topic: str, brief: str, run_id: str) -> dict:
    """File a research brief to the knowledge base. The single gated Execute action (research)."""
    row = _db.create("knowledge", {"topic": topic, "brief": brief, "run_id": run_id,
                                   "created_at": _now()})
    return {"ok": True, "knowledge_id": row["Id"]}


@mcp.tool()
def save_brief(topic: str, brief: str, run_id: str) -> dict:
    """File an approved research brief to the Knowledge base for `run_id`. Returns {ok, knowledge_id}.
    Runs only after the human gate approves."""
    return save_brief_impl(topic, brief, run_id)


if __name__ == "__main__":
    mcp.run()
