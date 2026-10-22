# claude-config

One Claude Code `settings.json` baseline for both hosts (claude-parity scope 6, #1053).

| file | holds |
|---|---|
| `baseline.json` | shared permissions, the deny list, `autoMode.soft_deny`, the official plugins plus `nomad-operator`, `agentPushNotifEnabled` |
| `wsl.json` | WSL-only rules and `additionalDirectories`; retires the old `install.sh` hooks |
| `windows.json` | `PowerShell(...)` mirrors of the git rules; retires the `ntfy.ps1` hooks |
| `sync_settings.py` | merges baseline + overlay into a host's `settings.json` |
| `fixtures/` | copies of both hosts' real settings, env values redacted, for the tests |

Each JSON file has `set` (a settings fragment: objects merge per key, lists gain missing items,
scalars are overwritten) and `retire` (substrings: hook commands and permission rules containing
one are removed, but only once `nomad-operator@nomad-marketplace` is installed).

Anything `set` doesn't name is left alone. Host-only rules, `env`, `theme`, `autoMode.environment`
stay as they are. `nomad-sync.json` next to `settings.json` records what the last run set, so an
item dropped from here is removed on the next run, and only if this tool put it there.

## Run (the operator's step)

```bash
# WSL
python3 ~/nomad/claude-config/sync_settings.py --dry-run     # read-only, values masked
python3 ~/nomad/claude-config/sync_settings.py

# Windows, from Git Bash (runs in WSL through the launcher)
bash nomad-operator/bin/nomad-run sync-settings --host windows \
  --settings "$USERPROFILE/.claude/settings.json" --dry-run
```

It writes only on a change, after a `settings.json.bak-<time>` copy, so a second run prints
`no change`. Run it again after the plugin install to retire the old hooks.

Tests: `uv run --no-project --with pytest python -m pytest -q claude-config`.
