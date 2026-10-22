# Agent-docs rubric

Adapted from claude-md-management's `quality-criteria.md` (Anthropic, Apache-2.0), with NOMAD's
own rules added. Score each doc out of 100 and grade it: A 90+, B 75+, C 60+, D 40+, F below.

## General criteria

| # | criterion | points | full marks when |
|---|---|---|---|
| 1 | Commands and workflows | 20 | build, test and run commands are present and can be pasted as written |
| 2 | Accurate map | 20 | key directories, entry points and how the parts talk to each other; every path named exists |
| 3 | Gotchas | 15 | the things that bite are written down, each with why and what to do instead |
| 4 | Conciseness | 15 | every line earns its place; nothing a reader would get from the code in ten seconds |
| 5 | Currency | 15 | ports, services, versions and file references match what is running today |
| 6 | Actionability | 15 | concrete steps, real paths, no "consider" or "maybe" |

Red flags, each worth a finding on its own: a command that would fail, a reference to a deleted file,
a TODO nobody finished, template text left uncustomised, and the same fact in two docs.

## NOMAD rules

These come from how NOMAD keeps its docs. A breach is a finding even if the doc scores well.

1. **Do not restate `docs/SDLC.md`.** The autonomy tiers, work sizes, branch rule and tracking rule
   live there and only there. A doc may name a rule in one line and link SDLC.md; a paragraph that
   re-explains the tiers will drift from the source.
2. **CLAUDE.md is a map of what is true now, not a changelog.** Dated narratives ("on 2026-09-23 we
   moved X") belong in `docs/HISTORY.md` or `docs/DECISIONS.md`. A date is fine when it says how
   fresh a fact is ("verified live 2026-09-23"). Struck-through rows for retired services should go
   once nothing depends on them.
3. **Nomad.md holds per-project metadata: name, status, lane, stack.** It points at
   `~/nomad/docs/SHARED-RESOURCES.md` for shared MCP tools and services and does not copy them.
4. **Size budget: 4 KB** for CLAUDE.md, AGENTS.md and Nomad.md (`checks.py size`). Each loads into
   every session. Over budget, move detail into a linked doc and leave a one-line pointer.
5. **Memory is not documentation.** A memory that repeats what a repo's CLAUDE.md or git history
   already records should be dropped; a memory that names a path that no longer exists is stale
   (`checks.py memory`).
6. **Prose follows JDE_STYLE** (`~/nomad/prompts/JDE_STYLE.md`): plain words, no em-dashes.

## Assessment process

1. Run `checks.py` and note every mechanical finding.
2. Read each doc in full. Cross-check with the repo: do the named paths exist, do the commands match
   the scripts, is the port table what `docker ps` and `systemctl --user` show?
3. Score the six criteria, then check the NOMAD rules.
4. Write one finding per issue: `file:line`, what is wrong, the evidence, the proposed change.
