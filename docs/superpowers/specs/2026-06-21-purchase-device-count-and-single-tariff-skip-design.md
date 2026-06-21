# Purchase flow: device-count selection + single-tariff skip

**Дата:** 2026-06-21
**Ветка:** `feat/purchase-device-count`
**Статус:** дизайн утверждён, ожидает ревью спека

## 1. Цель

Две UX-правки flow покупки тарифа (`SALES_MODE=tariffs`):

1. **Скип экрана «Выберите тариф»**, когда активен ровно один тариф — сразу вести в его flow.
2. **Выбор начального кол-ва устройств при покупке** (−/＋ на экране подтверждения), цена по `device_price_tiers`. После покупки кол-во меняется существующим «Изменение устройств».

Опирается на уже отгруженное на master: `Tariff.device_price_tiers`,
`Tariff.get_device_extra_price_per_month`, поддержку `device_limit` в
`pricing_engine.calculate_tariff_purchase_price`.

## 2. Зафиксированные решения

1. Кол-во устройств — на экране **подтверждения** кнопками −/＋ с живой итоговой ценой.
2. Шаг устройств показывать **только если** у тарифа есть цена устройств
   (`device_price_tiers` непусто ИЛИ `device_price_kopeks > 0`) И `max_device_limit > device_limit`.
3. Скип списка — когда **ровно 1** активный тариф.
4. Цена превью и списания — единый источник: `pricing_engine.calculate_tariff_purchase_price(..., device_limit=N)`.
5. Daily-тарифы и custom-дни/трафик confirm — **вне scope** (там `device_limit=база`).

## 3. A — Скип «Выберите тариф» при одном тарифе

`show_tariffs_list` ([tariff_purchase.py:590](../../../app/handlers/subscription/tariff_purchase.py)):
если `len(tariffs) == 1` → не рендерить список, а сразу выполнить рендер выбранного
тарифа (та же логика, что `select_tariff` для `tariffs[0].id`).

**Навигация «Назад»:** экраны периода/подтверждения сейчас ведут «Назад» на
`nz!_tariff_list`. При одном тарифе это зациклит на тот же тариф. Решение: прокинуть
`back_callback` в `get_tariff_periods_keyboard` / `get_tariff_periods_keyboard_with_traffic`
/ `get_tariff_confirm_keyboard`; при одном тарифе передавать `nz!_back_to_menu`, иначе
`nz!_tariff_list` (как сейчас). Признак «один тариф»: `show_tariffs_list` уже знает
`len(tariffs)`; при скипе кладёт `single_tariff=True` в state, и
`select_tariff` / `select_tariff_period` читают флаг из state для выбора `back_callback`.

## 4. B — Выбор кол-ва устройств на подтверждении

### Условие показа −/＋
`devices_selectable = (bool(tariff.device_price_tiers) or (tariff.device_price_kopeks or 0) > 0)
and (effective_max > base)`, где `base = tariff.device_limit or 1`,
`effective_max = tariff.max_device_limit or settings.MAX_DEVICES_LIMIT` (если оба пусты → нет выбора).

### Состояние
В state хранится `selected_device_limit` (старт = `base`). Обновляется при −/＋.

### Экран подтверждения
`select_tariff_period` ([:1266](../../../app/handlers/subscription/tariff_purchase.py)) рендерит:
```
✅ Подтверждение покупки
📦 Тариф: <name>
📊 Трафик: <traffic>
📱 Устройств: N            ← было tariff.device_limit, стало selected_device_limit
📅 Период: <period>
💰 Итого: <final_price>     ← из движка с device_limit=N
```
Клавиатура `get_tariff_confirm_keyboard`:
- если `devices_selectable`: строка `[−] N устр. [＋]` (clamp `[base..effective_max]`),
  callback `nz!_tariff_dev:{tariff_id}:{period}:{N±1}`. На границах соответствующая
  кнопка заменяется на `nz!_noop` (визуально присутствует, значение не двигает).
- строка `✅ Подтвердить покупку` (callback `nz!_tariff_confirm:{id}:{period}` — N берётся из state).
- строка «Назад» (см. навигацию A).

### Хендлер −/＋
Новый `handle_tariff_device_change` на callback `nz!_tariff_dev:{id}:{period}:{N}`:
clamp N в `[base..effective_max]`, `state.update_data(selected_device_limit=N)`,
повторно отрисовать экран подтверждения (переиспользовать рендер `select_tariff_period`).
Зарегистрировать callback в роутере (`app/handlers/subscription/__init__.py` или где
регистрируются `nz!_tariff_*`).

## 5. C — Цена (движок как единый источник)

`select_tariff_period` для превью считает цену через
`pricing_engine.calculate_tariff_purchase_price(tariff, period, device_limit=selected_device_limit, user=db_user, db=db)`
вместо ручного `period_prices[period] − promo`. `result.final_total` — итог с устройствами.
Это убирает рассинхрон превью↔списание (списание уже идёт через движок).

`confirm_tariff_purchase` ([:1387](../../../app/handlers/subscription/tariff_purchase.py)):
- читать `selected_device_limit` из state; для НОВОЙ покупки передавать его в
  `calculate_tariff_purchase_price(device_limit=N)` и в создание подписки
  (`device_limit=N`); для существующей подписки — текущая логика
  `max(tariff.device_limit, existing.device_limit)` сохраняется.
- ветка «недостаточно средств» в `select_tariff_period`: `cart_data['device_limit'] = N`,
  `missing` пересчитан от итога с устройствами.

## 6. Краевые случаи

- Нет цены устройств или `effective_max ≤ base` → −/＋ не показывать, поведение как сейчас (фикс. база).
- N за пределами `[base..effective_max]` → clamp.
- Daily-тариф (`get_daily_tariff_confirm`) и custom-дни/трафик confirm → `device_limit=база`, без −/＋ (вне scope).
- Существующая подписка на тот же тариф (продление): кол-во устройств не понижается
  (`max(base, existing)`), выбор −/＋ при продлении — вне scope (по желанию, отдельно).

## 7. Тесты

- Clamp кол-ва устройств в `[base..effective_max]`.
- Цена превью == списание: оба через `calculate_tariff_purchase_price(device_limit=N)`
  (юнит на движок с тиром: N=3 → база+40₽×месяцы и т.п.).
- `devices_selectable` логика: тариф с тирами+max>base → True; без цены или max≤base → False.
- Скип списка при 1 активном тарифе (рендерит экран тарифа, не список); при ≥2 — список.
- Покупка с N устройств создаёт подписку с `device_limit=N`.

## 8. Объём / файлы

- `app/handlers/subscription/tariff_purchase.py`: `show_tariffs_list` (скип),
  `select_tariff_period` (device-aware превью + рендер), новый `handle_tariff_device_change`,
  `confirm_tariff_purchase` (chosen device_limit), keyboard-функции
  `get_tariff_periods_keyboard*` / `get_tariff_confirm_keyboard` (back_callback + device-ряд).
- Регистрация нового callback `nz!_tariff_dev` в роутере.
- Тесты в `tests/`.

## 9. Вне scope

- Daily / custom-days / custom-traffic confirm device-выбор.
- Выбор устройств при продлении существующей подписки (есть «Изменение устройств»).
- Изменения модели/миграций — не требуются (всё уже на master).
