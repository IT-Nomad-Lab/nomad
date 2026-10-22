"""A project's `legacy-<project>` policy, built from its #1389 baseline (policies/baseline/<id>.json).

PRD 6.5: tagging a project first snapshots what it has now into a project policy so the
evaluator reproduces today's configuration. This covers what the evaluator decides: the MCP
servers the project reaches and the autopilot settings. The permission rendering and the
project's other keys are the compiler's business (nomad_policy/compiler.py), not the evaluator's.

The MCP servers come as one allow statement per channel (schema.CHANNELS): gateway grants,
the local-model set and the repo .mcp.json are three separate grants today, and the compiler
writes each one from its own channel, so a server reached one way does not spread to the others.

Returns the raw mapping, so it goes through schema.validate like any policy file.
"""

# The repo's .mcp.json entry for the MCP gateway (mcp-gateway/README.md). It is the connection,
# not a server: what it gives the project is mcp.gateway.servers, already counted.
GATEWAY_ENTRY = "mcp-gateway"

SETTINGS = ("cap", "model", "max_budget_usd", "deny_edit_globs", "pre_merge_gate", "milestone_gate")


def _step(cmd):
    """A snapshot Command ({run, locks, env}) back to the policy gate-step form."""
    out = {"run": cmd["run"]}
    for k in ("locks", "env"):
        if cmd.get(k):
            out[k] = cmd[k]
    return out


def channel_servers(baseline):
    """{channel: sorted server names} the baseline reaches, empty channels left out."""
    mcp = baseline.get("mcp") or {}
    by = {"gateway": set((mcp.get("gateway") or {}).get("servers") or []),
          "local": set(mcp.get("local_model_set") or []),
          "repo": set(mcp.get("repo_mcp_json") or []) - {GATEWAY_ENTRY}}
    return {c: sorted(v) for c, v in by.items() if v}


def legacy_policy(baseline):
    raw: dict = {"id": f"legacy-{baseline['id']}", "role": "permission",
                 "description": f"Snapshot of {baseline['id']} before tagging (#1389 baseline)."}
    statements = [{"sid": f"legacy-mcp-{c}", "effect": "allow", "kind": "mcp", "match": names,
                   "when": {"channel": [c]}}
                  for c, names in channel_servers(baseline).items()]
    if statements:
        raw["statements"] = statements
    ap = baseline.get("autopilot")
    settings = {}
    if ap:
        for key in SETTINGS:
            v = ap.get(key)
            if key in ("pre_merge_gate", "milestone_gate"):
                v = [_step(c) for c in v or []]
            if v not in (None, "", [], ()):
                settings[key] = v
    if settings:
        raw["settings"] = settings
    return raw
