# Тарифные тиры устройств (нелинейная цена за кол-во устройств)

**Дата:** 2026-06-21
**Ветка:** `feat/tiered-device-pricing`
**Статус:** дизайн утверждён, ожидает ревью спека

## 1. Цель

Дать админу задавать **нелинейную** цену за кол-во устройств в тарифе (булк-скидка),
сохранив текущий UX: юзер выбирает срок и оплачивает, затем добавляет устройства
вживую без пере-оформления подписки. Гигабайты безлимитны (`traffic_limit_gb=0`),
`_wl` — отдельный бакет с докупкой (без изменений).

Пример целевой сетки (база = 1 устройство = `period_prices`):

| Устройств | Итоговая цена (30 дней) |
|-----------|-------------------------|
| 1 | 30 ₽ |
| 3 | 70 ₽ |
| 5 | 100 ₽ |

Линейная докупка (один `device_price_kopeks`) такую сетку выразить не может —
нужна таблица тиров + интерполяция.

## 2. Зафиксированные продуктовые решения

1. **Семантика тира** — доплата сверх базы. База (включённые `device_limit` устройства)
   = `period_prices`. Тир хранит **доплату коп/мес** сверх базы.
2. **Масштаб по периоду** — помесячно × кол-во месяцев периода. Прорейт при докупке
   в середине периода (кап 1 месяц, как сейчас). Re-bill при продлении через движок.
3. **Выбор кол-ва** — юзер берёт **любое** значение `1..max_device_limit`. Цена для
   значений, отсутствующих в таблице тиров, считается **линейной интерполяцией** между
   соседними якорями.
4. **Настройка** — и в Telegram-админке, и в web-кабинете (React `bedolaga-cabinet`).
5. **Обратная совместимость** — поле аддитивное; пустые тиры → старый линейный
   `device_price_kopeks`. Существующие тарифы не меняют поведение.
6. **Classic-режим** (не-тариф, `PRICE_PER_DEVICE`) — вне scope, остаётся линейным.

## 3. Модель данных

Новое поле в `Tariff` ([app/database/models.py](../../../app/database/models.py), после `max_device_limit` ~строка 1740):

```python
# {"3": 4000, "5": 7000} — total_device_count: extra_kopeks_per_month сверх базы.
# Пусто = использовать линейный device_price_kopeks (старое поведение).
device_price_tiers = Column(JSON, default=dict)
```

- **Ключ** = полное кол-во устройств (строка), **значение** = доплата коп/мес сверх базы.
- Якорь `(device_limit, 0)` — неявный (базовые устройства бесплатны).
- `device_price_kopeks` **сохраняется** как линейный fallback. Не удаляется.

Хелпер в модели (единое место расчёта):

```python
def get_device_extra_price_per_month(self, total_count: int) -> int:
    """Доплата коп/мес сверх базы за total_count устройств.

    Источник: device_price_tiers (интерполяция/экстраполяция), иначе линейный
    fallback на device_price_kopeks. Базовые device_limit устройств = 0.
    """
```

Миграция `0117_add_device_price_tiers_to_tariffs.py`
(`down_revision='0116'`), по образцу `0063_add_wl_tariff_traffic_fields.py`:
`upgrade()` — ADD COLUMN `device_price_tiers` JSON default `{}`;
`downgrade()` — DROP COLUMN. Только аддитивно, без потери данных.

## 4. Алгоритм цены (интерполяция)

Якоря = `{device_limit: 0}` ∪ `device_price_tiers` (ключи→int, отсортированы).

Для запрошенного полного кол-ва `N`:

- `N <= device_limit` → `0` (базовые бесплатны).
- `N` совпал с якорем → значение якоря.
- `N` между якорями `a < N < b` →
  `extra(a) + (extra(b) - extra(a)) * (N - a) / (b - a)`, округление до целых копеек.
- `N` выше верхнего якоря `t` (но `≤ max_device_limit`) →
  **экстраполяция по наклону последнего сегмента** `(prev_t, t)`
  (монотонно, без «бесплатных» устройств). Если якорь один — наклон от `(device_limit, 0)`.

Результат — доплата **коп/мес**. Итог за период = `extra(N) * months`.
Прорейт при докупке = `extra(N) * min(days_left, 30) / 30` (текущий кап).

Проверка на целевой сетке (`device_limit=1`, база 30 ₽, тиры `{"3":4000,"5":7000}`):

| N | расчёт extra/мес | доплата | итого 30 д |
|---|------------------|---------|-----------|
| 1 | якорь | +0 ₽ | 30 ₽ |
| 2 | interp(1→3): 4000·(1/2) | +20 ₽ | 50 ₽ |
| 3 | якорь | +40 ₽ | **70 ₽** ✓ |
| 4 | interp(3→5): 4000+3000·(1/2) | +55 ₽ | 85 ₽ |
| 5 | якорь | +70 ₽ | **100 ₽** ✓ |

## 5. Точки изменения

Расчёт всегда идёт через `Tariff.get_device_extra_price_per_month()` — никаких
повторов формулы интерполяции.

### Бэкенд — расчёт
- **app/services/pricing_engine.py** `_calculate_tariff_core` (~587–596):
  замена `extra_devices * device_price_per_unit` на `extra(total)` помесячно.
  `devices_price_per_month` = `extra(total)` (сохранить для legacy-дисплея
  `classic_pricing_to_purchase_details` ~920–970). Renewal идёт через движок → авто-покрыт.
- **app/handlers/subscription/tariff_purchase.py** `_calc_extra_devices_cost` (~2133):
  тот же хелпер.

### Бэкенд — докупка в боте
- **app/handlers/subscription/devices.py** (`handle_change_devices` ~164,
  `confirm_change_devices` ~259, `execute_change_devices` ~513, `confirm_add_devices` ~1407):
  цена доплаты = `extra(new) − extra(current)`, прорейт + скидка `devices_pct`.
  Селектор остаётся диапазонным (`1..max`). Гейт доступности:
  «есть `device_price_tiers` ИЛИ `device_price_kopeks > 0`».
- **app/keyboards/inline.py** `get_change_devices_keyboard` (~2916): цена кнопки —
  дельта `extra` через хелпер.

### Бэкенд — miniapp / webapi
- **app/webapi/routes/miniapp.py** (~5021, ~6072, ~6567): расчёт/гейт через хелпер.
- **app/webapi/routes/subscriptions.py**: опц. — выдать структуру цен устройств, если UI покажет тиры (не обязательно для MVP).

### Настройка — Telegram-админка
- **app/handlers/admin/tariffs.py**: хелперы `_parse_device_price_tiers("3:4000,5:7000")`,
  `_format_device_price_tiers_display/_for_edit` (по образцу `_parse_period_prices`).
  `format_tariff_info` (~313): показывать тиры. `start_edit_tariff_device_price` /
  `process_edit_tariff_device_price` (~1457–1540): новый prompt + парс. Стейт
  `editing_tariff_device_price` переиспользуется.

### Настройка — Cabinet API
- **app/cabinet/schemas/tariffs.py**: `device_price_tiers: dict[str,int]` в
  `TariffDetailResponse`, `TariffCreateRequest`, `TariffUpdateRequest`
  (зеркало `wl_traffic_topup_packages`).
- **app/cabinet/routes/admin_tariffs.py**: `get_tariff` / `create_new_tariff` /
  `update_existing_tariff` — провод поля.
- **app/database/crud/tariff.py**: `create_tariff` / `update_tariff` — параметр
  `device_price_tiers`.

### Настройка — React-кабинет
- **bedolaga-cabinet/src/pages/AdminTariffCreate.tsx** — единый компонент create+edit
  (грузит data через useQuery): state + load + submit + редактор-таблица count→цена
  (зеркало `trafficTopupPackages`-редактора). Отдельного edit-файла нет;
  `AdminTariffs.tsx` — только список.
- **bedolaga-cabinet/src/api/tariffs.ts**: `device_price_tiers?: Record<number,number>`
  в `TariffDetail`/`TariffCreateRequest`/`TariffUpdateRequest`.
- **bedolaga-cabinet/src/locales/{ru,en,zh,fa}.json**: ключи редактора тиров.

## 6. Обратная совместимость

- Колонка default `{}` → существующие тарифы получают пустые тиры.
- Пустые тиры → старый код-путь `device_price_kopeks` (1-в-1 текущее поведение).
- `device_price_kopeks` (поле, бот-ввод, cabinet-схема, API) не удаляется.
- Живые подписки `device_limit>1` работают; продление как раньше, пока у тарифа нет тиров.
- Единственное изменение — **opt-in**: админ сам добавил тиры в тариф → будущие
  списания/продления этого тарифа идут по тирам. Намеренно.
- Миграция должна примениться до деплоя кода, читающего поле (иначе AttributeError
  на существующих инсталляциях).

## 7. Краевые случаи

- `N > max_device_limit` — отклонить (текущая проверка лимита сохраняется).
- Уменьшение кол-ва — возврат не делается (текущее поведение), сброс лишних
  устройств на панели — как сейчас.
- Прорейт-кап 30 дней при докупке в середине периода — сохранить.
- Daily-тарифы (`is_daily`, period ≤ 1 день): тиры помесячные → делить на 30 для
  суточного расчёта (как линейная ветка сейчас).
- Скидка `devices_pct` применяется к результату тира (после интерполяции, до стека).
- Валидация ввода тиров: ключи — целые ≥ 2 (или ≥ `device_limit+1`), значения ≥ 0,
  без дублей. Парсер сортирует.

## 8. Тесты

- **pricing_engine** (unit): интерполяция на целевой сетке 1/3/5 + промежуточные 2/4;
  экстраполяция выше верхнего якоря; прорейт-кап 30 д; стек скидок `devices_pct`;
  renewal-консистентность; fallback на `device_price_kopeks` при пустых тирах;
  один якорь (наклон от базы).
- **bot devices** (если есть харнес): дельта-цена при апгрейде/частичном.
- **cabinet route**: create/update с `device_price_tiers` round-trip; get отдаёт поле.
- **admin parse**: `_parse_device_price_tiers` валидные/битые строки.

## 9. Объём

~14 файлов: модель + миграция + crud (3), pricing_engine + tariff_purchase + devices +
inline-keyboards (4), miniapp/webapi (1–2), бот-админка (1), cabinet schemas + routes (2),
React страница + api + локали (1 блок), тесты. Всё ложится на готовые паттерны
`wl_traffic_topup_packages` / `period_prices`.

## 10. Вне scope

- Classic-режим (`PRICE_PER_DEVICE`) — линейный, без тиров.
- Безлимит GB и `_wl` докупка — без изменений (готовая конфигурация).
- Глобальная таблица тиров по умолчанию (settings-level) — не нужна; тиры per-tariff.
