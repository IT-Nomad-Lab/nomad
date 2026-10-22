#!/usr/bin/env bash
# Steps 3 and 4 of docs/DOCKER-ENGINE-RUNBOOK.md (#383): install Docker Engine in this distro, then
# bring the exported volumes onto it.
#
#   scripts/docker-engine-install.sh check    report the state of each piece; changes nothing
#   scripts/docker-engine-install.sh engine   step 3 only: install, configure, start, test the GPU
#   scripts/docker-engine-install.sh import   step 4 only: docker-engine-migrate.sh import
#   scripts/docker-engine-install.sh run      step 3, then step 4
#
# engine, in order (each part skips itself when it's already done, so a rerun is safe):
#   1. Docker's and NVIDIA's apt repos, with their signing keys
#   2. docker-ce, its CLI, containerd, buildx, compose and nvidia-container-toolkit
#   3. /etc/docker/daemon.json from docs/docker-engine/daemon.json, then nvidia-ctk merges the
#      nvidia runtime into it. An existing different file is saved to $BACKUP/handover first.
#   4. drops credsStore "desktop.exe" from ~/.docker/config.json (saved first): under Engine every
#      build and pull fails on it
#   5. enables and restarts docker and containerd, so the daemon reads the config
#   6. checks: not Docker Desktop, docker0 on 172.17.0.1, nvidia runtime present, GPU visible
#
# Run scripts/docker-engine-handover.sh first: two daemons can't share /var/run/docker.sock.
# Asks for sudo once. Rollback: the runbook's Rollback section.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
BACKUP=${BACKUP:-$HOME/docker-migration}
SAVE=$BACKUP/handover
DAEMON_SRC=$HERE/docs/docker-engine/daemon.json
CUDA_IMAGE=${CUDA_IMAGE:-nvidia/cuda:12.6.0-base-ubuntu24.04}
BRIDGE_IP=172.17.0.1
DOCKER_KEY=/etc/apt/keyrings/docker.asc
DOCKER_LIST=/etc/apt/sources.list.d/docker.list
NV_KEY=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
NV_LIST=/etc/apt/sources.list.d/nvidia-container-toolkit.list
PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit)

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "error: $*"; exit 1; }
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed'; }
desktop_left() {   # anything of Docker Desktop's still in this distro
  pgrep -f 'docker-desktop-user-distro proxy' >/dev/null && return 0
  find /usr/bin /usr/local/bin /usr/local/lib/docker/cli-plugins -maxdepth 1 -type l \
    -lname '/mnt/wsl/docker-desktop/*' 2>/dev/null | grep -q . && return 0
  return 1
}
creds_desktop() { [[ -f ~/.docker/config.json ]] && grep -q '"credsStore"[[:space:]]*:[[:space:]]*"desktop' ~/.docker/config.json; }

cmd_check() {
  log "Desktop leftovers in this distro: $(desktop_left && echo 'yes, run docker-engine-handover.sh first' || echo none)"
  log "Docker apt repo: $([[ -f $DOCKER_LIST ]] && echo yes || echo no)  NVIDIA apt repo: $([[ -f $NV_LIST ]] && echo yes || echo no)"
  local p; for p in "${PACKAGES[@]}"; do log "  $p: $(installed "$p" && dpkg-query -W -f='${Version}' "$p" || echo 'not installed')"; done
  log "/etc/docker/daemon.json: $([[ -f /etc/docker/daemon.json ]] && tr -d ' \n' </etc/docker/daemon.json | cut -c1-160 || echo none)"
  log "credsStore desktop in ~/.docker/config.json: $(creds_desktop && echo yes || echo no)"
  log "docker.service: $(systemctl is-active docker.service 2>/dev/null || true)"
  log "docker0: $(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' || true)"
  log "in the docker group: $(id -nG | tr ' ' '\n' | grep -qx docker && echo yes || echo no)"
  log "systemd: $(systemctl is-system-running || true)"
  log "export present: $([[ -s $BACKUP/volumes ]] && echo "$(wc -l <"$BACKUP/volumes") volumes" || echo no)"
}

preflight() {
  desktop_left && die "Docker Desktop is still wired into this distro; run scripts/docker-engine-handover.sh run first"
  case "$(systemctl is-system-running || true)" in running|degraded) ;; *) die "systemd isn't up in WSL" ;; esac
  [[ -f "$DAEMON_SRC" ]] || die "no $DAEMON_SRC; is this checkout on master?"
  [[ -x /usr/lib/wsl/lib/nvidia-smi ]] || die "no WSL GPU driver (/usr/lib/wsl/lib/nvidia-smi)"
  [[ -s "$BACKUP/volumes" ]] || die "no export in $BACKUP; run the runbook's step 1 first"
}

cmd_engine() {
  preflight
  sudo -v
  mkdir -p "$SAVE"
  local arch codename
  arch=$(dpkg --print-architecture)
  codename=$(. /etc/os-release && echo "$VERSION_CODENAME")

  log "1/6 apt repos"
  if [[ ! -s $DOCKER_KEY ]]; then
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$DOCKER_KEY"
    sudo chmod a+r "$DOCKER_KEY"
  fi
  [[ -f $DOCKER_LIST ]] || echo "deb [arch=$arch signed-by=$DOCKER_KEY] https://download.docker.com/linux/ubuntu $codename stable" \
    | sudo tee "$DOCKER_LIST" >/dev/null
  if [[ ! -s $NV_KEY ]]; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o "$NV_KEY"
  fi
  if [[ ! -f $NV_LIST ]]; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed "s#deb https://#deb [signed-by=$NV_KEY] https://#g" | sudo tee "$NV_LIST" >/dev/null
  fi

  log "2/6 packages"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PACKAGES[@]}" >/dev/null
  id -nG "$USER" | tr ' ' '\n' | grep -qx docker || sudo usermod -aG docker "$USER"
  hash -r

  log "3/6 daemon.json"
  if [[ -f /etc/docker/daemon.json ]] && ! grep -q '"host-gateway-ip"' /etc/docker/daemon.json; then
    cp /etc/docker/daemon.json "$SAVE/daemon.json.before"
    log "  saved the existing one to $SAVE/daemon.json.before"
  fi
  if ! grep -q '"host-gateway-ip"' /etc/docker/daemon.json 2>/dev/null; then
    sudo install -m 0644 "$DAEMON_SRC" /etc/docker/daemon.json
  fi
  sudo nvidia-ctk runtime configure --runtime=docker >/dev/null

  log "4/6 credsStore"
  if creds_desktop; then
    cp ~/.docker/config.json "$SAVE/docker-config.json"
    python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.docker/config.json")
d = json.load(open(p))
d.pop("credsStore", None)
json.dump(d, open(p, "w"), indent=2)
PY
    log "  dropped it (saved to $SAVE/docker-config.json)"
  fi

  log "5/6 starting docker"
  sudo systemctl enable --quiet docker.service containerd.service
  sudo systemctl restart containerd.service docker.service

  log "6/6 checking"
  verify_engine
}

# Docker group membership from usermod only reaches new logins; sg gives it to this process.
dk() {
  if docker info >/dev/null 2>&1; then docker "$@"; else sg docker -c "$(printf '%q ' docker "$@")"; fi
}

verify_engine() {
  local os bad=0
  os=$(dk info --format '{{.OperatingSystem}} {{.ServerVersion}}' 2>&1) || die "docker info failed: $os"
  log "  daemon: $os"
  [[ "$os" != *"Docker Desktop"* ]] || { log "  FAIL this is still Docker Desktop"; bad=1; }
  ip -4 -o addr show docker0 2>/dev/null | grep -q " $BRIDGE_IP/" \
    && log "  docker0: $BRIDGE_IP" || { log "  FAIL docker0 isn't on $BRIDGE_IP"; bad=1; }
  dk info --format '{{json .Runtimes}}' | grep -q '"nvidia"' \
    && log "  nvidia runtime: present" || { log "  FAIL no nvidia runtime"; bad=1; }
  local gpu
  if gpu=$(dk run --rm --gpus all "$CUDA_IMAGE" nvidia-smi -L 2>&1); then log "  GPU: $gpu"
  else log "  FAIL GPU test: $gpu"; bad=1; fi
  [[ $bad -eq 0 ]] || die "Engine isn't right (above); fix it before the import"
  log "Engine is up. Next: import (runbook step 4)."
}

cmd_import() {
  dk info --format '{{.OperatingSystem}}' 2>/dev/null | grep -qv 'Docker Desktop' \
    || die "no Docker Engine answering; run the engine step first"
  log "step 4: importing the volumes"
  if docker info >/dev/null 2>&1; then
    bash "$HERE/scripts/docker-engine-migrate.sh" import
  else
    sg docker -c "$(printf '%q ' bash "$HERE/scripts/docker-engine-migrate.sh" import)"
  fi
  log "import done and every manifest matched. Next: runbook step 5 (host services onto the bridge)."
}

case "${1:-}" in
  check)  cmd_check ;;
  engine) cmd_engine ;;
  import) cmd_import ;;
  run)    cmd_engine; cmd_import ;;
  *) sed -n '2,22p' "$0"; exit 2 ;;
esac
