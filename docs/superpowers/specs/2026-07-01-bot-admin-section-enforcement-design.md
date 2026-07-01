# Дизайн: включить секционные права админов бота (enforcement)

**Дата:** 2026-07-01
**Ветка:** feat/bot-admin-section-enforcement
**Статус:** утверждён к реализации

## Проблема

UI выдачи админки (`BotAdminRole`, [handlers/admin/bot_roles.py](../../../app/handlers/admin/bot_roles.py))
рисует галочки секций (users, payments, …), но **ни одна не работает**. Два
механизма enforcement написаны, но не подключены:

1. **`AdminPermissionMiddleware`** ([middlewares/admin_permission.py:117](../../../app/middlewares/admin_permission.py))
   — мапит callback → секцию и режет доступ. **Не зарегистрирован** в
   [bot.py](../../../app/bot.py). Grep по имени: только определение + свой лог.
2. **`@role_required(section)`** ([utils/decorators.py:67](../../../app/utils/decorators.py))
   — проверяет секцию. **Ни на одном handler не висит.**

Единственный работающий гейт — `@admin_required` ([decorators.py:37-39](../../../app/utils/decorators.py)):
пускает любого, у кого есть *хоть какая* `BotAdminRole`, секцию не смотрит.
`AuthMiddleware` ([auth.py:250-255](../../../app/middlewares/auth.py)) так же ставит
`is_admin=True` при наличии роли.

**Итог:** галочки секций — косметика. Выдал роль только с `support` → юзер
получает **полный** админ (users, payments, settings, servers, сами роли →
может выдать себе ещё). Privilege escalation. Роль с пустым `permissions=[]` =
тоже полный админ.

## Решения (из брейншторма)

| Вопрос | Решение |
|--------|---------|
| Модель прав | **Включить секции по-настоящему.** Галочки должны реально ограничивать. |
| Механизм | **A — зарегистрировать существующий `AdminPermissionMiddleware`.** Не декоратор `@role_required` (1 строка против навешивания на ~40 модулей — пропустишь = дыра). |
| Управление ролями | **Только суперадмин (ADMIN_IDS).** Section-админ не может выдать себе больше. |
| Объём | **Всё сразу: C1-C5.** |

## Критерий успеха

- Роль с `['support']`: открывает админ-панель, жмёт тикеты/поддержку; любая
  другая секция → `ACCESS_DENIED`.
- Суперадмин (ADMIN_IDS): полный доступ, всё как раньше.
- Роль с пустым `permissions=[]`: открывает панель, но каждая секция → deny
  (больше не эскалация).
- Управление ролями (`admin_bot_roles`, `bot_role_*`): доступно только
  суперадмину; section-админ с `settings` — нет.
- Section-админ в главном меню видит только свои кнопки.
- Всё проверено тестами.

## Компоненты

### C1 — Enforcement (ядро)

Зарегистрировать `AdminPermissionMiddleware` в
[bot.py](../../../app/bot.py) на `dp.callback_query` **после** `AuthMiddleware`
(стр. 193-194 — middleware читает `data['db_user']`, который ставит
`AuthMiddleware`):

```python
from app.middlewares.admin_permission import AdminPermissionMiddleware
...
dp.callback_query.middleware(AdminPermissionMiddleware())  # после AuthMiddleware
```

Middleware уже готов: суперадмин bypass ([:142](../../../app/middlewares/admin_permission.py)),
навигация `admin_panel`/`admin_submenu_` разрешена ([:134](../../../app/middlewares/admin_permission.py)),
неизвестный `admin_*` мягко падает на `@admin_required` ([:152-156](../../../app/middlewares/admin_permission.py)).
Правок в самом middleware не требуется (кроме C2/C3 ниже).

### C2 — Аудит мапы секций

`ADMIN_CALLBACK_SECTION_MAP` ([:27-95](../../../app/middlewares/admin_permission.py))
покрывает не все `admin_*` callbacks. Непокрытый callback уходит в мягкий
fallback (= доступен любому админу с любой ролью) — утечка прав.

- Собрать полный список зарегистрированных `admin_*` callback-префиксов
  (grep по `callback_data='admin_`, `F.data == 'admin_`, `F.data.startswith('admin_`
  во всех `handlers/admin/*` и клавиатурах).
- Каждый сопоставить с секцией; дописать пропущенные (кандидаты из register в
  [bot.py:201-270](../../../app/bot.py): `admin_freeze`, `admin_birthday`,
  `admin_bio_reward`, `admin_welcome_text`, `admin_wl_analytics`, `digest`,
  `admin_bulk_ban`/`admin_blocked`/`admin_blacklist` — проверить фактические
  callbacks у каждого).
- Fallback оставить мягким (безопасный rollout), но **логировать** непокрытые
  `admin_*` callbacks на уровне INFO/WARNING, чтобы дозаполнить мапу по факту.

### C3 — Управление ролями = только суперадмин

Сейчас `bot_role_*` callbacks не начинаются с `admin_` → middleware их
пропускает ([:130](../../../app/middlewares/admin_permission.py)). Вход
`admin_bot_roles` замаплен на `settings`, значит settings-админ мог бы войти и
через `bot_role_*` выдать себе всё.

- Новый декоратор `super_admin_required` в
  [utils/decorators.py](../../../app/utils/decorators.py): пропускает только
  `settings.is_admin(user.id)` (env ADMIN_IDS), иначе `ACCESS_DENIED`.
- Заменить `@admin_required` → `@super_admin_required` на **всех** handler в
  [bot_roles.py](../../../app/handlers/admin/bot_roles.py): `admin_bot_roles`,
  `bot_role_view/add/edit/toggle/save/delete/delete_confirm`,
  `bot_role_add_telegram_id`.
- В `ADMIN_CALLBACK_SECTION_MAP` строку `('admin_bot_roles', 'settings')`
  оставить (декоратор строже — двойная защита), либо убрать; декоратор — истина.

### C4 — Фильтр клавиатуры (UX)

`get_admin_main_keyboard` ([keyboards/admin.py:13](../../../app/keyboards/admin.py))
принимает только `language` и рисует все кнопки. Без фильтра section-админ видит
кнопки, дающие `ACCESS_DENIED`-алерт.

- Добавить параметры: `permissions: list[str] | None` и `is_super: bool`.
- Внутри — маппинг «кнопка → секция» (dict, зеркало логики
  `ADMIN_CALLBACK_SECTION_MAP`). Суперадмин → все кнопки. Section-админ →
  только кнопки, чья секция ∈ `permissions`.
- Кнопку **«👑 Роли»** ([admin.py:81-84](../../../app/keyboards/admin.py)) —
  только если `is_super`.
- Вызов в `show_admin_panel` ([handlers/admin/main.py:61](../../../app/handlers/admin/main.py)):
  посчитать права (`settings.is_admin` → super; иначе `role.permissions or []`)
  и передать в клавиатуру.
- Если у бота есть submenu-клавиатуры с секционными кнопками — отфильтровать
  так же (проверить при реализации).

### C5 — Правки корректности (в любом случае)

| Баг | Файл | Фикс |
|-----|------|------|
| FSM-стирание: потеря стейта (рестарт, MemoryStorage) → save пишет `[]`, показывает «Роль сохранена!» | [bot_roles.py:240-247](../../../app/handlers/admin/bot_roles.py) | На save: `data = await state.get_data(); if 'selected_permissions' not in data:` → ответить «Сессия истекла, откройте роль заново», `return` (не писать). То же для `bot_role_toggle`. |
| Пустая роль на save = бесполезная выдача | [bot_roles.py:244](../../../app/handlers/admin/bot_roles.py) | Если `selected` пуст → «Выберите хотя бы одну секцию», не сохранять. |
| NULL `permissions` → `TypeError`, глотается `@error_handler` | [bot_roles.py:184,200](../../../app/handlers/admin/bot_roles.py) | `list(existing.permissions or [])`, `list(role.permissions or [])`. |
| `created_by` перезатирается при update | [crud/bot_role.py:56-58](../../../app/database/crud/bot_role.py) | В ветке update не трогать `created_by` (ставить только при create). |
| B1: «не найден в базе» без объяснения | [bot_roles.py:180](../../../app/handlers/admin/bot_roles.py) | Текст: «Пользователь ещё не запускал бота. Попросите его открыть бота, затем выдайте роль.» |

> B1 (нельзя выдать роль тому, кто ни разу не открывал бота) остаётся
> ограничением: роль ключуется на `users.id`, а строка появляется только после
> первого захода. Pre-seed юзера — отдельная задача, в объём не входит; здесь
> только понятное сообщение.

## Разбиение на юниты (изоляция)

- **Enforcement** — middleware, одна точка входа, тестируется отдельно
  (`resolve_admin_section` + `__call__`).
- **Super-admin gate** — декоратор, чистая функция от `user.id` + ADMIN_IDS.
- **Keyboard filter** — чистая функция `(permissions, is_super) → markup`,
  тестируется без БД.
- **CRUD-правки** — `set_bot_role`, независимо.
- **Handler-правки** — FSM presence-check, независимо.

## Тесты

- `test_resolve_admin_section`: репрезентативные callbacks → правильная секция;
  неизвестный → `None`.
- `AdminPermissionMiddleware`: role-админ без секции → deny; с секцией → pass;
  суперадмин → bypass; `admin_panel`/`admin_submenu_` → pass; неизвестный
  `admin_*` → fall-through.
- `super_admin_required`: role-админ (даже с `settings`) на `bot_role_*` → deny;
  суперадмин → pass.
- FSM: save при пустом стейте → нет записи, показано сообщение; save с
  `selected_permissions=[]` (явно) → «выберите ≥1 секцию».
- `set_bot_role`: update сохраняет исходный `created_by`.
- Keyboard filter: section-админ видит только свои кнопки; «👑 Роли» только у
  суперадмина.

## Порядок реализации

1. C5 CRUD/handler правки + тесты → verify: тесты зелёные.
2. C3 `super_admin_required` + применить в bot_roles + тесты → verify: role-админ
   заблокирован на управлении ролями.
3. C1 регистрация middleware (после AuthMiddleware) + тесты → verify: секции
   режут.
4. C2 аудит и дозаполнение мапы + лог непокрытых → verify: все `admin_*`
   замаплены либо логируются.
5. C4 фильтр клавиатуры + тесты → verify: section-админ видит только своё.
6. Полный прогон тестов → verify: всё зелёное.
