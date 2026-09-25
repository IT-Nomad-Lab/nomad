# nomad-watch

The thing that notices a service has stopped doing its job.

## Why it exists

Three outages got through NOMAD, one after another, and every one of them was the same shape: an
absence of a signal, not a wrong one.

- The n8n approval gate suppressed **every** approval for three and a half months. Nothing errored.
- A run sat in `new` for two months with nobody waiting on it.
- The relay logged `Invalid API key` in a tight loop while `systemctl` reported it `active`, so
  approvals stopped reaching the phone and nothing anywhere said so.

All three were found by hand. Two now have their own backstop, but the general case was open: the
relay was the only thing watching, and nothing watched the relay.

## What it is

A oneshot fired by a systemd timer every five minutes. One pass probes every target, compares the
result with the previous pass, and posts **changes** to Zulip `#alerts`, topic `watchdog`.

Two design choices carry most of the weight.

**It does not run forever.** A long-lived watchdog that wedges reports `active` while watching
nothing, which would make it the fourth instance of the bug it exists to catch. A oneshot either
finishes or is killed by `TimeoutStartSec` and lands in `failed`, and `systemctl --user
list-timers` shows whether the timer is still firing.

**Reachable is not the same as working.** A port answering says the process is up, not that it is
doing anything. So the targets that matter most are checked by staleness: the relay touches a file
after each successful sync, the engine reports `sweep_age_s` on `/health`, and the alarm is the
number not moving. That is the only kind of check that would have caught the relay.

It posts with its own Zulip client rather than through the relay, because the relay is one of the
things it watches.

## Noise

An alert nobody reads is the same as no alert, so:

- Alerts fire on a **change**, not on a condition. A service that stays down stays one message.
- A target must fail `NOMAD_WATCH_CONFIRM` passes in a row (default 2, so about ten minutes) before
  it counts as down. A restart costs nothing.
- Recoveries are posted, with how long it was down.
- One **all-clear a day** at `NOMAD_WATCH_DIGEST_HOUR`, so a quiet `#alerts` means healthy rather
  than dead.
- Every target carries a `hint`, printed inside the alert. An alert with no next step is noise, and
  the tests enforce that every shipped target has one.

## Targets

`targets.json`. Four probe types:

| type | checks | use it when |
|---|---|---|
| `http` | a URL answers; optionally `status_ok`, `method`, `body`, `expect`, or `json_field`/`json_max` | the service has an endpoint |
| `heartbeat` | a file's mtime is younger than `max_age_s` | reachable would not prove it is working |
| `unit` | `systemctl --user is-active` | host-native services |
| `container` | `docker inspect`, and a crash loop counts as down | compose services |

`enabled: false` mutes a target without losing the note about why it was there.

Two targets are worth knowing about specifically:

- **n8n gate webhook** is probed with `POST` and treats `404` as down. n8n registers a webhook for
  one verb, so a `GET` answers 404 whether the workflow is live or deactivated. A deactivated gate
  is the three-and-a-half-month outage; an unauthenticated POST is rejected by n8n's header-auth
  credential before any node runs, so this creates no executions.
- **engine stall sweep** reads `sweep_age_s` off the engine's `/health`. The container staying up
  says nothing about whether the thread that fails abandoned runs is still alive.

## Running it

```bash
cd ~/nomad/nomad-watch
../nomad-relay/.venv/bin/python watch.py --dry-run   # probe and print, post nothing
../nomad-relay/.venv/bin/python watch.py --list      # last known state per target
../nomad-relay/.venv/bin/python watch.py             # one real pass
```

It shares the relay's venv: the same two dependencies, one thing to keep patched.

Install:

```bash
cp nomad-watch.service nomad-watch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nomad-watch.timer
systemctl --user list-timers nomad-watch.timer
```

Tests: `python3 -m pytest nomad-watch/test_watch.py -q`

## What watches the watchdog

The timer, and the daily all-clear. If `#alerts` goes quiet for more than a day, the watchdog is
what died. That is the honest limit of everything running on one box, and it is written here so
nobody assumes otherwise.
