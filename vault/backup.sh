#!/usr/bin/env bash
# Nightly OpenBao raft snapshot to ~/backups/nomad-vault (14 kept). Touches the nomad-watch
# heartbeat only when the snapshot is non-empty. The snapshot is encrypted by the vault's barrier,
# so it is useless without the unseal key; copy ~/.config/nomad/openbao/unseal.key offline too.
set -euo pipefail
dest="${NOMAD_VAULT_BACKUP_DIR:-$HOME/backups/nomad-vault}"; keep="${NOMAD_VAULT_BACKUP_KEEP:-14}"
beat="$HOME/.local/state/nomad-watch/heartbeat/nomad-vault-backup"
mkdir -p "$dest" "$(dirname "$beat")"; chmod 700 "$dest"
f="$dest/raft-$(date +%Y%m%d-%H%M%S).snap"
curl -fsS -H "X-Vault-Token: $(cat "$HOME/.config/nomad/openbao/root.token")" \
  http://127.0.0.1:8205/v1/sys/storage/raft/snapshot -o "$f"
[ -s "$f" ] || { rm -f "$f"; echo "empty snapshot" >&2; exit 1; }
chmod 600 "$f"
ls -1t "$dest"/raft-*.snap | tail -n +$((keep + 1)) | xargs -r rm -f
touch "$beat"; echo "snapshot $f ($(stat -c %s "$f") bytes)"
