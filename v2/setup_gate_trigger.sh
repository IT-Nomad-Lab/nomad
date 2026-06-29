#!/usr/bin/env bash
# NOMAD v2 — push-gate trigger (Postgres LISTEN/NOTIFY).
#
# Creates a trigger on the NocoDB `comms` table that NOTIFYs 'nomad_gate' with {run_id,status}
# whenever the operator flips status → approved/rejected. The engine LISTENs and resumes the run
# instantly (sub-second) — bypassing NocoDB's webhook delivery (broken in 2026.05.3, ADR-006).
# Idempotent. Re-run if the NocoDB base/schema is recreated. Needs POSTGRES_USER in env (or .env).
set -euo pipefail
cd "$(dirname "$0")/.."
PGUSER="${POSTGRES_USER:-$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)}"
RUN() { (docker exec -i nomad-postgres "$@" 2>/dev/null) || sg docker -c "docker exec -i nomad-postgres $*"; }

# NocoDB stores the base's data in a schema named by the base id — resolve it dynamically.
SCHEMA=$(RUN psql -U "$PGUSER" -d nomad_v2 -tAc \
  "SELECT table_schema FROM information_schema.columns WHERE table_name='comms' AND column_name='run_id' LIMIT 1")
SCHEMA="$(echo "$SCHEMA" | tr -d '[:space:]')"
[ -n "$SCHEMA" ] || { echo "comms table not found — is the NocoDB base set up?"; exit 1; }
echo "comms schema: $SCHEMA"

RUN psql -U "$PGUSER" -d nomad_v2 -v ON_ERROR_STOP=1 <<SQL
CREATE OR REPLACE FUNCTION nomad_gate_notify() RETURNS trigger AS \$\$
BEGIN
  IF NEW.status IN ('approved','rejected') AND NEW.status IS DISTINCT FROM OLD.status THEN
    PERFORM pg_notify('nomad_gate', json_build_object('run_id', NEW.run_id, 'status', NEW.status)::text);
  END IF;
  RETURN NEW;
END; \$\$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS nomad_gate_trg ON "$SCHEMA".comms;
CREATE TRIGGER nomad_gate_trg AFTER UPDATE ON "$SCHEMA".comms
  FOR EACH ROW EXECUTE FUNCTION nomad_gate_notify();

-- Live cockpit: NOTIFY 'nomad_runs' on ANY comms insert/update so the engine can push a refresh
-- to the cockpit over SSE (no polling). Covers captures, routing, proposals, resumes, and even
-- edits made directly in NocoDB.
CREATE OR REPLACE FUNCTION nomad_runs_notify() RETURNS trigger AS \$\$
BEGIN
  PERFORM pg_notify('nomad_runs', COALESCE(NEW.run_id, '')); RETURN NEW;
END; \$\$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS nomad_runs_trg ON "$SCHEMA".comms;
CREATE TRIGGER nomad_runs_trg AFTER INSERT OR UPDATE ON "$SCHEMA".comms
  FOR EACH ROW EXECUTE FUNCTION nomad_runs_notify();
SQL
echo "✓ push-gate + live-runs triggers installed on $SCHEMA.comms"
