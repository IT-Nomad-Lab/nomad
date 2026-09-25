#!/usr/bin/env bash
# NOMAD Zulip: first boot (ADR-008). Run from a normal terminal; it is interactive at the push
# registration step (Zulip asks you to accept its push service terms).
#
#   ./zulip/setup.sh            # secrets → init → up → tailscale serve → push → org link
#
# Safe to re-run: existing secrets and data are kept, finished steps are skipped or idempotent.
set -euo pipefail
cd "$(dirname "$0")"

PORT_HTTPS="${ZULIP_TS_PORT:-8446}"   # not 443/8443/10000: those are the only ports Funnel can publish
TS_HOST="$(tailscale status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
dc() { docker compose "$@"; }
manage() { dc exec -T -u zulip zulip /home/zulip/deployments/current/manage.py "$@"; }

# 1. secrets (.env is gitignored; mode 600)
if [ ! -f .env ]; then
  rnd() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }
  umask 077
  cat > .env <<EOF
ZULIP_EXTERNAL_HOST=${TS_HOST}:${PORT_HTTPS}
ZULIP_ADMINISTRATOR=${ZULIP_ADMINISTRATOR:-josias@itnomadlab.com}
ZULIP__POSTGRES_PASSWORD=$(rnd)
ZULIP__MEMCACHED_PASSWORD=$(rnd)
ZULIP__RABBITMQ_PASSWORD=$(rnd)
ZULIP__REDIS_PASSWORD=$(rnd)
ZULIP__SECRET_KEY=$(rnd)$(rnd)
ZULIP__EMAIL_PASSWORD=unused
EOF
  echo "✓ wrote zulip/.env (external host ${TS_HOST}:${PORT_HTTPS})"
fi
chmod 600 .env

# 2. first-boot init (only when the data volume is new), then run
if ! docker volume inspect nomad-zulip_zulip >/dev/null 2>&1; then
  dc pull
  dc run --rm zulip app:init
fi
dc up -d --wait
echo "✓ zulip up on 127.0.0.1:8096"

# 3. tailnet-only HTTPS. `serve`, never `funnel`: Zulip must not be on the public internet.
tailscale serve --bg --https="${PORT_HTTPS}" http://127.0.0.1:8096
if tailscale serve status --json | python3 -c '
import sys, json
fun = json.load(sys.stdin).get("AllowFunnel") or {}
sys.exit(0 if any(k.endswith(":'"${PORT_HTTPS}"'") and v for k, v in fun.items()) else 1)'; then
  tailscale serve --https="${PORT_HTTPS}" off
  echo "✗ Funnel is enabled for :${PORT_HTTPS}; that would put Zulip on the public internet. Stopped."; exit 1
fi
echo "✓ https://${TS_HOST}:${PORT_HTTPS} (tailnet only)"

# 4. mobile push (Zulip's free relay; accepts their ToS interactively)
docker compose exec -u zulip zulip /home/zulip/deployments/current/manage.py register_server || \
  echo "⚠ push registration did not finish; re-run: docker compose exec -u zulip zulip /home/zulip/deployments/current/manage.py register_server"

# 5. the organization
if [ -z "$(manage list_realms 2>/dev/null | tail -n +2)" ]; then
  echo
  echo "Open this link to create the NOMAD organization and your account:"
  manage generate_realm_creation_link
fi

cat <<EOF

Next:
  1. In Zulip: Settings → Personal settings → Bots → Add a new bot → Generic bot, name "NOMAD".
     Download its zuliprc to ~/.config/nomad/zulip-bot.zuliprc
  2. Settings → Account & privacy → API key → download zuliprc to ~/.config/nomad/zulip-admin.zuliprc
  3. chmod 600 ~/.config/nomad/zulip-*.zuliprc
  4. nomad-relay/install_service.sh    (creates channels, hardens the org, starts the relay;
     deletes the admin zuliprc, then regenerate your API key in Zulip)
  5. Settings → Account & privacy → Two-factor authentication: turn it on.
  6. Phone: install Tailscale + the Zulip app, log in to https://${TS_HOST}:${PORT_HTTPS}
  7. After you've logged in once, narrow LOADBALANCER_IPS in compose.yaml to the Docker bridge
     subnet (docker network inspect nomad-zulip_default) and run: docker compose up -d
EOF
