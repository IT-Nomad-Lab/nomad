#!/usr/bin/env bash
# Install the operator's Claude Code setup on this host: the nomad-operator plugin (tier gate,
# nomad-pm tracking and MCP, ntfy, SDLC context) and the shared settings (#1054). One script for
# both hosts: run it in WSL, and on Windows through install.ps1 (Git Bash).
# Run by the operator, not an agent: SDLC.md puts editing the guardrails in the red tier.
#
#   ~/nomad/claude-hooks/install.sh [--dry-run] [--export-only]
#
#   1. Export master of ~/nomad with git archive into a local folder, the marketplace:
#      WSL ~/.local/share/nomad-marketplace, Windows %USERPROFILE%\.nomad\marketplace. On Windows
#      the WSL export is refreshed first and copied over, since every hook runs in WSL.
#   2. Add that folder as the nomad-marketplace marketplace (or update it) and install (or update)
#      nomad-operator and nomad (the v2 agents, commands and lane MCPs, #1056) at user scope.
#   3. Run sync-settings: claude-config/ into settings.json, which also removes the old install.sh
#      and ntfy.ps1 hook entries now that the plugin carries them (backed up first).
#   4. Move the old hook copies nothing references any more to ~/.claude/backups/<time>/.
#   5. Smoke test the installed plugin, then print what is left for the operator (penpot MCP).
#
# --dry-run exports to a temp folder, prints the claude commands instead of running them, shows
# the settings diff, and smoke tests the temp export. --export-only does step 1 only.
#
# Environment: NOMAD_REPO (default ~/nomad, in WSL), NOMAD_REF (default master), NOMAD_WSL_DISTRO
# (default Ubuntu-24.04), NOMAD_WSL_ROOT (WSL export, default ~/.local/share/nomad-marketplace),
# NOMAD_WIN_ROOT (Windows export), CLAUDE_BIN (default claude), CLAUDE_CONFIG_DIR.
set -euo pipefail

dry=0 export_only=0
for a in "$@"; do
  case "$a" in
    --dry-run) dry=1 ;;
    --export-only) export_only=1 ;;
    -h|--help) sed -n '2,25p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option $a" >&2; exit 2 ;;
  esac
done

ref="${NOMAD_REF:-master}"
claude="${CLAUDE_BIN:-claude}"
cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
stamp="$(date +%Y%m%d-%H%M%S)"
mkt=nomad-marketplace
plugin="nomad-operator@$mkt"
plugins=("$plugin" "nomad@$mkt")
# What the plugin and nomad-run need: nomad-run runs claude-hooks/, mcp-local/, claude-config/.
paths=(.claude-plugin nomad-operator nomad-plugin claude-hooks claude-config mcp-local)

case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) host=windows ;;
  *) host=wsl ;;
esac

say() { printf '%s\n' "$*"; }
run() {                                    # a step that changes something: printed in a dry run
  if [ "$dry" = 1 ]; then say "would run: $*"; else "$@"; fi
}

# ── 1. export ──

# Swaps a fresh export into $1, reading a tar stream from stdin. The old export goes only after
# the new one is complete, so a failed export leaves the last good one in place.
swap_in() {
  local dest="$1" new
  mkdir -p "$(dirname "$dest")"
  new="$(mktemp -d "$dest.new.XXXXXX")"
  tar -x -C "$new"
  [ -f "$new/.claude-plugin/marketplace.json" ] || { echo "export FAILED: no marketplace.json" >&2; rm -rf "$new"; exit 1; }
  rm -rf "$dest.old"
  [ -e "$dest" ] && mv "$dest" "$dest.old"
  mv "$new" "$dest"
  rm -rf "$dest.old"
}

if [ "$host" = wsl ]; then
  repo="${NOMAD_REPO:-$HOME/nomad}"
  dest="${NOMAD_WSL_ROOT:-$HOME/.local/share/nomad-marketplace}"
  [ "$dry" = 1 ] && dest="$(mktemp -d)/nomad-marketplace"
  git -C "$repo" archive "$ref" "${paths[@]}" | swap_in "$dest"
  say "exported $repo@$ref ($(git -C "$repo" rev-parse --short "$ref")) to $dest"
else
  export MSYS_NO_PATHCONV=1                # keep /home/... arguments to wsl.exe as they are
  distro="${NOMAD_WSL_DISTRO:-Ubuntu-24.04}"
  wroot="${NOMAD_WSL_ROOT:-}"              # empty: the default, resolved inside WSL
  wsl() { wsl.exe -d "$distro" -e bash -c "$1" install "$ref" "$wroot"; }
  dest="${NOMAD_WIN_ROOT:-$HOME/.nomad/marketplace}"
  [ "$dry" = 1 ] && dest="$(mktemp -d)/marketplace"
  if [ "$dry" = 0 ]; then                  # this same script, from the same ref, in WSL
    wsl 'NOMAD_REF="$1" NOMAD_WSL_ROOT="$2" bash <(git -C "$HOME/nomad" show "$1":claude-hooks/install.sh) --export-only' \
      || { echo "WSL export FAILED: is $distro running, with ~/nomad in it?" >&2; exit 1; }
  fi
  wsl 'tar -C "${2:-$HOME/.local/share/nomad-marketplace}" -c .' | swap_in "$dest"
  say "copied the WSL export to $dest"
fi
[ "$export_only" = 1 ] && exit 0

# A path as the claude binary on this host wants it.
native() { if [ "$host" = windows ]; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# ── 2. marketplace and plugin ──

if "$claude" plugin marketplace list --json 2>/dev/null | grep -q "\"name\": *\"$mkt\""; then
  run "$claude" plugin marketplace update "$mkt"
else
  run "$claude" plugin marketplace add "$(native "$dest")" --scope user
fi
installed="$("$claude" plugin list --json 2>/dev/null || true)"
for p in "${plugins[@]}"; do
  if printf '%s' "$installed" | grep -q "\"id\": *\"$p\""; then
    run "$claude" plugin update "$p"
  else
    run "$claude" plugin install "$p" -s user
  fi
done

# ── 3. settings ──

settings="$cfg/settings.json"
sync=(--host "$host" --settings "$settings")
[ "$dry" = 1 ] && sync+=(--dry-run)
if [ "$host" = wsl ]; then
  python3 "$dest/claude-config/sync_settings.py" "${sync[@]}"
else                                       # C:/Users/...: winpath.py translates it in WSL
  sync[3]="$(cygpath -m "$settings")"
  bash "$dest/nomad-operator/bin/nomad-run" sync-settings "${sync[@]}"
fi

# ── 4. old hook copies ──

# Moves $cfg/hooks/$1 to the backups once settings.json no longer names it ($2).
retire_copy() {
  local item="$cfg/hooks/$1" needle="$2"
  [ -e "$item" ] || return 0
  if [ -f "$settings" ] && grep -qF "$needle" "$settings"; then
    say "kept $item: settings.json still runs it (the plugin isn't installed yet?)"
    return 0
  fi
  run mkdir -p "$cfg/backups/$stamp"
  run mv "$item" "$cfg/backups/$stamp/"
  [ "$dry" = 1 ] || say "retired $item to $cfg/backups/$stamp/"
}
if [ "$host" = wsl ]; then
  retire_copy tier-gate.py "hooks/tier-gate.py"
  retire_copy nomad-track "nomad-track/track.py"
else
  retire_copy ntfy.ps1 "hooks/ntfy.ps1"
fi

# ── 5. smoke test ──

# The installed copy when there is one, else the export (a dry run installs nothing).
launcher="$dest/nomad-operator/bin/nomad-run"
if [ "$dry" = 0 ]; then
  ip="$("$claude" plugin list --json 2>/dev/null | awk -v id="\"$plugin\"" '
    index($0, "\"id\"") { mine = index($0, id) > 0 }
    mine && index($0, "\"installPath\"") { sub(/.*"installPath": *"/, ""); sub(/".*/, ""); print; exit }')"
  ip="${ip//\\\\/\\}"
  [ -n "$ip" ] && [ "$host" = windows ] && ip="$(cygpath -u "$ip")"
  [ -n "$ip" ] && [ -f "$ip/bin/nomad-run" ] && launcher="$ip/bin/nomad-run"
fi
export NOMAD_WSL_ROOT="${NOMAD_WSL_ROOT:-}"
[ "$host" = wsl ] && [ "$dry" = 1 ] && NOMAD_WSL_ROOT="$dest"
[ -n "$NOMAD_WSL_ROOT" ] || unset NOMAD_WSL_ROOT
say "smoke testing $launcher"
gate() { printf '%s' "$1" | bash "$launcher" tier-gate; }
fail() { say "smoke test FAILED: $*"; exit 1; }

out="$(gate '{"tool_name":"Bash","tool_input":{"command":"git -C x push"}}')"
case "$out" in *'"ask"'*) say "smoke test: push asks, OK" ;; *) fail "push: $out" ;; esac
out="$(gate '{"tool_name":"PowerShell","tool_input":{"command":"git push origin main"}}')"
case "$out" in *'"ask"'*) say "smoke test: PowerShell push asks, OK" ;; *) fail "PowerShell push: $out" ;; esac
out="$(gate '{"tool_name":"Bash","tool_input":{"command":"git merge feat/x"}}')"
[ -z "$out" ] && say "smoke test: local merge passes, OK" || fail "merge: $out"
out="$(gate '{"tool_name":"Read","tool_input":{"file_path":"/home/x/.config/nomad/cockpit-token"}}')"
case "$out" in *'"deny"'*) say "smoke test: cockpit token read denied, OK" ;; *) fail "cockpit read: $out" ;; esac

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
cwd="$tmp"; [ "$host" = windows ] && cwd="$(cygpath -m "$tmp")"   # git.exe gets no MSYS paths
git -C "$cwd" init -q -b feat/1006-smoke
out="$(printf '{"cwd":"%s"}' "$cwd" | bash "$launcher" track start)"
case "$out" in *'#1006'*) say "smoke test: session start shows task #1006, OK" ;;
  *) fail "track start (is ~/nomad/mcp-local/.venv there in WSL?): $out" ;; esac

# ── left for the operator ──

[ -f "$HOME/.config/ntfy/topic" ] || say "note: no $HOME/.config/ntfy/topic, so ntfy pushes are off on this host"
if ! "$claude" mcp get penpot >/dev/null 2>&1; then
  say "note: no penpot MCP. Add it with your token (never commit it):"
  say "  claude mcp add --transport http --scope user penpot \"http://localhost:9001/mcp/stream?userToken=<YOUR_TOKEN>\""
fi
if [ "$host" = windows ] && [ -f "$cfg/CLAUDE.md" ]; then
  lines="$(grep -n 'ntfy\.ps1' "$cfg/CLAUDE.md" | cut -d: -f1 | paste -sd, -)"
  [ -n "$lines" ] && say "note: $cfg/CLAUDE.md line $lines still names ntfy.ps1; the plugin's ntfy.sh replaces it"
fi

if [ "$dry" = 1 ]; then
  rm -rf "$(dirname "$dest")"
  say "dry run done: nothing installed, settings.json unchanged."
else
  say "done. New sessions pick it up; run /reload-plugins in an open session."
fi
