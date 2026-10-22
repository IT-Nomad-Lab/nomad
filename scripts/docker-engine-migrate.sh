#!/usr/bin/env bash
# Move NOMAD's Docker volumes from Docker Desktop to Docker Engine in WSL (#383).
# The operator runs this by hand, following docs/DOCKER-ENGINE-RUNBOOK.md. Nothing here uses sudo.
#
#   scripts/docker-engine-migrate.sh counts  <file>  Qdrant points + Postgres rows, stack running
#   scripts/docker-engine-migrate.sh export          stop every container, tar every named volume
#   scripts/docker-engine-migrate.sh import          recreate each volume on the new daemon, untar,
#                                                    check its file manifest matches the export
#   scripts/docker-engine-migrate.sh verify          counts again, diffed against the export's
#   scripts/docker-engine-migrate.sh hosts           probe every host.docker.internal target from
#                                                    inside nomad-v2-engine
#
# export runs while Docker Desktop is still the daemon; import, verify and hosts run once Docker
# Engine is. Both talk to /var/run/docker.sock, so they can't run side by side: this is why the
# volumes go through tarballs. BACKUP (default ~/docker-migration) holds the tarballs, manifests and
# counts. Nothing in it is deleted by this script; remove it yourself once verify and a week of use
# look right.
set -euo pipefail

BACKUP=${BACKUP:-$HOME/docker-migration}
HELPER=${HELPER:-alpine:3.20}
QDRANT=${QDRANT:-http://127.0.0.1:6333}
PG=${PG:-nomad-postgres}

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "error: $*"; exit 1; }
is_desktop() { [[ "$(docker info --format '{{.OperatingSystem}}')" == *"Docker Desktop"* ]]; }

# Named volumes only. Anonymous ones (64 hex chars) belong to containers that get recreated.
volumes() {
  if [[ -n "${VOLUMES:-}" ]]; then tr ' ' '\n' <<<"$VOLUMES"; return; fi
  docker volume ls -q | grep -Ev '^[0-9a-f]{64}$' || true
}

counts() {
  local out=$1
  : >"$out"
  # Qdrant: points per collection.
  local cols
  cols=$(curl -fsS "$QDRANT/collections" | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin)["result"]["collections"]))') \
    || die "Qdrant at $QDRANT didn't answer; is the stack up?"
  for c in $cols; do
    curl -fsS "$QDRANT/collections/$c" \
      | python3 -c "import json,sys; print('qdrant $c', json.load(sys.stdin)['result']['points_count'])" >>"$out"
  done
  # Postgres: exact row counts for every table in every database.
  local user dbs
  user=$(docker exec "$PG" printenv POSTGRES_USER) || die "container $PG isn't running"
  dbs=$(docker exec "$PG" psql -U "$user" -d postgres -Atc \
    "select datname from pg_database where not datistemplate order by 1")
  for db in $dbs; do
    docker exec "$PG" psql -U "$user" -d "$db" -Atc "
      select '$db.' || table_schema || '.' || table_name || ' ' ||
             (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I',
               table_schema, table_name), false, true, '')))[1]::text
      from information_schema.tables
      where table_type = 'BASE TABLE' and table_schema not in ('pg_catalog', 'information_schema')
      order by 1" | sed 's/^/postgres /' >>"$out"
  done
  log "counts: $(wc -l <"$out") lines in $out"
}

manifest() {   # manifest <volume> <out file>: sha256 of every file, by path
  docker run --rm -v "$1":/v:ro "$HELPER" \
    sh -c 'cd /v && find . -type f -exec sha256sum {} + | sort -k2' >"$2"
}

cmd_export() {
  mkdir -p "$BACKUP"
  is_desktop || die "export runs against Docker Desktop, and this daemon isn't it"
  counts "$BACKUP/counts.before"
  docker ps --format '{{.Names}}' | sort >"$BACKUP/running.before"
  log "stopping $(wc -l <"$BACKUP/running.before") containers"
  docker ps -q | xargs -r docker stop >/dev/null
  volumes >"$BACKUP/volumes"
  while read -r v; do
    [[ -n "$v" ]] || continue
    log "export $v"
    docker run --rm -v "$v":/v:ro -v "$BACKUP":/b "$HELPER" tar -C /v -czf "/b/$v.tgz" .
    manifest "$v" "$BACKUP/$v.manifest"
  done <"$BACKUP/volumes"
  log "exported $(wc -l <"$BACKUP/volumes") volumes to $BACKUP. Desktop's copies are untouched."
}

cmd_import() {
  [[ -s "$BACKUP/volumes" ]] || die "no $BACKUP/volumes; run export first"
  is_desktop && die "import runs against Docker Engine, and this daemon is still Docker Desktop"
  docker pull -q "$HELPER" >/dev/null
  local bad=0
  while read -r v; do
    [[ -n "$v" ]] || continue
    if docker volume inspect "$v" >/dev/null 2>&1 \
       && [[ -n "$(docker run --rm -v "$v":/v:ro "$HELPER" ls -A /v)" ]]; then
      die "volume $v already exists on this daemon and isn't empty; not overwriting it"
    fi
    log "import $v"
    docker volume create "$v" >/dev/null
    docker run --rm -v "$v":/v -v "$BACKUP":/b:ro "$HELPER" tar -C /v -xzf "/b/$v.tgz"
    manifest "$v" "$BACKUP/$v.manifest.after"
    if cmp -s "$BACKUP/$v.manifest" "$BACKUP/$v.manifest.after"; then
      log "  $v: $(wc -l <"$BACKUP/$v.manifest") files, manifest matches"
    else
      log "  $v: MANIFEST DIFFERS, see diff $BACKUP/$v.manifest{,.after}"; bad=1
    fi
  done <"$BACKUP/volumes"
  # Compose labels the volumes it makes; a volume without them makes `up` warn but still reuses it.
  [[ $bad -eq 0 ]] || die "one or more volumes differ; don't start the stack, see the runbook's rollback"
  log "imported. Ownership and modes came across inside the tarballs."
}

cmd_verify() {
  [[ -s "$BACKUP/counts.before" ]] || die "no $BACKUP/counts.before; run export first"
  counts "$BACKUP/counts.after"
  if diff -u "$BACKUP/counts.before" "$BACKUP/counts.after"; then
    log "counts match: $(wc -l <"$BACKUP/counts.after") Qdrant collections and Postgres tables"
  else
    die "counts differ (above). Rows written since the export are expected only if the stack ran in between."
  fi
  docker ps --format '{{.Names}}' | sort >"$BACKUP/running.after"
  if ! diff -u "$BACKUP/running.before" "$BACKUP/running.after"; then
    log "the running set differs from before the export (above)"
  fi
}

cmd_hosts() {
  # Every host.docker.internal target the stack uses, probed from inside the engine container.
  # Any HTTP answer counts, even 401 or 404: the point is that the port is reachable.
  docker exec -i nomad-v2-engine python3 - <<'PY'
import socket, sys, urllib.error, urllib.request
TARGETS = {
    "claude-bridge": 8088, "codex-bridge": 8089, "dispatcher": 8090, "termd": 8091,
    "autopilot": 8092, "zulip": 8096, "openproject": 8097, "comfyui": 8188, "voice": 8200,
    "detect": 8214, "audio": 8220, "n8n": 5678, "ollama (windows)": 11434,
    "v2-engine (pm webhook)": 8099, "llama.cpp code": 8240, "llama.cpp vision": 8241,
}
# Started on demand (llama/*.sh start), so a refused connection is not a failure.
ON_DEMAND = {8240, 8241}
print("host.docker.internal ->", socket.gethostbyname("host.docker.internal"))
bad = 0
for name, port in TARGETS.items():
    url = f"http://host.docker.internal:{port}/"
    try:
        code = urllib.request.urlopen(url, timeout=5).status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = f"FAIL {type(e).__name__}: {e}"
        if port in ON_DEMAND:
            code += " (on demand, not counted)"
        else:
            bad += 1
    print(f"  {name:18} :{port:<5} {code}")
sys.exit(1 if bad else 0)
PY
}

case "${1:-}" in
  counts) [[ -n "${2:-}" ]] || die "usage: $0 counts <file>"; counts "$2" ;;
  export) cmd_export ;;
  import) cmd_import ;;
  verify) cmd_verify ;;
  hosts)  cmd_hosts ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
