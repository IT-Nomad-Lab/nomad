# Autopilot alerts (#878, PRD 5.7, R8, AC3)

`autopilot/alerts.py` sends one alert per event to three places and remembers what it sent, so the
same condition is never sent twice on any channel.

| Channel | How | Needs |
|---|---|---|
| cockpit | `StateStore.event(pid, "alert", title)`: the row's last event | the supervisor's store |
| Zulip `#autopilot`, topic = project id | Zulip REST `POST /api/v1/messages` with the relay/nomad-watch bot | `~/.config/nomad/zulip-bot.zuliprc` (`NOMAD_ZULIPRC`) |
| email to the operator | n8n `v2-send` webhook (Gmail Send), `X-Nomad-Token` header, as in `v2/mcp_comms.py` | `OPERATOR_EMAIL`, `N8N_WEBHOOK_TOKEN` (or `NOMAD_N8N_WEBHOOK_TOKEN`), optional `N8N_V2_SEND_URL` |

A channel without its config is skipped with one warning; the others still send. The values are
read from the environment, then from `NOMAD_ENV_FILE` or `<repo>/.env` for anything unset.
`NOMAD_AUTOPILOT_ALERT_CHANNELS=cockpit` limits the channels; `NOMAD_AUTOPILOT_ALERTS_DRY_RUN=1`
logs Zulip and email alerts instead of sending them.

## Events and dedup keys

| Event | Key | Re-armed when |
|---|---|---|
| task ends `needs_operator` / `failed` | `task:<pid>:<task>:<outcome>` | a worker starts the task again |
| project enters `needs_you` / `budget_paused` / `error` | `state:<pid>:<state>` | the project leaves that state |
| Feature completes | `feature:<pid>:<feature>` | never |
| anything else (#879 digest) | caller's key, e.g. `digest:<pid>:<date>` | never (key per day) |

The ledger (`~/.local/state/nomad-autopilot/alerts.json`, `NOMAD_AUTOPILOT_ALERTS`) records per key
which channels delivered. A failed channel is retried on the next call for the same condition, up
to 5 times, without re-sending to the others. The whole check-send-record runs under a file lock,
so two supervisors cannot both send. Sends never raise into the pass.

## Supervisor hook

Build once, next to the store:

```python
import alerts
alerter = alerts.from_env(store)
```

At the end of `run_pass`, after `store.finish_pass(pid, state, detail, workers)`:

```python
alerter.on_pass(pid, finished=finished, started=started, state=state, detail=detail)
```

`finished` is what `collect()` returned (`{"task", "outcome", "detail"?}`), `started` what
`start_workers()` returned. In `run_all`'s `except` branch, where the project is set to `error`:

```python
alerter.project_state(pid, "error", f"pass failed: {type(e).__name__}: {e}")
```

When the scheduler closes a Feature: `alerter.feature_completed(pid, feature_id, title)`.
The daily digest (#879): `alerter.notify(pid, f"digest:{pid}:{date}", "daily digest", text)`.
