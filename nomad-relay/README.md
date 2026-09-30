# nomad-relay

NOMAD's activity in Zulip, and your reactions and DMs back into NOMAD. Design: [ADR-008](../docs/adr/ADR-008-zulip-operator-channel.md).

| Channel | Push | What lands there |
|---|---|---|
| `#gate` | 🔔 | A run waiting for approval: action, goal, every argument, the full body, AI-text check. React ✅ / ❌ or reply `approve` / `reject`. The topic is marked resolved once the run moves on. |
| `#alerts` | 🔔 | Failed runs (react 🔁 to retry), blocked project tasks (🔁 retry, ⏭️ skip), services down or recovered, GPU hot. OpenProject work packages that turn Blocked, and @mentions of you there. |
| `#runs` | | One topic per run, every status change. |
| `#projects` | | One topic per project, progress as tasks finish. One `wp <id>` topic per OpenProject work package you watch: created, status changes, comments. |
| `#digest` | | Summaries at 07:30 and 19:00 (America/New_York). |

DM the NOMAD bot with `help` for commands. Anything that isn't a command goes to NOMAD's chat,
including `capture: <goal>` and `project: <title>`.

### T2.1: morning briefing / Friday review (read-only)
`briefing` and `friday review` (DM only) report calendar, mail that looks like it needs a reply,
open tasks by project, and what's waiting at the gate; `friday review` adds what ran this week.
It never drafts, sends, replies, labels, archives or accepts an invite — it only reads. Calendar
and mail come from a dedicated n8n workflow (`n8n/briefing-data.workflow.json`, id
`nomadbriefingdata`) that reuses the **existing** Gmail/Calendar OAuth2 credentials already
attached to `nomadgatev2clean` — no new credential, no OAuth flow, no Google token outside n8n.
A section that fails to load says so by name (`⚠ Mail: did not load — <error>`); it never renders
as "nothing today".

`briefing`/`friday review` always work as an on-demand, DM-only dry run. The **scheduled** post to
`#digest` only switches from the plain status summary to this enriched briefing once
`NOMAD_RELAY_BRIEFING_ARMED=1` (`nomad-relay.service`) — read a dry run, check it against your own
calendar/mail, and only then arm it. Armed, the 07:30 slot posts the morning briefing and the
19:00 slot posts the Friday review on Fridays (any other evening keeps the plain summary).

⚠ **Known limitation, not fixed by this change:** the Gmail OAuth2 credential n8n uses
(`xidYNnAY9GQTR97t`, "Gmail account") is currently broken — real Google OAuth calls against it
return `Client authentication failed`. The mail section reports that error honestly rather than
"nothing today"; it will keep doing so until the credential is reconnected in the n8n UI
(Credentials → Gmail account → Reconnect). This is the *existing* credential, not a new one.

Topics are ids only (`run 3f2a9c1d0b7e`, `project 8`, `wp 112`), so nothing from a proposal shows
on a lock screen.

### OpenProject (PM-01 P4)
OpenProject's signed webhook goes to the v2 engine, not here, because the engine holds the
OpenProject tokens and the relay holds none. The relay reads the engine's `/pm-events` whenever the
engine's event stream wakes it, and places each event:

- a work package you watch, or one that @mentions you → its `wp <id>` topic in `#projects`;
- a move into Blocked → `#alerts`, once per item, shared with the engine's own blocked-task alert;
- an @mention of you → `#alerts` as well, unless the same change already alerted as Blocked.

Anything you typed yourself in OpenProject is skipped. A message you type in a `wp <id>` topic
becomes a comment on that work package, posted by nomad-manager under an "Operator, via Zulip"
byline, and the relay reacts ✅ when it lands. Comment text shows in a code block, so a comment
cannot mention anyone or format itself in Zulip. Expect about a minute of lag: OpenProject
aggregates a work package's changes for a minute before it fires. An engine older than P4 has no
`/pm-events`, and the relay skips it quietly.

The briefing adds a **Board** section from the engine's `/pm-summary`: open work by status, blocked
items with what blocks them, and on Fridays what agents and people closed that week.

## What Zulip can and can't do
- **Only your account steers.** Your numeric user id is pinned in `~/.config/nomad/relay.env`. If any
  other active human account exists in the org, the relay acts on nothing and tells you why.
- **Approving from the phone** works only on a #gate message this relay posted, when all of these hold:
  - the action is in `NOMAD_RELAY_REMOTE_ACTIONS` (default `send_message`, `save_brief`,
    `save_content`, `generate_image`; `dispatch_build` is cockpit-only);
  - nothing in the proposal had to be cut to fit the message;
  - the message is unedited;
  - the proposal still hashes to what was shown. The engine checks this too (`expect_hash` on `/resume`).

  Otherwise the message says 🔒 and you approve in the cockpit. **Rejecting always works.**
- **Every gate, #runs and failure message links to the run's page in the cockpit**
  (`<NOMAD_TAILNET_ORIGIN>/cockpit/run/<id>`), which opens on the phone over the tailnet and shows
  the whole proposal. Without `NOMAD_TAILNET_ORIGIN` the messages carry no link.
- **Double taps** resolve once: the relay and the engine both hold a per-run claim.
- **`lockdown`** (DM) stops all actions from Zulip. Only the host can undo it:
  `.venv/bin/python relay.py --unlock`, then restart the service.

## Install
1. `zulip/setup.sh`: boots Zulip, tailnet-only HTTPS on :8446, push registration, org link. It
   stops if Funnel is on for that port.
2. In Zulip, create a **Generic bot** called NOMAD. Save its zuliprc to
   `~/.config/nomad/zulip-bot.zuliprc`, and your own (Settings → Account & privacy → API key) to
   `~/.config/nomad/zulip-admin.zuliprc`.
3. `nomad-relay/install_service.sh`: tests, bootstrap, then the `nomad-relay` systemd user service.
   Bootstrap creates the channels, sets notifications, hardens the org (admin-only invites, bots
   and emoji; no message editing; no link previews) and pins your user id. Then it **deletes
   `zulip-admin.zuliprc`. Regenerate your API key in Zulip afterwards** so the old one is dead.
4. Turn on two-factor authentication for your Zulip account.
5. Phone: Tailscale on, Zulip app, log in to `https://rogue01.tailc3147c.ts.net:8446`.

## Operate
```
systemctl --user status nomad-relay
journalctl --user -u nomad-relay -f
```
State (what has been posted, which message maps to which run) lives in
`~/.local/state/nomad-relay/state.json`. Delete it to re-seed; history is skipped and only pending
approvals are re-posted.

## Test
```
.venv/bin/python -m pytest -q tests
```
