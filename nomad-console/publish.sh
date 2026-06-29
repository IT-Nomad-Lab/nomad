#!/usr/bin/env bash
# Open a public tunnel to the NOMAD console (OPT-IN external exposure).
# The console runs login-free locally, but auto-REQUIRES Basic Auth for any request
# that arrives through this tunnel — so NOMAD_AUTH_USER/PASS in ../.env MUST be set,
# or the console will refuse (403) every external request. Reaches the console over the
# internal docker network (the host port is bound to 127.0.0.1 and not reachable here).
# Ephemeral *.trycloudflare.com URL (changes each run; stops when the container is removed).
#   start:  ./publish.sh
#   stop:   docker rm -f nomad-tunnel
docker rm -f nomad-tunnel >/dev/null 2>&1 || true
docker run -d --name nomad-tunnel --network nomad_default cloudflare/cloudflared:latest \
  tunnel --url http://nomad-console:8000 >/dev/null
echo "Opening Cloudflare tunnel to http://localhost:1701 …"
for i in $(seq 1 20); do
  url=$(docker logs nomad-tunnel 2>&1 | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1)
  [ -n "$url" ] && { echo "  PUBLIC URL: $url"; break; }
  sleep 2
done
echo "  Login: NOMAD_AUTH_USER / NOMAD_AUTH_PASS from .env"
echo "  Stop:  docker rm -f nomad-tunnel"
