#!/usr/bin/env bash
# Migrate the bot's PostgreSQL data between two (possibly custom-named) volumes/paths,
# e.g. PostgreSQL 15 -> PostgreSQL 18, preserving all data.
#
#   bash scripts/pg-migrate-custom.sh <OLD_VOL_OR_PATH> <NEW_VOL_OR_PATH>
#
#   OLD : where the current (PG15) data lives   - a docker volume name or host path
#   NEW : where the new (PG18) cluster goes     - a docker volume name or host path
#
# It dumps from a temp postgres:15 container on OLD and restores into a temp
# postgres:18 container on NEW. OLD is left untouched, so rolling back is easy.
#
# Must be run with bash (uses `set -o pipefail` and here-strings), from the bot dir.
set -Eeuo pipefail

if [ "$#" -ne 2 ]; then
	printf 'Usage: %s <OLD_VOL_OR_PATH> <NEW_VOL_OR_PATH>\n' "$0" >&2
	exit 2
fi
OLD="$1"; NEW="$2"

command -v docker >/dev/null || { printf 'docker not found\n' >&2; exit 1; }
cd "$(dirname "$0")/.."   # bot dir (has docker-compose.yml)
[ -f docker-compose.yml ] || { printf 'Run from the bot dir (no docker-compose.yml here)\n' >&2; exit 1; }

# Credentials exactly as compose would pass them to the postgres service.
env_out="$(docker compose run --rm --no-deps -T --entrypoint env postgres 2>/dev/null)" || true
g() { printf '%s\n' "$env_out" | sed -n "s/^$1=//p" | head -n1; }
POSTGRES_USER="$(g POSTGRES_USER)"
POSTGRES_PASSWORD="$(g POSTGRES_PASSWORD)"
POSTGRES_DB="$(g POSTGRES_DB)"
[ -n "$POSTGRES_USER" ] && [ -n "$POSTGRES_PASSWORD" ] && [ -n "$POSTGRES_DB" ] ||
	{ printf 'Missing POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB from compose\n' >&2; exit 1; }

log() { printf '\n==> %s\n' "$*"; }

docker compose stop bot postgres >/dev/null 2>&1 || true
WORK="$(mktemp -d)"; chmod 700 "$WORK"
trap 'docker rm -f pg15src pg18dst >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------- 1. dump OLD (PG15)
log "Dumping OLD (PG15): $OLD"
docker pull -q postgres:15-alpine >/dev/null
docker run -d --name pg15src --network none \
	-v "$OLD":/var/lib/postgresql/data \
	-e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" postgres:15-alpine >/dev/null
for i in $(seq 1 60); do
	docker exec pg15src psql -U "$POSTGRES_USER" -d postgres -Atc 'select 1' >/dev/null 2>&1 && break
	sleep 2
done
docker exec pg15src psql -U "$POSTGRES_USER" -d postgres -Atc 'select 1' >/dev/null 2>&1 ||
	{ docker logs --tail 30 pg15src >&2 || true; printf 'PG15 (pg15src) did not come up on OLD\n' >&2; exit 1; }

docker exec pg15src pg_dumpall -U "$POSTGRES_USER" --globals-only >"$WORK/globals.sql"
DBS="$(docker exec pg15src psql -U "$POSTGRES_USER" -d postgres -Atc \
	"select datname from pg_database where datallowconn and not datistemplate and datname <> 'postgres' order by 1")"
# include the postgres DB itself if it actually holds tables
if [ "$(docker exec pg15src psql -U "$POSTGRES_USER" -d postgres -Atc \
	"select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace \
	 where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_toast%'")" != 0 ]; then
	DBS="postgres
$DBS"
fi
[ -n "$DBS" ] || { printf 'No databases found to migrate in OLD\n' >&2; exit 1; }
while IFS= read -r db; do
	[ -n "$db" ] || continue
	docker exec pg15src pg_dump -U "$POSTGRES_USER" -Fc -d "$db" >"$WORK/$db.dump"
	printf '    dumped %s (%s)\n' "$db" "$(du -h "$WORK/$db.dump" | cut -f1)"
done <<<"$DBS"
docker rm -f pg15src >/dev/null

# ---------------------------------------------------------------- 2. restore NEW (PG18)
log "Restoring NEW (PG18): $NEW"
docker pull -q postgres:18-alpine >/dev/null
docker run -d --name pg18dst --network none \
	-v "$NEW":/var/lib/postgresql \
	-e POSTGRES_USER="$POSTGRES_USER" -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB="$POSTGRES_DB" \
	postgres:18-alpine >/dev/null
for i in $(seq 1 150); do
	if docker logs pg18dst 2>&1 | grep -q 'init process complete'; then
		docker exec pg18dst psql -U "$POSTGRES_USER" -d postgres -Atc 'select 1' >/dev/null 2>&1 && break
	fi
	sleep 2
done
docker exec pg18dst psql -U "$POSTGRES_USER" -d postgres -Atc 'select 1' >/dev/null 2>&1 ||
	{ docker logs --tail 30 pg18dst >&2 || true; printf 'PG18 (pg18dst) did not come up on NEW\n' >&2; exit 1; }

# The role-owner already exists after initdb; ignore only that expected error.
docker exec -i pg18dst psql -U "$POSTGRES_USER" -d postgres -X -q -v ON_ERROR_STOP=0 \
	<"$WORK/globals.sql" >/dev/null 2>"$WORK/globals.log" || true
if grep 'ERROR' "$WORK/globals.log" | grep -vq 'already exists'; then
	cat "$WORK/globals.log" >&2
	printf 'Failed to restore roles/globals\n' >&2
	exit 1
fi
while IFS= read -r db; do
	[ -n "$db" ] || continue
	if [ "$db" = "$POSTGRES_DB" ] || [ "$db" = postgres ]; then
		docker exec -i pg18dst pg_restore -U "$POSTGRES_USER" -d "$db" --exit-on-error --single-transaction <"$WORK/$db.dump"
	else
		docker exec -i pg18dst pg_restore -U "$POSTGRES_USER" -d postgres --create --exit-on-error <"$WORK/$db.dump"
	fi
	printf '    restored %s\n' "$db"
done <<<"$DBS"
docker rm -f pg18dst >/dev/null

log "Done. Data is now in NEW: $NEW"
printf '    Start with:  docker compose up -d bot postgres\n'
printf '    Rollback:    OLD (%s) is untouched.\n' "$OLD"
