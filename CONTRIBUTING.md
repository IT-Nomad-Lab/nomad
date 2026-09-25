# Contributing / Maintainer workflow

NOMAD lives in **two repositories**. Understanding which is which is the first rule of working
on it.

| Repo | Visibility | Role |
|---|---|---|
| `IT-Nomad-Lab/nomad-private` | private | **Source of truth.** The full working repo — real config, `docs/` (PRD/ADRs/roadmap), everything. Do all work here first. |
| `IT-Nomad-Lab/nomad` | public | **Sanitized downstream mirror.** Updated *after* private, only with vetted, secret-free content. |

**Golden rule:** private is always ahead. Change things in `~/nomad` (private) first, then port the
sanitized subset to public. Never edit the public repo directly with content that hasn't been
sanitized here.

## First-time setup (do this before your first commit)

```bash
cd ~/nomad
bash scripts/install-git-hooks.sh
```

This points `core.hooksPath` at `.githooks/`, which runs the secret scanner on every commit and
every push. **A fresh clone has no gate until you run it.** Git does not ship hooks with a clone,
so this is not optional and nothing will remind you.

The scanner is `.claude/hooks/secret_scan.py`, used from two places so the layers cannot drift: the
git hooks, and `guard.py`, which stops an agent before git even runs. It prefers `gitleaks` if it is
on `PATH` and falls back to a built-in ruleset. `.gitleaks.toml` adds four rules that gitleaks 8.28
does not ship: two Anthropic shapes, a Notion token and an OpenAI key. Those are not arbitrary
picks. An Anthropic key and a Notion token are the two credentials this repo has actually leaked.

Detection is regex-based. A credential that is split across lines or base64-wrapped still gets
through, so the hook is a backstop, not a reason to stop reading your own diff.

## Everyday flow

```bash
# 1. work in the private working repo
cd ~/nomad
git checkout -b feat/<thing>        # branch for non-trivial work
# ...make changes, test...
git commit && git push origin <branch or master>
```

Secrets never get committed anywhere: real values live only in `.env` (git-ignored). The committed
`.env.example` and the n8n workflow JSONs carry **placeholders only**.

## Porting a change to the public mirror

The public repo (`~/nomad-public`) is a **curated, sanitized snapshot** — not a full clone. Only
port genuine improvements (features, docs), and re-sanitize every time.

1. Copy only the changed **source/doc** files from `~/nomad` into `~/nomad-public`.
   Do **not** blindly overwrite files that were sanitized (see the checklist) — apply just your
   change and keep the sanitized form.
2. Run the sanitization sweep (below). It must come back clean.
3. Commit in `~/nomad-public` and `git push origin main`.

### Sanitization checklist (must be clean before pushing public)

Run from `~/nomad-public`:

```bash
grep -rI "/home/jdelimam"          .  --exclude-dir=.git    # -> use ${HOME} / relative paths
grep -rI "itnomadlab.com"          .  --exclude-dir=.git    # -> you@example.com (email removed)
grep -rIE "63e20555|xidYNnAY9GQTR97t|VXhPJzMAoEDfQh0Y" . --exclude-dir=.git   # internal Notion/n8n IDs -> placeholders
grep -rI  -e Civis -e RuneMatch -e OpenClaw -e ArduPilot . --exclude-dir=.git # personal project names -> generic
grep -rIE "PRIVATE KEY|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|ntn_[A-Za-z0-9]{20,}" . --exclude-dir=.git  # live keys
```

What the public mirror intentionally **excludes** (kept private):

- `docs/` — PRD, PLAN, ROADMAP, RISK-REGISTER, ADRs (gitignored in public).
- `n8n/approval-gate-v2.backup.json` — redundant, identity-leaking backup.
- The detailed `CLAUDE.md` — public gets a clean, stripped rewrite; private keeps the full
  working-memory version.

What gets rewritten/sanitized in public (never overwrite these from private without re-applying):

- `docker-compose.yml` — host paths become `${HOME}`.
- `.mcp.json` — absolute paths become relative.
- `n8n/*.json` — emails, credential IDs, and the Notion `database_id` become `YOUR_*` placeholders.
- `nomad-console/server.py`, `sync_projects.py`, `crew/dev_crew.py`, `v2/engine.py` — runtime
  identity defaults and example project names genericized.

Author attribution (the name in `LICENSE`, `plugin.json`, `marketplace.json`, architecture doc) is
kept on purpose — it's authorship, not a secret.

## Projects that consume NOMAD

Other repos opt in via a `Nomad.md` marker (name/status/lane/stack + a capabilities section). They
**use** the platform read-only; they don't modify it. When the platform gains a capability, refresh
the `Nomad.md` markers in the consumer repos.
