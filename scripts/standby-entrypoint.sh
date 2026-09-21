#!/bin/sh
# Entrypoint of the STANDBY database container (docker-compose.standby.yml).
# First start: copy the primary's whole database (pg_basebackup) and mark this server as a standby that keeps
# following it. Later starts: just start PostgreSQL, which resumes following (or, after failover, runs as the primary).
# BEST EFFORT / not tested on two machines: see docs/HA.md.
set -eu

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "standby: no data yet, copying the database from $PRIMARY_HOST ..."
  export PGPASSWORD="$REPLICATION_PASSWORD"
  until pg_basebackup -h "$PRIMARY_HOST" -U "$REPLICATION_USER" -D "$PGDATA" -R -X stream -P; do
    echo "standby: the primary is not reachable yet, retrying in 5 seconds ..."
    sleep 5
  done
  chown -R postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
fi

exec docker-entrypoint.sh postgres -c hot_standby=on
