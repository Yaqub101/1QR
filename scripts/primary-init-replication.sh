#!/bin/sh
# Runs ONCE, when the PRIMARY database is first created (mounted into /docker-entrypoint-initdb.d/).
# Creates the account the standby copies with, and lets it connect for replication.
# NOTE: it only runs on a brand-new data directory. On an existing primary, run the two statements by hand.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE ${REPLICATION_USER:-replicator} WITH REPLICATION LOGIN PASSWORD '${REPLICATION_PASSWORD:-replicator_password}';
SQL

echo "host replication ${REPLICATION_USER:-replicator} 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"
