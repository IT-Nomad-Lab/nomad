#!/usr/bin/env python3
"""Generate docs/MCP-REGISTRY.md: every MCP server NOMAD has, who gets it, and how it is reached.

The server list comes from the registry scripts/mcp-sync.py already builds (~/nomad/.mcp.json and
~/.claude.json), the servers the repo's plugins ship (PLUGINS, #1412), plus the gateway servers in
META below. A registered server with no entry in META
fails the run, so a new MCP cannot exist without a tier and an owner (PRD docs/prd/mcp-gateway.md).

  mcp-registry.py            write docs/MCP-REGISTRY.md
  mcp-registry.py --check    exit 1 when the file is out of date or a server has no entry
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "docs", "MCP-REGISTRY.md")

# name: (tier, network, approval, via, owner, what)
# tier: default | opt-in | service-owned | project-only | production | governance
META = {
    "nomad-pm": ("default", "internal", "none", "plugin nomad-operator (nomad-run); direct in ~/nomad", "nomad-pm", "work packages in OpenProject"),
    "nomad-comms": ("default", "external", "gate (refuses an unapproved run)", "plugin nomad (nomad-run)", "nomad", "v2 comms lane: send an approved email via n8n + Gmail"),
    "nomad-research": ("default", "internal", "gate (refuses an unapproved run)", "plugin nomad (nomad-run)", "nomad", "v2 research lane: file an approved brief in NocoDB"),
    "nomad-content": ("default", "internal", "gate (refuses an unapproved run)", "plugin nomad (nomad-run)", "nomad", "v2 ads lane: file approved copy in NocoDB"),
    "nomad-dev": ("default", "internal", "gate (refuses an unapproved run)", "plugin nomad (nomad-run)", "nomad", "v2 dev lane: dispatch an approved build to the Builder"),
    "nomad-create": ("governance", "internal", "per call", "direct", "nomad", "set up a new project from an approved PRD (#1374)"),
    "nomad-local": ("default", "internal", "none", "direct", "nomad", "delegate to a local model"),
    "nomad-scrape": ("default", "internal", "none", "direct", "nomad", "grounded web search and scrape"),
    "nomad-detect": ("default", "internal", "none", "direct", "nomad", "AI-text detection and style lint"),
    "nomad-diagram": ("default", "internal", "none", "direct", "nomad", "Mermaid/Graphviz/D2/Vega-Lite"),
    "penpot": ("default", "internal", "bot user", "separate service", "penpot", "Penpot design canvas (per-bot credentials)"),
    "penpot-agent1": ("default", "internal", "bot user", "separate service", "penpot", "Penpot canvas, second bot identity"),
    "nomad-penpot-nav": ("default", "internal", "bot user", "separate service", "penpot", "move the Penpot tab between files"),
    "nomad-penpot-nav-agent1": ("default", "internal", "bot user", "separate service", "penpot", "same, second bot identity"),
    "nomad-image": ("service-owned", "internal", "none", "direct", "nomad-video", "local image generation (GPU)"),
    "nomad-audio": ("service-owned", "internal", "none", "direct", "nomad-video", "local music and SFX (GPU)"),
    "nomad-codex-image": ("service-owned", "external", "none", "direct", "nomad-video", "image generation on ChatGPT quota"),
    "elevenlabs": ("service-owned", "external", "per call", "direct (OAuth)", "nomad-video", "speech, voices, video (spends credits)"),
    "nomad-social": ("opt-in", "external", "gate", "direct", "nomad", "Instagram/social analysis"),
    "supabase": ("opt-in", "external", "per call", "direct (OAuth)", "per project", "Supabase projects and docs"),
    "gmail": ("project-only", "external", "none", "gateway (Google proxy + vault)", "per project", "Gmail read and draft, via the vault-backed Google proxy (#1289)"),
    "gdrive": ("project-only", "external", "none", "gateway (Google proxy + vault)", "per project", "Drive read and file, via the vault-backed Google proxy (#1289)"),
    "gcalendar": ("project-only", "external", "none", "gateway (Google proxy + vault)", "per project", "Calendar read and free/busy, via the vault-backed Google proxy (#1289)"),
    "figma": ("project-only", "external", "none", "gateway (token wrapper)", "per project", "Figma design data (read), on request"),
    "alpaca": ("project-only", "external", "read-only toolsets", "gateway", "crypto-trading-bot", "Alpaca paper trading data"),
    "hostinger-billing": ("production", "external", "per call", "direct", "nomad", "Hostinger billing"),
    "hostinger-dns": ("production", "external", "per call", "direct", "nomad", "Hostinger DNS"),
    "hostinger-domains": ("production", "external", "per call", "direct", "nomad", "Hostinger domains"),
    "hostinger-ecommerce": ("production", "external", "per call", "direct", "nomad", "Hostinger ecommerce"),
    "hostinger-hosting": ("production", "external", "per call", "direct", "nomad", "Hostinger hosting"),
    "hostinger-reach": ("production", "external", "per call", "direct", "nomad", "Hostinger Reach (broken, #1266)"),
    "hostinger-vps": ("production", "external", "per call", "direct", "nomad", "Hostinger VPS"),
}
# plugins in this repo whose MCP servers every session on both hosts gets (#1056)
PLUGINS = ["nomad-operator", "nomad-plugin"]
# not in the Claude/Codex registry: reached only through the gateway or claude.ai
EXTRA = ["alpaca", "gmail", "gdrive", "gcalendar"]
CLAUDE_AI = [("Gmail", "read/draft"), ("Google Calendar", "read"), ("Google Drive", "read")]
ORDER = ["default", "service-owned", "opt-in", "project-only", "production", "governance"]
TITLE = {
    "default": "Default: every project gets these",
    "service-owned": "Owned by a service (media): used through nomad-video, not loaded by each project",
    "opt-in": "Opt-in: granted to a project on request",
    "project-only": "Project-only: granted to named projects, never by default",
    "production": "Production-changing: nomad and deploy projects, approval per call",
    "governance": "Governance",
}


def registered():
    spec = importlib.util.spec_from_file_location("mcp_sync", os.path.join(HERE, "mcp-sync.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return sorted(set(m.registry()) | set(plugin_servers()))


def plugin_servers():
    names = []
    for p in PLUGINS:
        try:
            with open(os.path.join(HERE, "..", p, ".mcp.json")) as f:
                names += list(json.load(f).get("mcpServers") or {})
        except (OSError, ValueError):
            continue
    return names


def render(names):
    unknown = [n for n in names if n not in META]
    if unknown:
        raise SystemExit("MCP servers with no tier in scripts/mcp-registry.py META: " + ", ".join(unknown))
    allnames = sorted(set(names) | set(EXTRA))
    out = ["# NOMAD MCP registry", "",
           "Generated by `scripts/mcp-registry.py`. Do not edit by hand. This is the full list of MCP",
           "servers NOMAD has, whether or not a session loaded them. A project gets the default tier",
           "plus what its gateway key grants (`docs/prd/mcp-project-map.md`). To request another one,",
           "ask the operator; the grant is a key change in the gateway, not a copy of the server.", "",
           f"{len(allnames)} servers.", ""]
    for tier in ORDER:
        rows = [n for n in allnames if META[n][0] == tier]
        if not rows:
            continue
        out += [f"## {TITLE[tier]}", "", "| server | network | approval | reached | owner | use |", "|---|---|---|---|---|---|"]
        for n in rows:
            _, net, appr, via, owner, what = META[n]
            out.append(f"| `{n}` | {net} | {appr} | {via} | {owner} | {what} |")
        out.append("")
    out += ["## Claude Code only (claude.ai connectors, tied to the operator's account)", "",
            "| connector | access |", "|---|---|"]
    out += [f"| {n} | {a} |" for n, a in CLAUDE_AI]
    out += ["", "Codex and the local model need their own Google OAuth client for these:",
            "`docs/prd/mcp-google-oauth-guide.md`.", ""]
    return "\n".join(out)


def main(argv):
    text = render(registered())
    if "--check" in argv:
        try:
            same = open(OUT).read() == text
        except OSError:
            same = False
        print("MCP-REGISTRY.md " + ("up to date" if same else "out of date"))
        return 0 if same else 1
    with open(OUT, "w") as f:
        f.write(text)
    print("wrote", os.path.normpath(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
