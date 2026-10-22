#!/usr/bin/env bash
# Deploy the autopilot supervisor from master (#1012).
# The unit runs from ~/nomad-wt/autopilot-live, a worktree detached at the hub's local master, so
# the running code never depends on what ~/nomad has checked out. This script creates or moves
# that worktree to master, installs the unit when the repo's copy differs, and restarts the
# service. Nothing is fetched or pushed. Restarting a shared service is yellow tier (docs/SDLC.md):
# run it, then say so on the task.
#   scripts/autopilot-deploy.sh            deploy master
#   scripts/autopilot-deploy.sh --check    say what would change, change nothing
set -euo pipefail
HUB="${NOMAD_HUB:-$HOME/nomad}"
LIVE="${NOMAD_AUTOPILOT_LIVE:-$HOME/nomad-wt/autopilot-live}"
UNIT=nomad-autopilot.service
UNIT_DIR="$HOME/.config/systemd/user"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

target="$(git -C "$HUB" rev-parse --verify master)"
if [ -d "$LIVE" ]; then
  if [ -n "$(git -C "$LIVE" status --porcelain --untracked-files=no)" ]; then
    echo "refusing: $LIVE has local changes to tracked files; it should only ever hold master." >&2
    git -C "$LIVE" status --short --untracked-files=no >&2
    exit 1
  fi
  current="$(git -C "$LIVE" rev-parse HEAD)"
else
  current=none
fi
echo "live: $current"
echo "master: $target"

src="$LIVE/autopilot/$UNIT"
[ $CHECK = 1 ] && { [ "$current" = "$target" ] && echo "code: up to date" || echo "code: would move to master"; }
[ $CHECK = 1 ] && exit 0

if [ "$current" = none ]; then
  git -C "$HUB" worktree add --detach "$LIVE" "$target"
elif [ "$current" != "$target" ]; then
  git -C "$LIVE" checkout -q --detach "$target"
fi

if ! cmp -s "$src" "$UNIT_DIR/$UNIT"; then
  mkdir -p "$UNIT_DIR"
  [ -f "$UNIT_DIR/$UNIT" ] && cp "$UNIT_DIR/$UNIT" "$UNIT_DIR/$UNIT.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$src" "$UNIT_DIR/$UNIT"
  systemctl --user daemon-reload
  echo "unit: installed from $src (backup next to it)"
fi

systemctl --user restart "$UNIT"
sleep 3
systemctl --user --no-pager --lines=5 status "$UNIT" || true
echo "deployed $(git -C "$LIVE" log -1 --format='%h %s')"
