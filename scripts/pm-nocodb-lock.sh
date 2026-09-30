#!/usr/bin/env bash
# PM-01 P5: make NocoDB mission control (mc_goals, milestones, tasks) read-only, or undo it.
# Work tracking moved to OpenProject (nomad-pm); NocoDB stays readable as the rollback.
#
#   scripts/pm-nocodb-lock.sh status    which of the three tables refuse writes
#   scripts/pm-nocodb-lock.sh lock      add a trigger that refuses INSERT, UPDATE and DELETE
#   scripts/pm-nocodb-lock.sh unlock    drop it again (the rollback, see docs/CUTOVER-RUNBOOK.md)
#
# A trigger, not a REVOKE: NocoDB connects as the tables' owner, and an owner can grant itself
# back whatever a REVOKE takes away. Take a pg_dump of nomad_v2 before lock or unlock.
set -euo pipefail

SCHEMA=pq1o5m40xalmdun
TABLES=(mc_goals milestones tasks)
CONTAINER=nomad-postgres

psql() {
  docker exec -i "$CONTAINER" sh -c 'psql -v ON_ERROR_STOP=1 -q -At -U "$POSTGRES_USER" -d nomad_v2'
}

status() {
  psql <<SQL
SELECT c.relname || ': ' || CASE WHEN t.tgname IS NULL THEN 'writable' ELSE 'read-only' END
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_trigger t ON t.tgrelid = c.oid AND t.tgname = 'nomad_readonly'
WHERE n.nspname = '$SCHEMA' AND c.relname IN ('mc_goals', 'milestones', 'tasks')
ORDER BY c.relname;
SQL
}

case "${1:-status}" in
  status) ;;
  lock)
    {
      echo "BEGIN;"
      echo "CREATE OR REPLACE FUNCTION $SCHEMA.nomad_readonly() RETURNS trigger LANGUAGE plpgsql AS"
      echo "  \$\$BEGIN RAISE EXCEPTION 'mission control moved to OpenProject (PM-01 P5); NocoDB is read-only'; END\$\$;"
      for t in "${TABLES[@]}"; do
        echo "DROP TRIGGER IF EXISTS nomad_readonly ON $SCHEMA.$t;"
        echo "CREATE TRIGGER nomad_readonly BEFORE INSERT OR UPDATE OR DELETE ON $SCHEMA.$t"
        echo "  FOR EACH STATEMENT EXECUTE FUNCTION $SCHEMA.nomad_readonly();"
      done
      echo "COMMIT;"
    } | psql ;;
  unlock)
    {
      echo "BEGIN;"
      for t in "${TABLES[@]}"; do echo "DROP TRIGGER IF EXISTS nomad_readonly ON $SCHEMA.$t;"; done
      echo "DROP FUNCTION IF EXISTS $SCHEMA.nomad_readonly();"
      echo "COMMIT;"
    } | psql ;;
  *) echo "usage: $0 status|lock|unlock" >&2; exit 2 ;;
esac
status
