#!/usr/bin/env bash
# failover.sh - make THIS laptop the server, because the primary is dead.
#
#   ./scripts/failover.sh              do it
#   ./scripts/failover.sh --dry-run    show every step, change nothing
#
# BEST EFFORT. It was written and syntax-checked, and its steps are exercised in --dry-run by the test suite, but it
# has NOT been run against two real machines: the development environment has no second computer, no router and no
# real network to test a promotion on. Read docs/HA.md, "What still needs real hardware", before relying on it, and
# rehearse it on the real equipment (Exit Gate 17 asks for someone other than the developer to do that).
#
# What it does, in order (target: under 2 minutes):
#   1. Check that the old server is really down. Two servers answering at once is the worst thing that can
#      happen (it splits the record in two), so if the old address still answers, it STOPS.
#   2. Promote the standby database: it stops copying from the old server and becomes the real database.
#   3. Start the application on this laptop.
#   4. Take over the server's address, so operators find the new server by the same name they always used.
#   5. Check the server is healthy.
#
# Settings (environment variables; sensible defaults):
#   PRIMARY_ADDR   the name operators use for the server                default: convocation.local
#   COMPOSE_FILE   the standby's compose file                           default: docker-compose.standby.yml
#   HEALTH_URL     where to check the new server is up                  default: http://localhost:8000/health
#   TAKEOVER_IP    (optional, Linux) IP address to take over, e.g. 192.168.10.20
#   TAKEOVER_IFACE (optional, Linux) network interface to put it on, e.g. eth0
#
# Exit codes: 0 done | 2 bad usage | 3 old server still answers | 4 promotion failed | 5 new server not healthy

set -euo pipefail

DRY=0
FORCE=0

usage() {
  echo "Usage: $0 [--dry-run] [--force]"
  echo "  --dry-run  show every step and change nothing"
  echo "  --force    continue even though the old server still answers (only if you are SURE it is switched off)"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 2 ;;
  esac
done

PRIMARY_ADDR="${PRIMARY_ADDR:-convocation.local}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.standby.yml}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
TAKEOVER_IP="${TAKEOVER_IP:-}"
TAKEOVER_IFACE="${TAKEOVER_IFACE:-}"
STARTED=$(date +%s)

say()  { echo; echo "== $*"; }
run()  { if [ "$DRY" -eq 1 ]; then echo "   would run: $*"; else echo "   \$ $*"; "$@"; fi; }

echo "FAILOVER"
[ "$DRY" -eq 1 ] && echo "DRY RUN - nothing will be changed."

say "Step 1 of 5: Check that the old server is really down"
if [ "$DRY" -eq 1 ]; then
  echo "   would ping $PRIMARY_ADDR and STOP if it answers (unless --force)."
elif ping -c 2 -W 2 "$PRIMARY_ADDR" >/dev/null 2>&1; then
  if [ "$FORCE" -eq 1 ]; then
    echo "   WARNING: $PRIMARY_ADDR still answers, continuing only because of --force."
  else
    echo "   $PRIMARY_ADDR STILL ANSWERS. The old server is not down."
    echo "   Do not continue. Two servers at once would split the record in two."
    echo "   Switch the old server off (or unplug its network cable), then run this again."
    exit 3
  fi
else
  echo "   $PRIMARY_ADDR does not answer. Good."
fi

say "Step 2 of 5: Promote the standby database"
run docker compose -f "$COMPOSE_FILE" exec -T db pg_ctl promote -D /var/lib/postgresql/data || { echo "   Could not promote the standby."; exit 4; }
if [ "$DRY" -eq 1 ]; then
  echo "   would wait up to 60 seconds for the database to leave recovery mode."
else
  for _ in $(seq 1 60); do
    state=$(docker compose -f "$COMPOSE_FILE" exec -T db psql -U "${POSTGRES_USER:-convocation_user}" -d "${POSTGRES_DB:-convocation_db}" -tAc "select pg_is_in_recovery()" 2>/dev/null | tr -d '[:space:]' || true)
    [ "$state" = "f" ] && break
    sleep 1
  done
  if [ "${state:-}" != "f" ]; then echo "   The database did not finish promoting."; exit 4; fi
  echo "   The standby is now the real database."
fi

say "Step 3 of 5: Start the application"
run docker compose -f "$COMPOSE_FILE" --profile failover up -d app

say "Step 4 of 5: Take over the server's address"
if [ -n "$TAKEOVER_IP" ] && [ -n "$TAKEOVER_IFACE" ] && command -v ip >/dev/null 2>&1; then
  run ip addr add "$TAKEOVER_IP/24" dev "$TAKEOVER_IFACE"
  if command -v arping >/dev/null 2>&1; then run arping -U -c 3 -I "$TAKEOVER_IFACE" "$TAKEOVER_IP"; fi
else
  echo "   This script cannot move the address for you on this machine. Do ONE of these:"
  echo "   a) In the router, point the name $PRIMARY_ADDR at THIS laptop's address (best: nothing to do on the operator laptops)."
  echo "   b) On each operator laptop, add this line to its hosts file:   <this laptop's address>   $PRIMARY_ADDR"
  echo "   (Set TAKEOVER_IP and TAKEOVER_IFACE to let this script take a fixed IP itself on Linux.)"
fi

say "Step 5 of 5: Check the server is healthy"
if [ "$DRY" -eq 1 ]; then
  echo "   would fetch $HEALTH_URL until it says db=up (up to 30 seconds)."
else
  ok=0
  for _ in $(seq 1 30); do
    if curl -fsS "$HEALTH_URL" 2>/dev/null | grep -q '"db":"up"'; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" -ne 1 ]; then echo "   The new server is not answering at $HEALTH_URL."; exit 5; fi
  echo "   The new server is up."
fi

ELAPSED=$(( $(date +%s) - STARTED ))
echo
if [ "$DRY" -eq 1 ]; then
  echo "DRY RUN finished. Nothing was changed."
else
  echo "DONE in ${ELAPSED} seconds (target: under 120). Operators reconnect on their own once they can reach $PRIMARY_ADDR."
fi
