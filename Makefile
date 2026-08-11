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
