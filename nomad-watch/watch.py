"""nomad-watch: the thing that notices a service has stopped doing its job.

Three outages got through NOMAD in a row and every one of them was an ABSENCE of a signal, not a
wrong one: the n8n gate that suppressed every approval for three and a half months, the run that
sat in `new` for two months, and the relay that logged `Invalid API key` while systemctl reported
it active. Each was found by hand. The relay was the only thing watching, and nothing watched the
relay.

So this is deliberately the dumbest process in the stack, and it is shaped around the two lessons
those outages taught:

1. **It does not run forever.** A long-lived loop that wedges keeps systemd reporting `active`
   while it does nothing, which is exactly how the relay hid. This is a oneshot fired by a timer.
   A hung pass is killed by TimeoutStartSec and the unit lands in `failed`; a timer that stops
   firing shows up in `systemctl --user list-timers`.
2. **Reachable is not the same as working.** A port answering says the process is up, not that it
   is doing its job. So some targets are checked by HEARTBEAT: the service touches a file each
   time it completes a real cycle, and staleness is the alarm. That is the only check that would
   have caught the relay.

It posts to Zulip itself, with its own client, because the service it most needs to report on is
the relay. If Zulip is unreachable the pass fails loudly rather than swallowing it.

What watches this? The timer, plus the daily all-clear it posts. If #alerts goes quiet for more
than a day, the watchdog is the thing that died. That is the honest limit of a single box, and it
is written here so nobody assumes otherwise.

Run one pass:   .venv/bin/python watch.py
Print, no post: .venv/bin/python watch.py --dry-run
Current state:  .venv/bin/python watch.py --list
"""
import datetime as dt
import json
import os
import subprocess
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_PATH = os.environ.get("NOMAD_WATCH_TARGETS", os.path.join(HERE, "targets.json"))
STATE_PATH = os.path.expanduser(os.environ.get("NOMAD_WATCH_STATE",
                                               "~/.local/state/nomad-watch/state.json"))
ZULIPRC = os.path.expanduser(os.environ.get("NOMAD_ZULIPRC", "~/.config/nomad/zulip-bot.zuliprc"))
CHANNEL = os.environ.get("NOMAD_WATCH_CHANNEL", "alerts")
TOPIC = os.environ.get("NOMAD_WATCH_TOPIC", "watchdog")
# Consecutive failing passes before we call it down. The timer's period times this is how long an
# outage can hide, so it trades a page on one blip against noticing a few minutes later.
CONFIRM = int(os.environ.get("NOMAD_WATCH_CONFIRM", "2"))
# Hour (local) after which the first pass of the day posts an all-clear, so silence means healthy
# rather than dead. Set to -1 to turn the digest off.
DIGEST_HOUR = int(os.environ.get("NOMAD_WATCH_DIGEST_HOUR", "8"))
HTTP_TIMEOUT = float(os.environ.get("NOMAD_WATCH_HTTP_TIMEOUT", "8"))


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ── probes ────────────────────────────────────────────────────────────────────────────────────
# Each returns (ok, detail). `detail` is what goes in the alert, so it has to say something the
# operator can act on, not just "failed".

def probe_http(t):
    url = t["url"]
    # n8n registers a webhook for one verb; a GET on a POST-only path answers 404 whether the
    # workflow is live or deactivated, which would make this probe useless for the exact outage
    # it exists to catch. So the method is part of the target.
    method = t.get("method", "GET").upper()
    try:
        r = requests.request(method, url, json=t.get("body"),
                             timeout=t.get("timeout", HTTP_TIMEOUT))
    except requests.RequestException as e:
        return False, f"{method} {url} unreachable: {type(e).__name__}"
    ok_status = t.get("status_ok")    # explicit list, for endpoints where 404 is the real failure
    if ok_status is not None:
        if r.status_code not in ok_status:
            return False, f"{method} {url} returned {r.status_code}, expected one of {ok_status}"
    elif r.status_code >= 500:
        return False, f"{method} {url} returned {r.status_code}"
    want = t.get("expect")            # a substring the body must contain, when "answers" is weak
    if want and want not in r.text:
        return False, f"{url} answered {r.status_code} but without {want!r}"
    field = t.get("json_field")
    if field:
        # A service reporting its own staleness: `sweep_age_s` on the engine is how long since a
        # background thread last finished real work. Reachable and stale is the failure that a
        # status code cannot express.
        try:
            val = float(r.json()[field])
        except Exception as e:
            return False, f"{url} has no usable {field!r} ({type(e).__name__})"
        if val > float(t["json_max"]):
            return False, f"{field}={int(val)}, allowed {int(float(t['json_max']))}"
        return True, f"{field}={int(val)}"
    return True, f"{r.status_code}"


def probe_unit(t):
    """A systemd user unit. `active` is necessary and not sufficient — that is the whole point of
    the heartbeat probes — but a unit in `failed` is unambiguous and worth catching on its own."""
    name = t["unit"]
    try:
        p = subprocess.run(["systemctl", "--user", "is-active", name],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return False, f"systemctl failed: {type(e).__name__}: {e}"
    state = p.stdout.strip() or "unknown"
    return state == "active", state


def probe_container(t):
    name = t["container"]
    try:
        p = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}} {{.State.Restarting}}", name],
                           capture_output=True, text=True, timeout=25)
    except Exception as e:
        return False, f"docker inspect failed: {type(e).__name__}: {e}"
    if p.returncode != 0:
        return False, (p.stderr.strip() or "no such container")[:120]
    status, restarting = (p.stdout.strip().split() + ["?", "?"])[:2]
    if restarting == "true":
        return False, "restarting (crash loop)"
    return status == "running", status


def probe_heartbeat(t, now=None):
    """The only probe that asks whether the service is doing its JOB. The service touches `path`
    at the end of each real cycle; if the file stops moving, the process may still be `active`
    and still be dead. This is the check the relay outage needed."""
    path = os.path.expanduser(t["path"])
    max_age = float(t.get("max_age_s", 300))
    now = now or dt.datetime.now().timestamp()
    try:
        age = now - os.path.getmtime(path)
    except FileNotFoundError:
        return False, f"no heartbeat at {path} (never written, or state was wiped)"
    except Exception as e:
        return False, f"{path}: {type(e).__name__}: {e}"
    if age > max_age:
        return False, f"last beat {int(age)}s ago, allowed {int(max_age)}s"
    return True, f"{int(age)}s ago"


PROBES = {"http": probe_http, "unit": probe_unit, "container": probe_container,
          "heartbeat": probe_heartbeat}


def check(t):
    fn = PROBES.get(t.get("type"))
    if not fn:
        return False, f"unknown probe type {t.get('type')!r}"
    try:
        return fn(t)
    except Exception as e:                       # a broken probe must not take the pass down
        return False, f"probe crashed: {type(e).__name__}: {e}"


# ── state ─────────────────────────────────────────────────────────────────────────────────────
def load_state(path=STATE_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state, path=STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)                        # never leave a half-written state behind


def load_targets(path=TARGETS_PATH):
    with open(path) as f:
        data = json.load(f)
    return [t for t in data.get("targets", []) if t.get("enabled", True)]


# ── one pass ──────────────────────────────────────────────────────────────────────────────────
def run_pass(targets, state, now=None):
    """Probe everything, fold the results into `state`, and return the messages to post.

    Alerts fire on a CHANGE, not on a condition, so a service that is down stays one message
    instead of one every five minutes. `fails` has to reach CONFIRM before we call it down, and
    a single success clears it, so a blip costs nothing and a real outage is reported once.
    """
    now = now or dt.datetime.now()
    msgs, down_now = [], []
    for t in targets:
        name = t["name"]
        ok, detail = check(t)
        prev = state.get(name, {"ok": True, "fails": 0})
        was_ok = prev.get("ok", True)
        fails = 0 if ok else prev.get("fails", 0) + 1
        is_ok = ok or fails < CONFIRM            # not down until it has failed CONFIRM times
        if was_ok and not is_ok:
            msgs.append(f"🔴 **{name}** is down — {detail}\n{t.get('hint', '')}".rstrip())
        elif not was_ok and is_ok:
            since = prev.get("since", "")
            msgs.append(f"🟢 **{name}** is back — {detail}" + (f" (down since {since})" if since else ""))
        if not is_ok:
            down_now.append(name)
        state[name] = {"ok": is_ok, "fails": fails, "detail": detail,
                       "checked": now.isoformat(timespec="seconds"),
                       "since": prev.get("since") if not is_ok and not was_ok
                       else (now.isoformat(timespec="seconds") if not is_ok else None)}
    return msgs, down_now


def digest_due(state, now, hour=None):
    """One all-clear a day, so an #alerts channel that has gone quiet means healthy and not dead.
    Without it, a watchdog that died looks exactly like a stack with nothing wrong."""
    hour = DIGEST_HOUR if hour is None else hour
    if hour < 0:
        return False
    last = (state.get("_meta", {}) or {}).get("digest_day")
    return now.hour >= hour and last != now.date().isoformat()


def digest_text(targets, state, down):
    if down:
        return f"👁 watchdog: {len(down)} of {len(targets)} down — " + ", ".join(down)
    return f"👁 watchdog: all {len(targets)} services healthy."


# ── zulip ─────────────────────────────────────────────────────────────────────────────────────
def zulip_sender():
    """Its own client, deliberately. The relay is one of the things being watched, so routing
    through the relay would mean the watchdog goes silent exactly when it matters."""
    import zulip
    client = zulip.Client(config_file=ZULIPRC)

    def send(text):
        res = client.send_message({"type": "stream", "to": CHANNEL, "topic": TOPIC, "content": text})
        if res.get("result") != "success":
            raise RuntimeError(f"zulip send: {res.get('msg')}")
    return send


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    state = load_state()
    if "--list" in argv:
        for k, v in sorted(state.items()):
            if k != "_meta":
                print(f"{'ok ' if v.get('ok') else 'DOWN'} {k:22} {v.get('detail', '')}")
        return 0
    targets = load_targets()
    now = dt.datetime.now()
    msgs, down = run_pass(targets, state, now)
    if digest_due(state, now):
        msgs.append(digest_text(targets, state, down))
        state.setdefault("_meta", {})["digest_day"] = now.date().isoformat()
    for m in msgs:
        log(m.replace("\n", " | "))
    if msgs and "--dry-run" not in argv:
        send = zulip_sender()                    # built only when there is something to say
        for m in msgs:
            send(m)
    save_state(state)
    log(f"pass done: {len(targets)} targets, {len(down)} down, {len(msgs)} posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
