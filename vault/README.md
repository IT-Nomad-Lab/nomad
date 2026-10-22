# NOMAD vault (OpenBao)

Epic #1291, PRD `docs/prd/secrets-vault-google-broker.md`. OpenBao 2.6.2, Raft, audit log, loopback
only (`127.0.0.1:8205`; 8200 is the voice service).

| what | where |
|---|---|
| run | `cd ~/nomad/vault && docker compose up -d`, then `./bootstrap.sh` the first time |
| unseal key, root token, proxy AppRole | `~/.config/nomad/openbao/` (0600). Never in git, chat or `.env` |
| unseal after a reboot | `nomad-vault-unseal.service` (calls `unseal.sh`) |
| backup | `nomad-vault-backup.timer` 03:45 -> `~/backups/nomad-vault`, 14 kept; heartbeat watched |
| audit log | volume `nomad-vault_audit`, `/openbao/audit/audit.log` in the container |
| load the Google client + refresh token | `python3 seed_google.py` (after `mcp-gateway/google_consent.py`) |

## Accepted trade-off

Unseal is 1 share, threshold 1, from a root-only key file, so a reboot needs no operator. Whoever
can read that file and the volume can read the vault. Keep a copy of `unseal.key` in a password
manager: without it a snapshot cannot be restored. To change to 3-of-5 with manual unseal:
`bao operator rekey -init -key-shares=5 -key-threshold=3`, then disable the unseal unit.

## Restore

```
docker compose down; docker volume rm nomad-vault_data
docker compose up -d      # fresh, uninitialized
curl -X POST -H "X-Vault-Token: <root>" --data-binary @raft-<stamp>.snap \
  http://127.0.0.1:8205/v1/sys/storage/raft/snapshot-force   # needs a token valid in the snapshot
```
Use the unseal key and root token that belong to the snapshot, not a new init.

## Notes

- OpenBao drops mlock: do not set `disable_mlock`. Audit devices are declared in `config.hcl`
  (the API refuses to enable them). The data volume must be owned by uid 100 / gid 1000.
- `bao status` exits 2 while sealed; scripts here tolerate that under `pipefail`.
