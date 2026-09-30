#!/usr/bin/env bash
# Перенос базы бота с PostgreSQL 15 на PostgreSQL 18 без потери данных.
#
#   make pg-upgrade            (или: bash scripts/pg-upgrade.sh)
#
# Что делает:
#   1. останавливает бота и базу, чтобы во время переноса никто не писал;
#   2. поднимает временный PostgreSQL старой версии на СТАРОМ томе, снимает
#      полный дамп в backups/postgres-upgrade-<время>/ и проверяет, что он читается;
#   3. считает строки во всех таблицах и значения всех последовательностей;
#   4. поднимает временный PostgreSQL 18 на НОВОМ томе и восстанавливает дамп;
#   5. сверяет строки и последовательности один в один — при любом расхождении
#      новый том удаляется, а всё остаётся как было;
#   6. запускает базу через docker compose, ещё раз сверяет данные и запускает бота.
#
# Старый том (postgres_data) не удаляется и не очищается: это откат одной
# командой. Удалить его можно вручную, когда убедитесь, что всё работает.
# Скрипт можно запускать повторно: если перенос уже сделан, он ничего не меняет.
#
# Флаги:
#   --yes      не спрашивать подтверждение
#   --no-bot   после переноса запустить только базу, без бота
#   -h|--help  справка
set -Eeuo pipefail

TARGET_MAJOR=18
TARGET_IMAGE="postgres:${TARGET_MAJOR}-alpine"
DB_SERVICE=postgres
APP_SERVICE=bot
OLD_VOLUME_KEY=postgres_data
NEW_VOLUME_KEY=postgres18_data
READY_TIMEOUT=300

ASSUME_YES=0
START_BOT=1

usage() {
	sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
	case "$1" in
	--yes | -y) ASSUME_YES=1 ;;
	--no-bot) START_BOT=0 ;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "Неизвестный параметр: $1" >&2
		usage >&2
		exit 2
		;;
	esac
	shift
done

# Ответ на подтверждение читается прямо из терминала, а у самого скрипта stdin
# отвязан от него: docker compose run и docker exec подключаются к stdin и сбивали
# ввод — «y» не распознавался. Команды, которым нужен ввод, получают его из файлов.
if [ "$ASSUME_YES" -ne 1 ] && ! (exec </dev/tty) 2>/dev/null; then
	echo 'Нет терминала для подтверждения — запустите с --yes.' >&2
	exit 1
fi
exec </dev/null

log() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    ⚠️  %s\n' "$*" >&2; }
die() {
	printf '\n❌ %s\n' "$*" >&2
	exit 1
}

cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- проверки

command -v docker >/dev/null 2>&1 || die 'Не найден docker.'
docker compose version >/dev/null 2>&1 || die 'Нужен Docker Compose v2 (команда «docker compose»).'
[ -f docker-compose.yml ] || die 'Запускайте из папки бота: не найден docker-compose.yml.'
[ -f .env ] || die 'Не найден .env — запускайте из папки бота.'

COMPOSE_CONFIG="$(docker compose config 2>/dev/null)" || die 'docker compose config завершился ошибкой — проверьте docker-compose.yml и .env.'
PROJECT="$(printf '%s\n' "$COMPOSE_CONFIG" | sed -n 's/^name:[[:space:]]*//p' | head -n1 | tr -d "\"'")"
[ -n "$PROJECT" ] || die 'Не удалось определить имя проекта docker compose.'

printf '%s\n' "$COMPOSE_CONFIG" | grep -q "image: ${TARGET_IMAGE}" ||
	die "В docker-compose.yml база не на ${TARGET_IMAGE}. Сначала обновите бота (git pull), потом запустите скрипт."

# Том проекта по метке compose; если меток нет (том создан вручную) — по имени по умолчанию.
volume_for() {
	local key="$1" found
	found="$(docker volume ls -q \
		--filter "label=com.docker.compose.project=${PROJECT}" \
		--filter "label=com.docker.compose.volume=${key}" | head -n1)"
	if [ -z "$found" ] && docker volume inspect "${PROJECT}_${key}" >/dev/null 2>&1; then
		found="${PROJECT}_${key}"
	fi
	printf '%s' "$found"
}

in_target_image() {
	docker run --rm --network none "$@"
}

SRC="${PROJECT}-pg-upgrade-src"
DST="${PROJECT}-pg-upgrade-dst"
NEW_VOLUME=''
NEW_VOLUME_TOUCHED=0
MIGRATED=0
STOPPED=0
BACKUP_DIR=''

cleanup() {
	local status=$?
	docker rm -f "$SRC" "$DST" >/dev/null 2>&1 || true
	if [ "$MIGRATED" -ne 1 ] && [ "$NEW_VOLUME_TOUCHED" -eq 1 ] && [ -n "$NEW_VOLUME" ]; then
		# Недоперенесённая база на новом томе опаснее пустого тома: compose поднял бы
		# на ней бота. Убираем её — сторож снова будет требовать переноса.
		docker volume rm -f "$NEW_VOLUME" >/dev/null 2>&1 || true
		printf '\n    Новый том %s удалён: перенос не завершён.\n' "$NEW_VOLUME" >&2
	fi
	if [ "$status" -ne 0 ] && [ "$MIGRATED" -ne 1 ]; then
		if [ "$STOPPED" -ne 1 ]; then
			printf '\n    Ничего не изменено: бот и база не останавливались.\n' >&2
		else
			printf '\n    Данные в старом томе не тронуты. Бот остановлен.\n' >&2
			[ -n "$BACKUP_DIR" ] && printf '    Резервная копия (если успела сняться): %s\n' "$BACKUP_DIR" >&2
			printf '    Вернуться на PostgreSQL 15 без переноса: откатите docker-compose.yml на прошлую\n' >&2
			printf '    версию бота и выполните «docker compose up -d».\n' >&2
		fi
	fi
	exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

log "Проект docker compose: ${PROJECT}"

docker pull -q "$TARGET_IMAGE" >/dev/null || die "Не удалось скачать ${TARGET_IMAGE}."

OLD_VOLUME="$(volume_for "$OLD_VOLUME_KEY")"
if [ -z "$OLD_VOLUME" ]; then
	info "Старого тома ${OLD_VOLUME_KEY} нет — переносить нечего (новая установка)."
	exit 0
fi

OLD_MAJOR="$(in_target_image -v "${OLD_VOLUME}:/old:ro" --entrypoint cat "$TARGET_IMAGE" /old/PG_VERSION 2>/dev/null | tr -d '[:space:]' || true)"
if [ -z "$OLD_MAJOR" ]; then
	info "В томе ${OLD_VOLUME} нет базы — переносить нечего."
	exit 0
fi
case "$OLD_MAJOR" in
'' | *[!0-9]*) die "Непонятная версия в ${OLD_VOLUME}/PG_VERSION: «${OLD_MAJOR}»." ;;
esac
[ "$OLD_MAJOR" -lt "$TARGET_MAJOR" ] || die "В томе ${OLD_VOLUME} уже PostgreSQL ${OLD_MAJOR} — переносить нечего."

NEW_VOLUME="$(volume_for "$NEW_VOLUME_KEY")"
if [ -n "$NEW_VOLUME" ]; then
	if in_target_image -v "${NEW_VOLUME}:/new:ro" --entrypoint sh "$TARGET_IMAGE" -c "test -s /new/${TARGET_MAJOR}/docker/PG_VERSION"; then
		info "Перенос уже выполнен: база PostgreSQL ${TARGET_MAJOR} лежит в томе ${NEW_VOLUME}."
		info "Запустить бота: docker compose up -d"
		exit 0
	fi
	# Docker копирует в новый том пустой каталог 18/docker из образа — это не данные.
	if [ -n "$(in_target_image -v "${NEW_VOLUME}:/new:ro" --entrypoint sh "$TARGET_IMAGE" -c 'find /new -type f | head -n1')" ]; then
		die "Том ${NEW_VOLUME} не пустой, но базы PostgreSQL ${TARGET_MAJOR} в нём нет. Разберитесь вручную: скрипт не перезаписывает данные."
	fi
fi

# Учётные данные ровно те, что compose передаст сервису (с учётом .env и значений по умолчанию).
DB_ENV="$(docker compose run --rm --no-deps -T --entrypoint env "$DB_SERVICE" 2>/dev/null)" ||
	die 'Не удалось прочитать окружение сервиса postgres из docker compose.'
env_value() { printf '%s\n' "$DB_ENV" | sed -n "s/^$1=//p" | head -n1; }
POSTGRES_USER="$(env_value POSTGRES_USER)"
POSTGRES_PASSWORD="$(env_value POSTGRES_PASSWORD)"
POSTGRES_DB="$(env_value POSTGRES_DB)"
POSTGRES_INITDB_ARGS="$(env_value POSTGRES_INITDB_ARGS)"
[ -n "$POSTGRES_USER" ] && [ -n "$POSTGRES_PASSWORD" ] && [ -n "$POSTGRES_DB" ] ||
	die 'В окружении сервиса postgres нет POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB.'
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB POSTGRES_INITDB_ARGS

# compose run создал новый том, если его не было.
NEW_VOLUME="$(volume_for "$NEW_VOLUME_KEY")"
[ -n "$NEW_VOLUME" ] || die "Не найден том ${NEW_VOLUME_KEY} после docker compose run."

# ---------------------------------------------------------------- место на диске

kib_used="$(in_target_image -v "${OLD_VOLUME}:/old:ro" --entrypoint du "$TARGET_IMAGE" -sk /old | cut -f1)"
mkdir -p backups
chmod 700 backups
backup_free="$(df -Pk backups | awk 'NR==2 {print $4}')"
docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
# Дамп сжат и обычно заметно меньше базы; новый кластер — примерно размер старого.
need_backup=$((kib_used + 102400))
need_docker=$((kib_used * 12 / 10 + 102400))
if [ -n "$docker_root" ] && [ -d "$docker_root" ]; then
	docker_free="$(df -Pk "$docker_root" | awk 'NR==2 {print $4}')"
	if [ "$(df -Pk backups | awk 'NR==2 {print $1}')" = "$(df -Pk "$docker_root" | awk 'NR==2 {print $1}')" ]; then
		need_docker=$((need_docker + need_backup))
	fi
	[ "$docker_free" -ge "$need_docker" ] ||
		die "Мало места для Docker (${docker_root}): свободно $((docker_free / 1024)) МБ, нужно около $((need_docker / 1024)) МБ."
fi
[ "$backup_free" -ge "$need_backup" ] ||
	die "Мало места для резервной копии в ./backups: свободно $((backup_free / 1024)) МБ, нужно около $((need_backup / 1024)) МБ."

# ---------------------------------------------------------------- подтверждение

log "Перенос PostgreSQL ${OLD_MAJOR} → ${TARGET_MAJOR}"
info "Старый том:  ${OLD_VOLUME} (остаётся как есть — это откат)"
info "Новый том:   ${NEW_VOLUME}"
info "База:        ${POSTGRES_DB}, пользователь ${POSTGRES_USER}, данных ~$((kib_used / 1024)) МБ"
info "На время переноса бот будет остановлен."
if [ "$ASSUME_YES" -ne 1 ]; then
	printf '\n    Продолжить? [y/N] '
	answer=''
	read -r answer </dev/tty || true
	answer="$(printf '%s' "$answer" | tr -d '\r[:space:]')"
	case "$answer" in
	y | Y | yes | Yes | YES | д | Д | да | Да | ДА) ;;
	'') die 'Отменено.' ;;
	*) die "Отменено: ответ «${answer}» не похож на «y»." ;;
	esac
fi

# ---------------------------------------------------------------- вспомогательное

wait_ready() {
	local container="$1" need_init="$2" waited=0
	while :; do
		if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != true ]; then
			docker logs --tail 50 "$container" >&2 2>&1 || true
			die "Временный PostgreSQL (${container}) остановился."
		fi
		if [ "$need_init" -eq 0 ] || docker logs "$container" 2>&1 | grep -q 'PostgreSQL init process complete'; then
			if docker exec "$container" psql -U "$POSTGRES_USER" -d postgres -Atqc 'select 1' >/dev/null 2>&1; then
				return 0
			fi
		fi
		[ "$waited" -lt "$READY_TIMEOUT" ] || {
			docker logs --tail 50 "$container" >&2 2>&1 || true
			die "Временный PostgreSQL (${container}) не поднялся за ${READY_TIMEOUT} с."
		}
		sleep 2
		waited=$((waited + 2))
	done
}

# Снимок, по которому сверяется перенос: строки в каждой таблице и значение каждой последовательности.
SNAPSHOT_SQL="
select 'table', format('%I.%I', n.nspname, c.relname),
       (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', n.nspname, c.relname), false, true, '')))[1]::text
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where c.relkind in ('r', 'p')
   and n.nspname not in ('pg_catalog', 'information_schema')
   and n.nspname not like 'pg_toast%'
union all
select 'sequence', format('%I.%I', schemaname, sequencename), coalesce(last_value::text, 'unused')
  from pg_sequences
order by 1, 2;"

snapshot() {
	docker exec -i "$1" psql -U "$POSTGRES_USER" -d "$2" -X -A -t -F '|' -v ON_ERROR_STOP=1 <<<"$SNAPSHOT_SQL"
}

safe_name() { printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_'; }

# ---------------------------------------------------------------- 1. остановка

log 'Останавливаю бота и базу'
STOPPED=1
docker compose stop "$APP_SERVICE" "$DB_SERVICE" >/dev/null 2>&1 || true
# Контейнер базы из прошлой версии compose тоже принадлежит проекту, но на случай
# ручного запуска гасим всё, что смонтировало старый том.
for container in $(docker ps -q --filter "volume=${OLD_VOLUME}"); do
	docker stop -t 60 "$container" >/dev/null
done

# ---------------------------------------------------------------- 2. дамп со старой версии

log "Поднимаю временный PostgreSQL ${OLD_MAJOR} на старом томе"
docker pull -q "postgres:${OLD_MAJOR}-alpine" >/dev/null || die "Не удалось скачать postgres:${OLD_MAJOR}-alpine."
docker rm -f "$SRC" >/dev/null 2>&1 || true
docker run -d --name "$SRC" --network none \
	-v "${OLD_VOLUME}:/var/lib/postgresql/data" \
	-e POSTGRES_PASSWORD \
	"postgres:${OLD_MAJOR}-alpine" >/dev/null
wait_ready "$SRC" 0

docker exec "$SRC" psql -U "$POSTGRES_USER" -d postgres -Atqc 'select 1' >/dev/null 2>&1 ||
	die "Пользователь ${POSTGRES_USER} не найден в старой базе — POSTGRES_USER в .env менялся после установки?"

DATABASES="$(docker exec "$SRC" psql -U "$POSTGRES_USER" -d postgres -X -Atqc \
	"select datname from pg_database where datallowconn and not datistemplate and datname <> 'postgres' order by 1")"
if [ "$(docker exec "$SRC" psql -U "$POSTGRES_USER" -d postgres -X -Atqc \
	"select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
	  where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_toast%'")" != 0 ]; then
	DATABASES="$(printf 'postgres\n%s' "$DATABASES")"
fi
[ -n "$DATABASES" ] || die 'В старой базе не нашлось ни одной базы данных для переноса.'

BACKUP_DIR="backups/postgres-upgrade-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
log "Снимаю резервную копию в ${BACKUP_DIR}"

docker exec "$SRC" pg_dumpall -U "$POSTGRES_USER" --globals-only >"$BACKUP_DIR/globals.sql"
while IFS= read -r db; do
	[ -n "$db" ] || continue
	file="$BACKUP_DIR/$(safe_name "$db")"
	docker exec "$SRC" pg_dump -U "$POSTGRES_USER" -d "$db" -Fc >"$file.dump"
	docker exec -i "$SRC" pg_restore -l <"$file.dump" >/dev/null ||
		die "Дамп базы ${db} не читается."
	snapshot "$SRC" "$db" >"$file.before.txt"
	info "${db}: $(grep -c '^table|' "$file.before.txt") таблиц, дамп $(du -h "$file.dump" | cut -f1)"
done <<<"$DATABASES"

{
	echo "Резервная копия перед переносом PostgreSQL ${OLD_MAJOR} → ${TARGET_MAJOR}"
	echo "Время:        $(date)"
	echo "Проект:       ${PROJECT}"
	echo "Старый том:   ${OLD_VOLUME}"
	echo "Базы:         $(printf '%s' "$DATABASES" | tr '\n' ' ')"
	echo
	echo "Ручное восстановление в пустой PostgreSQL:"
	echo "  psql -U <user> -d postgres < globals.sql"
	echo "  pg_restore -U <user> -d <база> --exit-on-error <база>.dump"
} >"$BACKUP_DIR/README.txt"

docker stop -t 120 "$SRC" >/dev/null
docker rm "$SRC" >/dev/null

# ---------------------------------------------------------------- 3. восстановление в 18

log "Поднимаю временный PostgreSQL ${TARGET_MAJOR} на новом томе"
docker rm -f "$DST" >/dev/null 2>&1 || true
NEW_VOLUME_TOUCHED=1
docker run -d --name "$DST" --network none \
	-v "${NEW_VOLUME}:/var/lib/postgresql" \
	-e POSTGRES_USER -e POSTGRES_PASSWORD -e POSTGRES_DB -e POSTGRES_INITDB_ARGS \
	"$TARGET_IMAGE" >/dev/null
wait_ready "$DST" 1

log 'Восстанавливаю данные'
# Роль-владелец уже создана initdb — «already exists» здесь ожидаемо, всё остальное — ошибка.
docker exec -i "$DST" psql -U "$POSTGRES_USER" -d postgres -X -q -v ON_ERROR_STOP=0 \
	<"$BACKUP_DIR/globals.sql" >/dev/null 2>"$BACKUP_DIR/globals.restore.log" || true
if grep 'ERROR' "$BACKUP_DIR/globals.restore.log" | grep -vq 'already exists'; then
	cat "$BACKUP_DIR/globals.restore.log" >&2
	die 'Не удалось восстановить роли.'
fi

while IFS= read -r db; do
	[ -n "$db" ] || continue
	file="$BACKUP_DIR/$(safe_name "$db")"
	if [ "$db" = "$POSTGRES_DB" ] || [ "$db" = postgres ]; then
		docker exec -i "$DST" pg_restore -U "$POSTGRES_USER" -d "$db" --exit-on-error --single-transaction <"$file.dump"
	else
		docker exec -i "$DST" pg_restore -U "$POSTGRES_USER" -d postgres --create --exit-on-error <"$file.dump"
	fi
	info "${db}: восстановлена"
done <<<"$DATABASES"

docker exec "$DST" vacuumdb -U "$POSTGRES_USER" --all --analyze-only --quiet

log 'Сверяю данные'
while IFS= read -r db; do
	[ -n "$db" ] || continue
	file="$BACKUP_DIR/$(safe_name "$db")"
	snapshot "$DST" "$db" >"$file.after.txt"
	if ! diff -u "$file.before.txt" "$file.after.txt" >"$file.diff.txt"; then
		head -n 40 "$file.diff.txt" >&2
		die "После восстановления база ${db} не совпадает с исходной (подробно: ${file}.diff.txt)."
	fi
	info "${db}: строки и последовательности совпадают"
done <<<"$DATABASES"

docker stop -t 120 "$DST" >/dev/null
docker rm "$DST" >/dev/null
MIGRATED=1

# ---------------------------------------------------------------- 4. запуск через compose

log "Запускаю базу PostgreSQL ${TARGET_MAJOR} через docker compose"
docker compose up -d "$DB_SERVICE" >/dev/null
db_container="$(docker compose ps -q "$DB_SERVICE")"
waited=0
until [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$db_container")" = healthy ]; do
	[ "$waited" -lt "$READY_TIMEOUT" ] || {
		docker compose logs --tail 50 "$DB_SERVICE" >&2 || true
		die 'База не стала healthy. Данные перенесены (том сохранён), разберитесь по логам: docker compose logs postgres'
	}
	sleep 3
	waited=$((waited + 3))
done

main_file="$BACKUP_DIR/$(safe_name "$POSTGRES_DB")"
if [ -f "$main_file.before.txt" ]; then
	snapshot "$db_container" "$POSTGRES_DB" >"$main_file.compose.txt"
	diff -q "$main_file.before.txt" "$main_file.compose.txt" >/dev/null ||
		die "База, поднятая через compose, не совпадает с исходной — проверьте тома в docker-compose.yml."
fi
server_version="$(docker exec "$db_container" psql -U "$POSTGRES_USER" -d postgres -X -Atqc 'show server_version')"
info "PostgreSQL ${server_version}, данные совпадают с исходными"

if [ "$START_BOT" -eq 1 ]; then
	# Скрипт запускают сразу после git pull: без --build бот поднялся бы на образе
	# со старым кодом.
	log 'Собираю и запускаю бота'
	docker compose up -d --build || die 'База перенесена, но бот не запустился — смотрите вывод выше и «docker compose logs bot».'
fi

if [ "$START_BOT" -eq 1 ]; then
	bot_line=''
else
	bot_line="
   ⚠️  Бот не запущен (--no-bot). Запустить: docker compose up -d --build
"
fi

cat <<EOF

✅ Готово: база перенесена на PostgreSQL ${TARGET_MAJOR}.
${bot_line}
   Резервная копия:  ${BACKUP_DIR}
   Старый том:       ${OLD_VOLUME} — не тронут

   Если что-то пойдёт не так — откат на PostgreSQL ${OLD_MAJOR}:
     docker compose down
     верните docker-compose.yml прошлой версии бота
     docker compose up -d
   (изменения, сделанные после переноса, при откате не сохранятся)

   Когда убедитесь, что всё работает (например, через неделю):
     docker volume rm ${OLD_VOLUME}
     rm -rf ${BACKUP_DIR}
EOF
