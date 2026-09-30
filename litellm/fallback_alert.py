"""LiteLLM fallback alert: a Zulip message when a Claude alias falls through to codex or local.

The fallback chains in config.yaml keep NOMAD answering when the Claude subscription throttles, and
that is exactly why a fall-through is easy to miss: the answer still arrives, just from a different
brain. This callback makes the switch visible. It fires when a request for a Claude alias (`deep`,
`balanced`) was served by `codex` or by a local model (`fast`, `private`, `code`), and posts which
alias was asked for, which tier answered, and when.

`gpt` (metered cloud) is deliberately not a tier here: the approved scope is codex and local.
Add it to TIERS if landing on metered billing should page too.

How LiteLLM calls it. config.yaml registers `fallback_alert.proxy_handler_instance` under
`litellm_settings.callbacks`; the proxy resolves the module next to the config file, which is why
docker-compose mounts this file at /app/fallback_alert.py beside /app/config.yaml. After a fallback
group succeeds, the router calls `log_success_fallback_event(original_model_group, kwargs, exc)` on
every CustomLogger, with `kwargs["model"]` set to the group that answered. A nested fall-through
(deep -> balanced -> codex) is reported by the innermost hop, so "requested" can read `balanced`
when the caller asked for `deep`. Either way it is a Claude alias, and the tier is right.

Delivery. Same bot credentials the relay, nomad-watch and autopilot use (the zuliprc, mounted
read-only), straight to Zulip's REST API with urllib so the LiteLLM image needs nothing installed.
The send runs on a daemon thread and never raises into the request: an alert that cannot go out is
logged, the completion is untouched. Repeats of the same requested->served pair are held for
NOMAD_FALLBACK_ALERT_COOLDOWN seconds so a throttled afternoon posts once, not once per request.

Environment:
    NOMAD_ZULIPRC                     bot credentials (default ~/.config/nomad/zulip-bot.zuliprc)
    NOMAD_FALLBACK_ZULIP_STREAM       Zulip stream (default "alerts", where nomad-watch posts)
    NOMAD_FALLBACK_ZULIP_TOPIC        Zulip topic (default "litellm-fallback")
    NOMAD_FALLBACK_ZULIP_URL          base URL to connect to, when the zuliprc's `site` is not
                                      reachable from where this runs (inside the container:
                                      http://host.docker.internal:8096). The zuliprc host is still
                                      sent as the Host header, so Zulip resolves the realm.
    NOMAD_FALLBACK_ALERT_COOLDOWN     seconds between repeats of one requested->served pair (300)
    NOMAD_FALLBACK_ALERTS_DRY_RUN     "1": log the message instead of sending it

Manual check, before trusting it:
    python3 litellm/fallback_alert.py --dry-run                 # print the formatted message
    python3 litellm/fallback_alert.py --test-alert              # send a real one from the host
    docker exec nomad-litellm python /app/fallback_alert.py --test-alert   # ...from the container
"""
import argparse
import base64
import configparser
import datetime as dt
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # run standalone on the host for --test-alert / --dry-run
    CustomLogger = object

log = logging.getLogger("nomad.fallback_alert")

CLAUDE = {"deep", "balanced"}
TIERS = {"codex": "codex", "fast": "local", "private": "local", "code": "local"}

DEFAULT_ZULIPRC = "~/.config/nomad/zulip-bot.zuliprc"
SEND_TIMEOUT = 10


def _env(name, default):
    return os.environ.get(name) or default


def classify(requested, served):
    """The tier ('codex' or 'local') when `requested` is a Claude alias served by one, else None."""
    if requested not in CLAUDE:
        return None
    return TIERS.get(served)


def format_message(requested, served, tier, when=None, reason=""):
    when = when or dt.datetime.now().astimezone()
    lines = [f"**LiteLLM fell back from Claude to {tier}**",
             "",
             f"- requested: `{requested}`",
             f"- served by: `{served}` ({tier})",
             f"- at: {when.strftime('%Y-%m-%d %H:%M:%S %Z')}"]
    if reason:
        lines.append(f"- first error: {reason[:300]}")
    return "\n".join(lines)


class Zulip:
    """A stream message to Zulip over its REST API. `send` raises on anything but success."""

    def __init__(self, site, email, key, stream, topic, connect_url=None, insecure=False):
        self.site = site.rstrip("/")
        self.url = (connect_url or site).rstrip("/")
        self.email, self.key, self.stream, self.topic = email, key, stream, topic
        self.insecure = insecure

    @classmethod
    def from_env(cls):
        """Built from the zuliprc and env, or None when the file or a field is missing."""
        path = os.path.expanduser(_env("NOMAD_ZULIPRC", DEFAULT_ZULIPRC))
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
        return cls(site, email, key,
                   _env("NOMAD_FALLBACK_ZULIP_STREAM", "alerts"),
                   _env("NOMAD_FALLBACK_ZULIP_TOPIC", "litellm-fallback"),
                   connect_url=os.environ.get("NOMAD_FALLBACK_ZULIP_URL") or None,
                   insecure=api.get("insecure", "").lower() in ("1", "true", "yes"))

    def send(self, content):
        data = urllib.parse.urlencode({"type": "stream", "to": self.stream, "topic": self.topic,
                                       "content": content}).encode()
        req = urllib.request.Request(self.url + "/api/v1/messages", data=data, method="POST")
        token = base64.b64encode(f"{self.email}:{self.key}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
        if self.url != self.site:
            req.add_header("Host", urllib.parse.urlsplit(self.site).netloc)
        ctx = None
        if self.insecure and self.url.startswith("https"):
            import ssl
            ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT, context=ctx) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            ok = json.loads(body).get("result") == "success"
        except ValueError:
            ok = False
        if not ok:
            raise RuntimeError(f"zulip send: {body[:200]}")


class FallbackAlert(CustomLogger):
    """Posts to Zulip when the router's fallback lands a Claude request on codex or local."""

    def __init__(self, sender=None, dry_run=None, cooldown=None, clock=time.monotonic):
        if CustomLogger is not object:
            super().__init__()
        self._sender = sender
        self.dry_run = (_env("NOMAD_FALLBACK_ALERTS_DRY_RUN", "") == "1") if dry_run is None else dry_run
        self.cooldown = float(_env("NOMAD_FALLBACK_ALERT_COOLDOWN", "300")) if cooldown is None else cooldown
        self.clock = clock
        self._last = {}
        self._lock = threading.Lock()

    def sender(self):
        if self._sender is None:
            self._sender = Zulip.from_env() or False
            if not self._sender:
                log.warning("fallback alert: no usable zuliprc at %s, alerts are logged only",
                            _env("NOMAD_ZULIPRC", DEFAULT_ZULIPRC))
        return self._sender

    def _due(self, pair):
        now = self.clock()
        with self._lock:
            last = self._last.get(pair)
            if last is not None and now - last < self.cooldown:
                return False
            self._last[pair] = now
            return True

    def handle(self, requested, served, reason="", background=True):
        """Alert for one fallback. Returns the message it sent (or would), None when not alerted."""
        tier = classify(requested, served)
        if tier is None or not self._due((requested, served)):
            return None
        msg = format_message(requested, served, tier, reason=reason)
        if background:
            threading.Thread(target=self._deliver, args=(msg,), daemon=True).start()
        else:
            self._deliver(msg)
        return msg

    def _deliver(self, msg):
        try:
            sender = self.sender()
            if self.dry_run or not sender:
                log.warning("[fallback alert%s] %s", " dry run" if self.dry_run else "", msg)
                return
            sender.send(msg)
        except Exception as e:  # an alert must never break a completion
            log.error("fallback alert: send failed: %s", e)

    # ── LiteLLM hooks ────────────────────────────────────────────────────────────────────────────
    async def log_success_fallback_event(self, original_model_group, kwargs, original_exception):
        try:
            served = (kwargs or {}).get("model", "")
            self.handle(original_model_group, served, reason=_short(original_exception))
        except Exception as e:
            log.error("fallback alert: %s", e)

    async def log_failure_fallback_event(self, original_model_group, kwargs, original_exception):
        return None  # that fallback group failed too; the router tries the next one


def _short(exc):
    if exc is None:
        return ""
    return f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}".strip()


proxy_handler_instance = FallbackAlert()


def main(argv=None):
    p = argparse.ArgumentParser(description="Send or preview the LiteLLM fallback Zulip alert.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--test-alert", action="store_true", help="send a real test message to Zulip")
    g.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    p.add_argument("--requested", default="deep")
    p.add_argument("--served", default="codex", choices=sorted(TIERS))
    a = p.parse_args(argv)
    tier = TIERS[a.served]
    msg = format_message(a.requested, a.served, tier, reason="manual test, no real fallback")
    msg = msg.replace("**LiteLLM fell back", "**[TEST] LiteLLM fell back", 1)
    if a.dry_run:
        print(msg)
        return 0
    z = Zulip.from_env()
    if not z:
        print(f"no usable zuliprc at {_env('NOMAD_ZULIPRC', DEFAULT_ZULIPRC)}", file=sys.stderr)
        return 2
    z.send(msg)
    print(f"sent to #{z.stream} > {z.topic} via {z.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
