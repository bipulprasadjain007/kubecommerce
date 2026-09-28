#!/usr/bin/env bash
# =============================================================================
# KubeCommerce - PostgreSQL first-init script.
#
# Mounted read-only into the postgres image at
#   /docker-entrypoint-initdb.d/10-init-databases.sh
# The official entrypoint runs it once on an EMPTY data directory, as the
# superuser defined by $POSTGRES_USER, connected to $POSTGRES_DB.
#
# Creates one database per DB-owning service, all owned by $POSTGRES_USER.
# Idempotent: safe to run against an already-provisioned instance.
# =============================================================================
set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER must be set}"
POSTGRES_DB="${POSTGRES_DB:-postgres}"

create_database_if_missing() {
  local db="$1"
  local exists
  exists="$(psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -tAc "SELECT 1 FROM pg_database WHERE datname = '${db}'")"
  if [ "$exists" = "1" ]; then
    echo "init-databases: database '${db}' already exists; skipping"
    return 0
  fi
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -c "CREATE DATABASE \"${db}\" OWNER \"${POSTGRES_USER}\""
  echo "init-databases: created database '${db}' owned by '${POSTGRES_USER}'"
}

for db in auth_db catalog_db orders_db; do
  create_database_if_missing "$db"
done

echo "init-databases: done"
