"""NOMAD v2 · 3B — the Ads/Content MCP tool: `ads.save_content`.

Files approved marketing copy to the `content` table. The single gated Execute action of the
ads lane; runs only after the human gate approves.
"""
import datetime

from mcp.server.fastmcp import FastMCP
from nocodb import NocoDB

_db = NocoDB()
mcp = FastMCP("nomad-content")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save_content_impl(topic: str, content: str, run_id: str) -> dict:
    row = _db.create("content", {"topic": topic, "content": content, "run_id": run_id,
                                 "created_at": _now()})
    return {"ok": True, "content_id": row["Id"]}


@mcp.tool()
def save_content(topic: str, content: str, run_id: str) -> dict:
    """File approved content/copy to the content table for `run_id`. Returns {ok, content_id}."""
    return save_content_impl(topic, content, run_id)


if __name__ == "__main__":
    mcp.run()
