# prompts/ — the NOMAD prompt registry

Named, reusable prompt blocks. One flat namespace: `<NAME>.md` is addressable as `<NAME>`. Any
agent calls it by name instead of copy-pasting the text into six different system prompts.

| Name | What it is |
|---|---|
| `JDE_STYLE` | The house writing voice (Josias De Lima's style). Applied to any agent that writes prose for a human. |
| `CRITIC_MODE` | Confidence, named assumptions, failure conditions — for any agent that plans, routes, reviews, or recommends. |
| `DESIGN_TASTE` | UI/interaction taste rules — for any agent that builds or reviews a screen. Load before calling a UI change done. |

## Use it

```python
from prompts import get_prompt
style = get_prompt("JDE_STYLE")     # returns the block; "" if missing (fail-open)
```

Non-package consumers (the crew and v2 containers) read the same files directly, pointed at the
mounted directory via `NOMAD_PROMPTS_DIR` (compose sets it to `/app/prompts`).

## Add a block

Drop a markdown file here. No code change. `available()` lists what's registered.

## Where it's wired

- **crew** — agents with a `style: JDE_STYLE` key in `crew/agents.yaml` get the block appended to
  their backstory (Writer and Comms today).
- **v2 engine** — lanes with a `style` key in `v2/specialists.py` LANES get it appended to their
  Skill (ads and comms today).
- **LiteLLM** — `litellm/config.yaml` points here; the gateway routes models, the registry owns
  prompts. Keep prompts out of the routing config.
- **nomad-detect** — the block's "Detection pass" section is ENFORCED, not just advised. Lanes with
  a `style` key run their draft through the detector (`v2/detector.py` → `nomad-detect:8214`),
  rewrite once against their own findings, and carry the report into the human gate. Check any
  draft by hand with `nomad-detect/check.py`, or from inside Claude Code with the `detect_ai` MCP
  tool.
- **frontend-ux / dev crew's frontend_engineer + qa_engineer** — `DESIGN_TASTE` is referenced in
  `.claude/agents/frontend-ux.md` and `crew/dev_team.yaml`. Pair it with the Playwright UI check
  (`dispatcher/ui_check.sh`, callable via the crew's `run_check` tool or the dispatcher's
  `POST /verify`) so a UI change is judged on the rendered screen, not just the diff.
