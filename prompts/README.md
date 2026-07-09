# prompts/ — the NOMAD prompt registry

Named, reusable prompt blocks. One flat namespace: `<NAME>.md` is addressable as `<NAME>`. Any
agent calls it by name instead of copy-pasting the text into six different system prompts.

| Name | What it is |
|---|---|
| `JDE_STYLE` | The house writing voice (Josias De Lima's style). Applied to any agent that writes prose for a human. |

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
