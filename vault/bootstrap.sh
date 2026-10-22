#!/usr/bin/env bash
# One-time init + policy/AppRole setup for the NOMAD vault. Safe to re-run: skips what exists.
# Writes ~/.config/nomad/openbao/{unseal.key,root.token,proxy-approle.json} (0600). Prints no secret.
set -euo pipefail
D="$HOME/.config/nomad/openbao"; mkdir -p "$D"; chmod 700 "$D"
BAO() { docker exec -i -e BAO_TOKEN="${BAO_TOKEN:-}" nomad-vault-openbao-1 bao "$@"; }
status() { docker exec nomad-vault-openbao-1 bao status -format=json 2>/dev/null || true; }   # exits 2 while sealed
until status | grep -q initialized; do sleep 1; done
if ! status | grep -q '"initialized": true'; then
  # One share, threshold 1: reboots are unattended (nomad-vault-unseal.service). The key file is
  # the trust anchor; see the PRD "Security" section. Rekey later with `bao operator rekey`.
  docker exec nomad-vault-openbao-1 bao operator init -key-shares=1 -key-threshold=1 -format=json > "$D/init.json"
  python3 - "$D" <<'P'
import json, os, sys
d = sys.argv[1]; j = json.load(open(f"{d}/init.json"))
for name, val in (("unseal.key", j["unseal_keys_b64"][0]), ("root.token", j["root_token"])):
    fd = os.open(f"{d}/{name}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, val.encode()); os.close(fd)
os.remove(f"{d}/init.json")
P
fi
"$(dirname "$0")/unseal.sh"
export BAO_TOKEN="$(cat "$D/root.token")"
BAO secrets list -format=json | grep -q '"secret/"' || BAO secrets enable -path=secret -version=2 kv >/dev/null
BAO auth list -format=json | grep -q '"approle/"' || BAO auth enable approle >/dev/null
BAO policy write google-proxy - >/dev/null <<'H'
path "secret/data/google/*" { capabilities = ["read"] }
H
BAO write auth/approle/role/google-proxy token_policies=google-proxy token_ttl=1h token_max_ttl=4h >/dev/null
python3 - "$D" <<'P'
import json, os, subprocess, sys
d = sys.argv[1]
def bao(*a):
    return subprocess.run(["docker", "exec", "-e", "BAO_TOKEN=" + open(f"{d}/root.token").read(), "nomad-vault-openbao-1", "bao", *a],
                          check=True, capture_output=True, text=True).stdout
rid = json.loads(bao("read", "-format=json", "auth/approle/role/google-proxy/role-id"))["data"]["role_id"]
sid = json.loads(bao("write", "-f", "-format=json", "auth/approle/role/google-proxy/secret-id"))["data"]["secret_id"]
fd = os.open(f"{d}/proxy-approle.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, json.dumps({"role_id": rid, "secret_id": sid}).encode()); os.close(fd)
P
echo "vault ready: kv secret/, audit file, approle google-proxy"
