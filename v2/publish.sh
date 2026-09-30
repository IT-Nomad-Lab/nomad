#!/usr/bin/env bash
# Open a public tunnel to the NOMAD v2 engine (OPT-IN external exposure).
#
#   start:  ./v2/publish.sh
#   stop:   ./v2/publish.sh stop
#
# The engine is a much worse thing to expose than the console was. Same process: a shell on
# /ws/terminal, Claude Code in your repos on /api/dispatch, and the approval of irreversible actions
# on /resume. So this script does three things the console's did not.
#
# It turns the login ON EXPLICITLY, by restarting the engine with NOMAD_AUTH_MODE=on, rather than
# leaving the engine to notice a proxy header and hope. The console's detection is its whole gate: a
# proxy that sets none of the five headers it looks for is served with no password at all. Here the
# mode decides and the header sniff is only a backstop that refuses.
#
# It PROVES the login is on before any URL exists. The tunnel is opened after the probes pass and
# not before, because the failure that matters is the one where the password did not take and the
# public URL went out anyway.
#
# And the engine is READ-ONLY while the tunnel is up. /ws/terminal and /api/dispatch are closed
# outright, and so is everything else that writes: the point of publishing is showing NOMAD to
# someone, which means handing them the URL and the password, and a shared password in front of a
# shell is not a boundary. It is not a boundary in front of the approval gate either, and the gate
# has two doors, since typing "approve" into the chat box reaches resume_run without /resume being
# touched. Approvals keep going through Zulip over Tailscale. A visitor watching the run feed sees
# real work move while the operator drives it from the tailnet, which is the better demo anyway.
set -euo pipefail
cd "$(dirname "$0")/.."

ENGINE=http://127.0.0.1:8099          # published host-only; the tunnel reaches it over the compose network
TUNNEL=nomad-v2-tunnel

say() { printf '%s\n' "$*"; }
die() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }

# Every teardown in this script used to sit on the happy path, and the first real run proved that is
# not good enough. `url=$(docker logs … | grep …)` with nothing to match yet exits 1, `set -e` killed
# the script between turning the login on and printing anything, and it left the engine exposed and
# the tunnel up with no output saying so. A script whose job is closing a door cannot rely on
# reaching its own last line. So the teardown is a trap, and only a URL actually printed disarms it.
DONE=0
teardown() {
  [ "$DONE" = "1" ] && return 0
  printf 'Backing out: closing the tunnel and putting the engine back to local mode.\n' >&2
  docker rm -f "$TUNNEL" >/dev/null 2>&1 || true
  docker compose up -d nomad-v2-engine >/dev/null 2>&1 || true
}
trap teardown EXIT INT TERM       # EXIT alone does not cover a ctrl-C or a kill

# ── stop ────────────────────────────────────────────────────────────────────────────────────────
if [ "${1:-}" = "stop" ]; then
  docker rm -f "$TUNNEL" >/dev/null 2>&1 || true
  say "Tunnel closed. Putting the engine back to local mode …"
  docker compose up -d nomad-v2-engine >/dev/null      # NOMAD_AUTH_MODE unset, so compose uses `tailnet`
  for _ in $(seq 1 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$ENGINE/health" || true)" = "200" ] && break
    sleep 1
  done
  mode=$(curl -s -o /dev/null -w '%{http_code}' "$ENGINE/runs" || true)
  [ "$mode" = "200" ] || die "the engine is still asking for a login (/runs gave $mode). Check it by hand."
  say "Login off, engine local again."
  DONE=1
  exit 0
fi

# ── the credentials have to exist before anything is exposed ────────────────────────────────────
# Read from .env without sourcing it: .env holds every key this machine has and sourcing it into an
# interactive shell is how one ends up in a log.
have() { grep -qE "^$1=.+" .env; }
[ -f .env ] || die "no .env in $(pwd)."
have NOMAD_AUTH_USER || die "NOMAD_AUTH_USER is missing or empty in .env. Nothing is exposed."
have NOMAD_AUTH_PASS || die "NOMAD_AUTH_PASS is missing or empty in .env. Nothing is exposed."
USER_V=$(grep -m1 -E '^NOMAD_AUTH_USER=' .env | cut -d= -f2-)
PASS_V=$(grep -m1 -E '^NOMAD_AUTH_PASS=' .env | cut -d= -f2-)

# ── turn the login on ───────────────────────────────────────────────────────────────────────────
say "Restarting the engine with the login on …"
NOMAD_AUTH_MODE=on docker compose up -d nomad-v2-engine >/dev/null
for _ in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$ENGINE/health" || true)" = "200" ] && break
  sleep 1
done

# ── prove it, then open the tunnel. Never the other way round ───────────────────────────────────
code() { curl -s -o /dev/null -w '%{http_code}' "$@" || true; }
fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then printf '  ok    %-42s %s\n' "$1" "$3"
  else printf '  FAIL  %-42s got %s, wanted %s\n' "$1" "$3" "$2"; fail=1; fi
}
say "Checking the login actually took …"
check "an anonymous request is refused"      401 "$(code "$ENGINE/runs")"
check "a wrong password is refused"          401 "$(code -u "$USER_V:definitely-not-the-password" "$ENGINE/runs")"
check "the real password gets in"            200 "$(code -u "$USER_V:$PASS_V" "$ENGINE/runs")"
check "the cockpit loads with the password"  200 "$(code -u "$USER_V:$PASS_V" "$ENGINE/")"
check "dispatch is closed even with it"      403 "$(code -u "$USER_V:$PASS_V" -X POST -H 'Content-Type: application/json' -d '{}' "$ENGINE/api/dispatch")"
check "the gate cannot be approved"          403 "$(code -u "$USER_V:$PASS_V" -X POST -H 'Content-Type: application/json' -d '{}' "$ENGINE/resume")"
check "nor through the chat box"             403 "$(code -u "$USER_V:$PASS_V" -X POST -H 'Content-Type: application/json' -d '{\"message\":\"approve\"}' "$ENGINE/chat")"
check "health still answers for nomad-watch" 200 "$(code "$ENGINE/health")"

if [ "$fail" != "0" ]; then
  say ""
  die "the login did not take, so nothing was opened."
fi

# ── the tunnel ──────────────────────────────────────────────────────────────────────────────────
# Ephemeral *.trycloudflare.com, no Cloudflare account, new URL each run, gone when the container is.
docker rm -f "$TUNNEL" >/dev/null 2>&1 || true
docker run -d --name "$TUNNEL" --network nomad_default cloudflare/cloudflared:latest \
  tunnel --url http://nomad-v2-engine:8099 >/dev/null
say ""
say "Opening a Cloudflare tunnel to the engine …"
url=""
for _ in $(seq 1 30); do
  # `|| true`: on the first passes cloudflared has not printed the banner yet, grep exits 1, and
  # under `set -e` an unguarded assignment here ends the script. That is what happened on the first
  # real run of this file.
  url=$(docker logs "$TUNNEL" 2>&1 | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1 || true)
  [ -n "$url" ] && break
  sleep 2
done
[ -n "$url" ] || die "cloudflared never printed a URL."
say "  PUBLIC URL: $url"
say "  Login:      NOMAD_AUTH_USER / NOMAD_AUTH_PASS from .env"
say "  Closed:     /ws/terminal and /api/dispatch. Use them over Tailscale."
say "  Stop:       ./v2/publish.sh stop   (removes the tunnel AND turns the login back off)"
DONE=1
