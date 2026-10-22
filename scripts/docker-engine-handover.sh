#!/usr/bin/env bash
# Step 2 of docs/DOCKER-ENGINE-RUNBOOK.md (#383), without the clicking: take /var/run/docker.sock
# away from Docker Desktop so Docker Engine can have it.
#
#   scripts/docker-engine-handover.sh check      report what would change; changes nothing
#   scripts/docker-engine-handover.sh run        do it (asks for sudo once, for the distro cleanup)
#   scripts/docker-engine-handover.sh rollback   undo it and start Docker Desktop again
#
# run, in order:
#   1. saves Desktop's settings-store.json and its "Docker Desktop" autostart Run key to
#      $BACKUP/handover/ (rollback restores them from there)
#   2. stops Docker Desktop (`docker desktop stop`, then the processes if any are left)
#   3. in settings-store.json: drops this distro from IntegratedWslDistros and turns off
#      EnableIntegrationWithDefaultWslDistro and AutoStart ("Start when you sign in")
#   4. deletes the Run key, which Desktop only syncs from AutoStart when it runs
#   5. in this distro: stops Desktop's proxy, removes the socket it leaves and every symlink into
#      /mnt/wsl/docker-desktop (docker, docker-compose, the CLI plugins). Left in place, the dangling
#      plugin links in /usr/local/lib/docker/cli-plugins come before Engine's in the CLI's search.
#   6. checks: no Desktop docker on PATH, no socket, no proxy, systemd up
#
# Desktop stays installed and its volumes untouched: that's the rollback until runbook step 9.
# Refuses to run unless the export (runbook step 1) is done and no container is running.
set -euo pipefail

BACKUP=${BACKUP:-$HOME/docker-migration}
SAVE=$BACKUP/handover
DISTRO=${WSL_DISTRO_NAME:-Ubuntu-24.04}
WIN=/mnt/c/Windows/System32
PS=$WIN/WindowsPowerShell/v1.0/powershell.exe
REG=$WIN/reg.exe
RUNKEY='HKCU\Software\Microsoft\Windows\CurrentVersion\Run'
DESKTOP_EXE='C:\Program Files\Docker\Docker\Docker Desktop.exe'
DD=/mnt/wsl/docker-desktop
LINK_DIRS=(/usr/bin /usr/local/bin /usr/local/lib/docker/cli-plugins /usr/libexec/docker/cli-plugins)

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "error: $*"; exit 1; }
# Windows tools complain when started from a Linux cwd; /mnt/c keeps them quiet.
win() { (cd /mnt/c && "$@" 2>&1 | tr -d '\r'); }
ps1() { win "$PS" -NoProfile -NonInteractive -Command "$1"; }

settings_file() {
  local appdata
  appdata=$(ps1 '$env:APPDATA') || die "can't ask Windows for APPDATA"
  printf '%s/Docker/settings-store.json' "$(wslpath -u "$appdata")"
}
desktop_procs() { ps1 'Get-Process "Docker Desktop","com.docker.backend","com.docker.build" -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessName }'; }
desktop_links() { find "${LINK_DIRS[@]}" -maxdepth 1 -type l -lname "$DD/*" 2>/dev/null || true; }
proxy_pids() { pgrep -f 'docker-desktop-user-distro proxy' || true; }
runkey_value() { win "$REG" query "$RUNKEY" /v "Docker Desktop" | sed -n 's/^ *Docker Desktop *REG_SZ *//p'; }

show_settings() {
  python3 - "$1" "$DISTRO" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  IntegratedWslDistros={d.get('IntegratedWslDistros')} "
      f"EnableIntegrationWithDefaultWslDistro={d.get('EnableIntegrationWithDefaultWslDistro')} "
      f"AutoStart={d.get('AutoStart')}")
PY
}

cmd_check() {
  local s; s=$(settings_file)
  log "Desktop settings: $s"; show_settings "$s"
  log "autostart Run key: $(runkey_value || true)"
  log "Desktop processes on Windows: $(desktop_procs | xargs echo)"
  log "proxy in this distro: $(proxy_pids | xargs echo)"
  log "socket: $(ls -l /var/run/docker.sock 2>&1)"
  log "docker on PATH: $(command -v docker || echo none) -> $(readlink -f "$(command -v docker)" 2>/dev/null || true)"
  log "Desktop symlinks to remove: $(desktop_links | wc -l)"; desktop_links | sed 's/^/  /' >&2
  log "systemd: $(systemctl is-system-running || true)"
}

preflight() {
  [[ -s "$BACKUP/volumes" ]] || die "no $BACKUP/volumes: run the export (runbook step 1) first"
  if docker info >/dev/null 2>&1; then
    [[ -z "$(docker ps -q)" ]] || die "containers are still running; the export should have stopped them"
  fi
  [[ -x "$PS" && -x "$REG" ]] || die "can't reach Windows tools under $WIN"
  case "$(systemctl is-system-running || true)" in running|degraded) ;; *) die "systemd isn't up in WSL; Engine needs it" ;; esac
}

cmd_run() {
  preflight
  sudo -v   # ask for the password now, not halfway through
  local s; s=$(settings_file)
  [[ -f "$s" ]] || die "no Desktop settings at $s"

  log "1/6 saving Desktop settings and the autostart key to $SAVE"
  mkdir -p "$SAVE"
  [[ -f "$SAVE/settings-store.json" ]] || cp "$s" "$SAVE/settings-store.json"   # keep the first, pre-run copy
  runkey_value >"$SAVE/runkey.txt" || true

  log "2/6 stopping Docker Desktop"
  docker desktop stop >/dev/null 2>&1 || true
  local i
  for i in $(seq 1 30); do [[ -z "$(desktop_procs)" ]] && break; sleep 2; done
  if [[ -n "$(desktop_procs)" ]]; then
    log "  still running after 60s, stopping the processes"
    ps1 'Get-Process "Docker Desktop","com.docker.backend","com.docker.build" -ErrorAction SilentlyContinue | Stop-Process -Force' >/dev/null
    sleep 3
    [[ -z "$(desktop_procs)" ]] || die "Docker Desktop won't stop: $(desktop_procs | xargs echo)"
  fi

  log "3/6 turning off WSL integration for $DISTRO and start-at-sign-in"
  python3 - "$s" "$DISTRO" <<'PY'
import json, os, sys
path, distro = sys.argv[1], sys.argv[2]
d = json.load(open(path))
d["IntegratedWslDistros"] = [x for x in d.get("IntegratedWslDistros", []) if x != distro]
d["EnableIntegrationWithDefaultWslDistro"] = False
d["AutoStart"] = False
tmp = path + ".nomad-tmp"
with open(tmp, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp, path)
PY
  show_settings "$s"

  log "4/6 removing the autostart Run key"
  if [[ -s "$SAVE/runkey.txt" ]]; then win "$REG" delete "$RUNKEY" /v "Docker Desktop" /f >/dev/null; fi

  log "5/6 clearing Desktop's leftovers in $DISTRO"
  local pids; pids=$(proxy_pids)
  [[ -z "$pids" ]] || sudo kill $pids
  for i in $(seq 1 10); do [[ -z "$(proxy_pids)" ]] && break; sleep 1; done
  [[ -z "$(proxy_pids)" ]] || sudo kill -9 $(proxy_pids)
  desktop_links | tee "$SAVE/links.txt" | xargs -r sudo rm -f
  [[ ! -S /var/run/docker.sock ]] || sudo rm -f /var/run/docker.sock
  hash -r

  log "6/6 checking"
  verify
}

verify() {
  local bad=0 d
  d=$(command -v docker || true)
  if [[ -n "$d" && "$(readlink -f "$d")" == "$DD"/* ]]; then log "  FAIL docker is still Desktop's: $d"; bad=1; fi
  if [[ -e /var/run/docker.sock ]]; then log "  FAIL /var/run/docker.sock is still there"; bad=1; fi
  if [[ -n "$(proxy_pids)" ]]; then log "  FAIL Desktop's proxy still runs"; bad=1; fi
  if [[ -n "$(desktop_links)" ]]; then log "  FAIL symlinks into $DD remain"; bad=1; fi
  if [[ -n "$(desktop_procs)" ]]; then log "  FAIL Docker Desktop still runs on Windows"; bad=1; fi
  case "$(systemctl is-system-running || true)" in running|degraded) ;; *) log "  FAIL systemd isn't up"; bad=1 ;; esac
  [[ $bad -eq 0 ]] || die "handover incomplete (above)"
  log "handover done: docker on PATH: ${d:-none}, no socket, Desktop stopped. Next: runbook step 3."
}

cmd_rollback() {
  [[ -f "$SAVE/settings-store.json" ]] || die "nothing saved in $SAVE; was run ever used?"
  local s; s=$(settings_file)
  log "restoring Desktop settings and the autostart key"
  cp "$SAVE/settings-store.json" "$s"
  if [[ -s "$SAVE/runkey.txt" ]]; then
    win "$REG" add "$RUNKEY" /v "Docker Desktop" /t REG_SZ /d "$(cat "$SAVE/runkey.txt")" /f >/dev/null
  fi
  log "starting Docker Desktop; it recreates the socket, the proxy and the CLI links in $DISTRO"
  ps1 "Start-Process '$DESKTOP_EXE'" >/dev/null
  log "give it a minute, then: docker info --format '{{.OperatingSystem}}'"
}

case "${1:-}" in
  check)    cmd_check ;;
  run)      cmd_run ;;
  rollback) cmd_rollback ;;
  *) sed -n '2,23p' "$0"; exit 2 ;;
esac
