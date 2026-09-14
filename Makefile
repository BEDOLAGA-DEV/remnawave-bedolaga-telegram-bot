.PHONY: up
up: ## Поднять контейнеры (detached)
	@echo "🚀 Поднимаем контейнеры (detached)..."
	docker compose up -d --build

.PHONY: up-follow
up-follow: ## Поднять контейнеры с логами
	@echo "📡 Поднимаем контейнеры (в консоли)..."
	docker compose up --build

.PHONY: down
down: ## Остановить и удалить контейнеры
	@echo "🛑 Останавливаем и удаляем контейнеры..."
	docker compose down

.PHONY: reload
reload: ## Перезапустить контейнеры (detached)
	@$(MAKE) down
	@$(MAKE) up

.PHONY: reload-follow
reload-follow: ## Перезапустить контейнеры с логами
	@$(MAKE) down
	@$(MAKE) up-follow

.PHONY: test
test: ## Запустить тесты
	uv run pytest -v

# Имя и порт совпадают с тем, что прописано в CI-workflow tests.yml, образ —
# с docker-compose.yml. Порт 55433 выбран нестандартным, чтобы не столкнуться
# с локальной боевой базой на 5432.
PG_TEST_CONTAINER ?= bedolaga_test_pg
PG_TEST_PORT ?= 55433
PG_TEST_URL ?= postgresql+asyncpg://test:test@localhost:$(PG_TEST_PORT)/test

.PHONY: pg-test-up
pg-test-up: ## Поднять PostgreSQL для тестов
	@docker rm -f $(PG_TEST_CONTAINER) >/dev/null 2>&1 || true
	docker run -d --name $(PG_TEST_CONTAINER) \
		-e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
		-p $(PG_TEST_PORT):5432 postgres:15-alpine >/dev/null
	@echo "⏳ Ждём готовности PostgreSQL..."
	@for i in $$(seq 1 30); do \
		docker exec $(PG_TEST_CONTAINER) pg_isready -U test -d test >/dev/null 2>&1 && break; \
		sleep 1; \
	done
	@docker exec $(PG_TEST_CONTAINER) pg_isready -U test -d test

.PHONY: pg-test-down
pg-test-down: ## Убрать PostgreSQL для тестов
	@docker rm -f $(PG_TEST_CONTAINER) >/dev/null 2>&1 || true
	@echo "🧹 Контейнер $(PG_TEST_CONTAINER) удалён"

.PHONY: test-postgres
test-postgres: ## Только тесты, которым нужен настоящий PostgreSQL
	TEST_DATABASE_URL=$(PG_TEST_URL) REQUIRE_POSTGRES_TESTS=1 uv run pytest -m postgres -q

.PHONY: test-all
test-all: ## Весь прогон вместе с тестами на PostgreSQL
	TEST_DATABASE_URL=$(PG_TEST_URL) REQUIRE_POSTGRES_TESTS=1 uv run pytest -q

.PHONY: lint
lint: ## Проверить код (ruff check)
	uv run ruff check .

.PHONY: format
format: ## Форматировать код (ruff format)
	uv run ruff format .

.PHONY: fix
fix: ## Исправить код (ruff check --fix + format)
	uv run ruff check . --fix
	uv run ruff format .

.PHONY: docs-structure
docs-structure: ## Пересобрать docs/project_structure_reference.md из кода
	uv run python -m scripts.generate_structure_reference

.PHONY: migrate
migrate: ## Применить миграции (alembic upgrade head)
	uv run alembic upgrade head

.PHONY: backfill-remnawave-ids
backfill-remnawave-ids: ## Сухой прогон бэкфила панельных id (Remnawave 3.0.0)
	uv run python -m scripts.backfill_remnawave_ids

.PHONY: backfill-remnawave-ids-apply
backfill-remnawave-ids-apply: ## Применить бэкфил панельных id (Remnawave 3.0.0)
	uv run python -m scripts.backfill_remnawave_ids --apply

.PHONY: migration
migration: ## Создать миграцию (usage: make migration m="description")
	uv run alembic revision --autogenerate -m "$(m)"

.PHONY: migrate-stamp
migrate-stamp: ## Пометить БД как актуальную (для существующих БД)
	uv run alembic stamp head

.PHONY: migrate-history
migrate-history: ## Показать историю миграций
	uv run alembic history --verbose

.PHONY: help
help: ## Показать список доступных команд
	@echo ""
	@echo "📘 Команды Makefile:"
	@echo ""
	@awk -F':.*## ' '/^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
