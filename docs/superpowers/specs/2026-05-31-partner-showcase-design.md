# Витрина партнёров (outbound cross-promo) — design

**Date:** 2026-05-31
**Scope:** CMS-список партнёрских ссылок, показ в боте + кабинете, счётчик кликов, admin-CRUD
**Status:** Draft
**Feature:** #9 остаток (showcase). Inbound cross-promo (атрибуция/отчёт/выплаты) — УЖЕ есть.

## Проблема

Inbound cross-promo готов (кампании, partner-stats, выплаты). Нет
**outbound** витрины: бот не показывает юзерам список партнёрских
проектов/ссылок (бартер-реклама «мы рекламируем партнёра»). Нужен
admin-управляемый список карточек с подсчётом кликов.

Низкоприоритетная фича (подтверждено) — держим минимальной.

## Решение

CMS-таблица `PartnerPromo` (по образцу `InfoPage`), admin-CRUD, показ
списка активных карточек в боте + кабинете, клик через redirect-эндпоинт
который инкрементит счётчик и 302-редиректит на партнёрский URL (с
link-safety валидацией).

### Компонент 1: миграция + модель (0099)

`PartnerPromo` (mirror `InfoPage`-стиля, models.py:4582):
```python
op.create_table(
    'partner_promos',
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('title', sa.JSON(), nullable=False, server_default='{}'),        # multilingual {lang: text}
    sa.Column('description', sa.JSON(), nullable=False, server_default='{}'),   # multilingual
    sa.Column('url', sa.String(2048), nullable=False),                          # partner link (https only)
    sa.Column('image_url', sa.String(2048), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
    sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('click_count', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
)
```
Модель `PartnerPromo(Base)` с теми же колонками. `title`/`description` —
multilingual dict (JSON) как `InfoPage.title`. down_revision `0098`.

### Компонент 2: CRUD + link-safety

`app/database/crud/partner_promo.py`:
- `list_active(db) -> list[PartnerPromo]` (is_active, sort by sort_order, id).
- `list_all(db)` (admin).
- `get(db, id)`, `create(db, **)`, `update(db, id, **)`, `delete(db, id)`.
- `increment_click(db, id)` — атомарный `update(PartnerPromo).where(id==).values(click_count=PartnerPromo.click_count + 1)` (без read-modify-write race).

Link-safety helper `_is_safe_url(u) -> bool`: только `https://` + валидный
hostname; отклонять `javascript:`/`http:`/`data:`/пустое. `create`/`update`
вызывают его на `url` и `image_url` (если задан), отклоняют невалидное.

### Компонент 3: клик-redirect эндпоинт

`GET /partner-promo/{id}/go` (public webserver route, без авторизации —
открывается в браузере):
1. `promo = get(db, id)`; не найден / `is_active=False` → 404.
2. `await increment_click(db, id)`.
3. `RedirectResponse(promo.url, status_code=302)`.

URL уже https-валидирован при создании. Раз нужен счётчик — все кнопки
ведут через `/go`, не напрямую на `promo.url`.

### Компонент 4: показ в боте

Пункт меню «🤝 Партнёры» (gated по `PARTNER_SHOWCASE_ENABLED` И наличию
активных промо). Callback `nz!_partner_showcase` → хендлер рисует карточки:
для каждой — `title[lang]` + URL-кнопка на `{public_base}/partner-promo/{id}/go`.
Пустой список → пункт меню скрыт. Точка: `app/keyboards/inline.py` (рядом
с `menu_info`) + хендлер в `app/handlers/` (mirror info-page list).

### Компонент 5: показ в кабинете

React `PartnerShowcase.tsx` + `api/partnerPromo.ts`
(`GET /cabinet/partner-promos` → активные). Карточки image+title+desc +
кнопка «Перейти» → `/partner-promo/{id}/go`. Маршрут + nav (gated, mirror
speedtest-подход). Нестед cabinet-репо.

### Компонент 6: admin-CRUD

Бэк: `app/cabinet/routes/admin_partner_promos.py` — list/create/update/
delete/toggle (mirror `admin_info_pages`/`admin_landings`), https-валидация
через CRUD. React admin-UI — follow-up (v1 = бэк-эндпоинты).

### Компонент 7: конфиг

`settings.PARTNER_SHOWCASE_ENABLED: bool = False` (env). Дефолт OFF.

## Что НЕ входит

- Inbound cross-promo (есть).
- Авто-выплаты за клики (витрина = бартер, не CPA).
- Ротация/таргетинг/A-B (только sort_order).
- Per-day клик-аналитика (только суммарный счётчик; follow-up).
- React admin-CRUD UI (бэк в v1; UI follow-up).

## Архитектура

```
PartnerPromo (CMS, mirror InfoPage) — migration 0099
  ├── crud/partner_promo.py: list_active/get/create/update/delete/increment_click(atomic) + _is_safe_url(https)
  ├── public redirect: GET /partner-promo/{id}/go → inc click → 302 promo.url
  ├── bot: «🤝 Партнёры» menu (gated) → cards → URL button to /go
  ├── cabinet: PartnerShowcase.tsx + GET /cabinet/partner-promos → cards → /go
  └── admin: admin_partner_promos.py CRUD (https validation)
config: PARTNER_SHOWCASE_ENABLED (env, default OFF)
```

## Поток данных

1. Админ создаёт PartnerPromo (https URL, multilingual title) через admin-API.
2. Юзер открывает «Партнёры» (бот/кабинет) → список активных карточек.
3. Клик → `/partner-promo/{id}/go` → счётчик++ → 302 на партнёрский URL.
4. Админ видит click_count.

## Обработка ошибок

- url не https → отклонить при create/update (link-safety).
- promo неактивен/удалён → `/go` 404.
- increment_click атомарен (UPDATE-инкремент) → нет гонки.
- PARTNER_SHOWCASE_ENABLED False → меню/страница/эндпоинты 404/скрыты.
- Пустой список → пункт меню скрыт.

## Тестирование

Backend юнит (`tests/.../test_partner_promo.py`):
- create/update отклоняет не-https url (javascript:/http:/data:).
- list_active: только is_active, сортировка sort_order.
- increment_click: атомарный инкремент (мок execute проверяет update-stmt).
- redirect-эндпоинт: активный → 302 на url + счётчик++; неактивный → 404;
  disabled flag → 404.
- multilingual title fallback (нет языка юзера → дефолт/первый).

Frontend: список рендерится, кнопка ведёт на /go. Ручная проверка (vitest
нет — см. speedtest).

## Rollback

- За `PARTNER_SHOWCASE_ENABLED` (env, дефолт OFF).
- Миграция 0099 обратима (drop_table).
- `git revert` + `alembic downgrade 0098`.

## Open questions

Решено: показ = бот + кабинет; клики считаем через redirect. Link-safety =
https-only. Admin React-UI + per-day аналитика — follow-up.
