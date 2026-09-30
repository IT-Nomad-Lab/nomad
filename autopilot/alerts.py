"""Autopilot alerts (PRD 5.7, R8, AC3): one alert per event, on each of three channels.

What alerts:
- a task ends in *needs operator* or *failed*;
- a project enters `needs_you`, `budget_paused` or `error`;
- a Feature completes.

Where it goes:
- **cockpit**: an `alert` entry in the project's event log in the supervisor's state store, which
  is what the cockpit row shows as its last event;
- **Zulip** `#autopilot`, one topic per project. It posts with the same bot credentials the relay
  and nomad-watch use (`~/.config/nomad/zulip-bot.zuliprc`, NOMAD_ZULIPRC overrides), straight to
  Zulip's REST API with `requests`, so no new credential and no dependency on the `zulip` package;
- **email** to OPERATOR_EMAIL through n8n's existing `v2-send` webhook (Gmail Send), with the shared
  `X-Nomad-Token` header, the same path v2/mcp_comms.py uses. Only ever to the operator's address.

A channel that is not configured (no zuliprc, no operator address or webhook token) is skipped with
one logged warning at construction. The others still send.

Dedup. The RuneMatch trial posted one digest twice. Every alert has a key naming its condition, for
example `task:runematch:412:needs_operator` or `state:runematch:error`. A ledger on disk
(`~/.local/state/nomad-autopilot/alerts.json`, NOMAD_AUTOPILOT_ALERTS overrides) records, per key,
which channels have delivered it. A key a channel has delivered is never sent to that channel again
until the condition clears:
- a project state key clears when the project leaves that state;
- a task key clears when a worker starts that task again (`task_started`);
- a Feature key never clears.
A channel that failed is retried on the next call for the same condition, up to MAX_TRIES, without
re-sending to the channels that already have it. Load, check, send and save all happen under an
exclusive file lock, so two supervisor processes sharing the ledger cannot both send.

Sends never raise into the caller: an alert that cannot go out is logged, not a pass failure.

Integration (supervisor, per pass; see `Alerter.on_pass`):

    alerter = alerts.from_env(store)                  # once, next to the StateStore
    ...
    alerter.on_pass(pid, finished=finished, started=started, state=state, detail=detail)

or the individual hooks: `task_outcome`, `task_started`, `project_state`, `feature_completed`.
`notify(project, key, title, body)` is the generic once-per-key send, for #879's daily digest
(key `digest:<project>:<date>`).

Environment:
    NOMAD_AUTOPILOT_ALERT_CHANNELS   subset of "cockpit,zulip,email" (default all three)
    NOMAD_AUTOPILOT_ALERTS_DRY_RUN   "1": log Zulip/email alerts instead of sending them
    NOMAD_AUTOPILOT_ZULIP_CHANNEL    Zulip stream (default "autopilot")
    NOMAD_ZULIPRC                    bot credentials (default ~/.config/nomad/zulip-bot.zuliprc)
    OPERATOR_EMAIL                   the only address email alerts go to
    N8N_V2_SEND_URL                  default http://localhost:5678/webhook/v2-send
    N8N_WEBHOOK_TOKEN                (or NOMAD_N8N_WEBHOOK_TOKEN) the n8n shared header
    NOMAD_ENV_FILE                   a .env read for the above when unset (default <repo>/.env)
"""
import configparser
import contextlib
import json
import logging
import os
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

log = logging.getLogger("autopilot.alerts")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LEDGER = os.path.expanduser(
    os.environ.get("NOMAD_AUTOPILOT_ALERTS", "~/.local/state/nomad-autopilot/alerts.json"))
DEFAULT_ZULIPRC = "~/.config/nomad/zulip-bot.zuliprc"
DEFAULT_SEND_URL = "http://localhost:5678/webhook/v2-send"
CHANNELS = ("cockpit", "zulip", "email")

TASK_OUTCOMES = {"needs_operator": "needs operator", "failed": "failed"}
PROJECT_STATES = {"needs_you": "needs you", "budget_paused": "budget paused", "error": "error"}

MAX_TRIES = 5                   # per channel per condition, then give up (logged)
RETAIN_SECS = 90 * 86400        # ledger entries untouched this long are dropped
SEND_TIMEOUT = 45               # the v2-send webhook waits for Gmail


def _now():
    return time.time()


# ---- channels ---------------------------------------------------------------------------------

class Channel:
    """One place an alert goes. `send` returns normally on delivery and raises on failure."""
    name = "?"

    def send(self, project, title, body):
        raise NotImplementedError


class CockpitChannel(Channel):
    """The cockpit row: an `alert` event in the project's log in the supervisor's StateStore."""
    name = "cockpit"

    def __init__(self, store):
        self.store = store

    def send(self, project, title, body):
        self.store.event(project, "alert", title)


class ZulipChannel(Channel):
    """Zulip `#<stream>`, topic = the project id. Topics carry ids only: task titles are untrusted."""
    name = "zulip"

    def __init__(self, site, email, key, stream="autopilot", http=None, timeout=20):
        self.site = site.rstrip("/")
        self.email, self.key, self.stream = email, key, stream
        self.http, self.timeout = http, timeout

    @classmethod
    def from_zuliprc(cls, path, stream="autopilot", http=None):
        """The [api] email/key/site of a zuliprc, or None when the file or a field is missing."""
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return None
        cp = configparser.ConfigParser()
        cp.read(path)
        if not cp.has_section("api"):
            return None
        api = cp["api"]
        site, email, key = api.get("site", ""), api.get("email", ""), api.get("key", "")
        if not (site and email and key):
            return None
        if not site.startswith("http"):
            site = "https://" + site
        return cls(site, email, key, stream, http=http)

    def send(self, project, title, body):
        http = self.http or _requests()
        r = http.post(self.site + "/api/v1/messages", auth=(self.email, self.key), timeout=self.timeout,
                      data={"type": "stream", "to": self.stream, "topic": str(project)[:60],
                            "content": f"**{title}**" + (f"\n\n{body}" if body else "")})
        try:
            res = r.json()
        except Exception:
            res = {}
        if getattr(r, "status_code", 200) >= 400 or res.get("result") != "success":
            raise RuntimeError(f"zulip send: HTTP {getattr(r, 'status_code', '?')} {res.get('msg', '')}")


class EmailChannel(Channel):
    """Email to the operator through n8n's `v2-send` webhook (webhook -> Gmail Send)."""
    name = "email"

    def __init__(self, url, token, to, http=None, timeout=SEND_TIMEOUT):
        self.url, self.token, self.to = url, token, to
        self.http, self.timeout = http, timeout

    def send(self, project, title, body):
        http = self.http or _requests()
        r = http.post(self.url, timeout=self.timeout,
                      headers={"Content-Type": "application/json", "X-Nomad-Token": self.token},
                      data=json.dumps({"to": self.to, "subject": f"[autopilot] {project}: {title}",
                                       "body": f"{title}\n\n{body}".rstrip() + "\n"}))
        if getattr(r, "status_code", 200) >= 400:
            raise RuntimeError(f"n8n v2-send: HTTP {r.status_code}")


class DryRunChannel(Channel):
    """Stands in for an external channel when NOMAD_AUTOPILOT_ALERTS_DRY_RUN=1: logs, sends nothing."""

    def __init__(self, name):
        self.name = name

    def send(self, project, title, body):
        log.info("[dry run] %s alert for %s: %s", self.name, project, title)


def _requests():
    import requests
    return requests


# ---- the ledger -------------------------------------------------------------------------------

class Ledger:
    """Which channels have delivered which alert keys. A JSON file, written atomically; every
    read-modify-write runs under an exclusive flock on a sidecar lock file."""

    def __init__(self, path=DEFAULT_LEDGER):
        self.path = path
        self._tlock = threading.RLock()

    @contextlib.contextmanager
    def locked(self):
        with self._tlock:
            d = os.path.dirname(self.path) or "."
            os.makedirs(d, exist_ok=True)
            with open(self.path + ".lock", "a") as lf:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    data = self._load()
                    yield data
                    self._save(data)
                finally:
                    if fcntl:
                        fcntl.flock(lf, fcntl.LOCK_UN)

    def read(self):
        with self._tlock:
            return self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            # A broken ledger at worst repeats alerts once; refusing to alert would be worse.
            log.error("alert ledger %s unreadable (%s); starting empty", self.path, e)
            return {}

    def _save(self, data):
        cutoff = _now() - RETAIN_SECS
        for k in [k for k, v in data.items() if v.get("last", 0) < cutoff]:
            del data[k]
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".alerts.", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# ---- the alerter ------------------------------------------------------------------------------

class Alerter:
    def __init__(self, channels, ledger, max_tries=MAX_TRIES):
        self.channels = list(channels)
        self.ledger = ledger
        self.max_tries = max_tries

    # -- generic --

    def notify(self, project, key, title, body=""):
        """Send `title`/`body` once per channel for `key`. Returns {channel: "sent"|"dup"|
        "failed"|"gave_up"}. Never raises."""
        out = {}
        try:
            with self.ledger.locked() as data:
                ent = data.setdefault(key, {"project": project, "title": title, "first": _now(),
                                            "sent": {}, "tries": {}})
                ent["last"] = _now()
                for ch in self.channels:
                    if ch.name in ent["sent"]:
                        out[ch.name] = "dup"
                        continue
                    tries = ent["tries"].get(ch.name, 0)
                    if tries >= self.max_tries:
                        out[ch.name] = "gave_up"
                        continue
                    try:
                        ch.send(project, title, body)
                        ent["sent"][ch.name] = _now()
                        out[ch.name] = "sent"
                    except Exception as e:
                        ent["tries"][ch.name] = tries + 1
                        out[ch.name] = "failed"
                        lvl = log.error if tries + 1 >= self.max_tries else log.warning
                        lvl("alert %s via %s failed (%d/%d): %s: %s", key, ch.name, tries + 1,
                            self.max_tries, type(e).__name__, e)
        except Exception:
            log.exception("alert %s could not be processed", key)
        return out

    def clear(self, prefix, keep=None):
        """Forget every key starting with `prefix` except `keep`, so the condition alerts again
        the next time it happens. Never raises."""
        try:
            with self.ledger.locked() as data:
                for k in [k for k in data if k.startswith(prefix) and k != keep]:
                    del data[k]
        except Exception:
            log.exception("alert clear %s failed", prefix)

    # -- the PRD 5.7 events --

    def task_outcome(self, project, task, outcome, detail=""):
        """A finished worker's outcome. Alerts on needs_operator and failed; others are ignored."""
        if outcome not in TASK_OUTCOMES:
            return {}
        return self.notify(project, f"task:{project}:{task}:{outcome}",
                           f"task #{task} {TASK_OUTCOMES[outcome]}", detail or "")

    def task_started(self, project, task):
        """A worker started the task again: its earlier outcome alerts are over."""
        self.clear(f"task:{project}:{task}:")

    def project_state(self, project, state, detail=""):
        """The project's state after a pass. Alerts once on entering needs_you, budget_paused or
        error; leaving the state re-arms it."""
        prefix = f"state:{project}:"
        if state not in PROJECT_STATES:
            self.clear(prefix)
            return {}
        key = prefix + state
        self.clear(prefix, keep=key)
        return self.notify(project, key, f"project {project} is {PROJECT_STATES[state]}", detail or "")

    def feature_completed(self, project, feature, title=""):
        """A Feature's children are all done. Alerts once, ever, per Feature."""
        return self.notify(project, f"feature:{project}:{feature}",
                           f"Feature #{feature} complete", title or "")

    def on_pass(self, project, finished=(), started=(), state=None, detail=""):
        """Everything one supervisor pass produces, in one call. `finished` are collect()'s dicts
        ({"task", "outcome", "detail"?}), `started` are start_workers()'s dicts ({"task"}), and
        state/detail are what the pass stored with finish_pass."""
        for f in finished or ():
            self.task_outcome(project, f.get("task"), f.get("outcome"), f.get("detail", ""))
        for w in started or ():
            self.task_started(project, w.get("task"))
        if state is not None:
            self.project_state(project, state, detail)


# ---- wiring -----------------------------------------------------------------------------------

def load_env(path=None):
    """os.environ, plus a .env file for keys the environment does not set (like v2/nocodb._env)."""
    env = dict(os.environ)
    path = path or env.get("NOMAD_ENV_FILE") or os.path.join(ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, _, v = ln.partition("=")
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
    return env


def build_channels(store, env=None, http=None):
    """The configured channels, in PRD order. Unconfigured ones are skipped with a warning."""
    env = load_env() if env is None else env
    wanted = [c.strip() for c in env.get("NOMAD_AUTOPILOT_ALERT_CHANNELS", ",".join(CHANNELS)).split(",")
              if c.strip()]
    dry = env.get("NOMAD_AUTOPILOT_ALERTS_DRY_RUN", "0") == "1"
    out = []
    for name in wanted:
        if name == "cockpit":
            if store is None:
                log.warning("alerts: cockpit channel skipped, no state store given")
                continue
            out.append(CockpitChannel(store))
        elif name == "zulip":
            rc = env.get("NOMAD_ZULIPRC", DEFAULT_ZULIPRC)
            ch = ZulipChannel.from_zuliprc(rc, env.get("NOMAD_AUTOPILOT_ZULIP_CHANNEL", "autopilot"),
                                           http=http)
            if ch is None:
                log.warning("alerts: Zulip channel skipped, no usable zuliprc at %s", rc)
                continue
            out.append(DryRunChannel("zulip") if dry else ch)
        elif name == "email":
            to = env.get("OPERATOR_EMAIL", "").strip()
            token = (env.get("N8N_WEBHOOK_TOKEN") or env.get("NOMAD_N8N_WEBHOOK_TOKEN") or "").strip()
            url = env.get("N8N_V2_SEND_URL", DEFAULT_SEND_URL)
            if not (to and "@" in to and token):
                log.warning("alerts: email channel skipped, OPERATOR_EMAIL or N8N_WEBHOOK_TOKEN unset")
                continue
            out.append(DryRunChannel("email") if dry else EmailChannel(url, token, to, http=http))
        else:
            log.warning("alerts: unknown channel %r ignored", name)
    return out


def from_env(store, ledger_path=None, env=None, http=None):
    """The Alerter the supervisor uses: channels from the environment, the default ledger."""
    env = load_env() if env is None else env
    path = ledger_path or os.path.expanduser(env.get("NOMAD_AUTOPILOT_ALERTS", DEFAULT_LEDGER))
    return Alerter(build_channels(store, env, http), Ledger(path))
