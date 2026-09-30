#!/bin/sh
# Точка входа контейнера postgres: не даёт PostgreSQL 18 молча создать пустую
# базу поверх установки, где данные ещё лежат в старом томе PostgreSQL 15.
#
# Без этой проверки после обновления compose контейнер 18 увидел бы пустой
# новый том, выполнил initdb, и бот стартовал бы на пустой базе — со стороны
# это выглядит как потеря всех данных, хотя они целы в старом томе.
#
# Старый том смонтирован сюда только на чтение (см. docker-compose.yml).
set -eu

LEGACY_DATA="${PG_LEGACY_DATA:-/mnt/pg-legacy-data}"

if [ ! -s "$PGDATA/PG_VERSION" ] && [ -s "$LEGACY_DATA/PG_VERSION" ]; then
	legacy_version="$(cat "$LEGACY_DATA/PG_VERSION")"
	cat >&2 <<EOF

==========================================================================
  PostgreSQL не запущен: база ещё на PostgreSQL ${legacy_version}.

  Бот перешёл на PostgreSQL 18, а ваши данные лежат в старом томе.
  Чтобы перенести их без потерь, выполните в папке бота:

      make pg-upgrade

  (или: bash scripts/pg-upgrade.sh)

  Скрипт сделает резервную копию, перенесёт данные в PostgreSQL 18,
  сверит их и запустит бота. Старый том при этом не удаляется.
  Подробно: docs/postgresql-18-upgrade.md
==========================================================================

EOF
	exit 1
fi

exec docker-entrypoint.sh "$@"
