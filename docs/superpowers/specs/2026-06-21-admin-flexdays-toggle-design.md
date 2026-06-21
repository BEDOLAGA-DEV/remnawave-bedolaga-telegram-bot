# Bot-admin toggle for flexible_days_enabled

**Дата:** 2026-06-21
**Ветка:** `feat/admin-flexdays-toggle`
**Статус:** дизайн утверждён, ожидает ревью спека

## 1. Цель

Дать админу включать/выключать `flexible_days_enabled` (кнопка «✏️ Свой срок»)
на тарифе из Telegram-админки, без SQL. `crud.update_tariff` уже принимает флаг.

## 2. Зафиксированные решения

- Только **бот-админка** (Telegram). Cabinet/React — вне scope.
- Минимум: тумблер булева флага по образцу существующих
  (`toggle_tariff`/`toggle_trial_tariff`/`toggle_tariff_traffic_topup`).

## 3. Изменения (всё в `app/handlers/admin/tariffs.py`)

### A. Кнопка в `get_tariff_view_keyboard` (~256, перед блоком триала)
```python
    if getattr(tariff, 'flexible_days_enabled', False):
        buttons.append([InlineKeyboardButton(text='✏️ Свой срок: ✅', callback_data=f'admin_tariff_toggle_flexdays:{tariff.id}')])
    else:
        buttons.append([InlineKeyboardButton(text='✏️ Свой срок: ❌', callback_data=f'admin_tariff_toggle_flexdays:{tariff.id}')])
```

### B. Хендлер `toggle_tariff_flexible_days` (после `toggle_trial_tariff` ~616)
По образцу `toggle_trial_tariff`: fetch → `update_tariff(db, tariff, flexible_days_enabled=not bool(...))`
→ re-render `format_tariff_info` + `get_tariff_view_keyboard`. В `callback.answer` —
короткий статус; если включили и `len(tariff.get_available_periods()) < 2` — добавить
заметку «добавьте ≥2 периода» (свой срок при одном якоре бесполезен, не блокирует).

### C. Дисплей в `format_tariff_info` (блок «Параметры», ~414)
Добавить строку `• Свой срок (произвольные дни): {flexdays_status}` где
`flexdays_status = '✅' if getattr(tariff, 'flexible_days_enabled', False) else '❌'`.

### D. Регистрация (~3249)
```python
    dp.callback_query.register(toggle_tariff_flexible_days, F.data.startswith('admin_tariff_toggle_flexdays:'))
```
И добавить в фильтр `toggle_tariff` (defensive, как у прочих toggle):
`& ~F.data.startswith('admin_tariff_toggle_flexdays:')`.

## 4. Краевые случаи

- Тариф не найден → `callback.answer('Тариф не найден', show_alert=True)` (как в образце).
- Включение при <2 периодах → флаг ставится, но в ответе заметка (свой срок клампится к одному якорю).
- Колбэк-префикс: `admin_tariff_toggle:` не матчит `admin_tariff_toggle_flexdays:` (двоеточие vs `_`), но исключение добавляем для консистентности с существующим фильтром.

## 5. Тесты

- Лёгкий: модуль импортируется; `toggle_tariff_flexible_days` существует и зарегистрирован
  на `admin_tariff_toggle_flexdays:` (source-inspection или import + hasattr).

## 6. Вне scope

- Cabinet (React) чекбокс.
- Настройка периодов/тиров (уже есть отдельно).
