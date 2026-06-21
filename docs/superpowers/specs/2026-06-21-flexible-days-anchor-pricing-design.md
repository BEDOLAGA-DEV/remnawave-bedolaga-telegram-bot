# Flexible custom-days with floor-anchor pricing

**Дата:** 2026-06-21
**Ветка:** `feat/flexible-days-pricing`
**Статус:** дизайн утверждён, ожидает ревью спека

## 1. Цель

Дать юзеру вводить **произвольное число дней** при покупке тарифа, с ценой,
выведенной из кнопок-периодов (`period_prices`) по правилу **floor-anchor с капом**.
Совмещается с уже отгруженными device-тирами (доплата за устройства сверху).

Отдельно от существующего «плоского» custom-days (`custom_days_enabled` +
`price_per_day_kopeks`) — тот не трогаем.

## 2. Зафиксированные решения

1. **Цена** — floor-anchor с капом следующим якорем (монотонно):
   `base(D) = min(D × rate_floor, price_ceil)`, округление до рубля.
2. **Ввод** — кнопки-якоря + кнопка **«✏️ Свой срок»** (ввод числа текстом). Оба.
3. **Мин/макс дни** = наименьший / наибольший якорь `period_prices`.
4. **Конфиг** — новый флаг `Tariff.flexible_days_enabled` (миграция 0118).
5. **Устройства** — доплата сверх базы, `months = D/30` (через движок). Total = base(D) + device_extra.
6. **Вне scope** — совмещение с custom-traffic, daily-тарифы, старый flat custom-days.

## 3. Формула цены (floor-anchor + кап)

Якоря = `period_prices` (дни→коп), отсортированы. Для `D` (в `[min_anchor..max_anchor]`):

- `D` совпал с якорем → `price[D]`.
- Иначе: `floor` = наибольший якорь `< D`, `ceil` = наименьший якорь `> D`.
  `rate_floor = price[floor] / floor`.
  `base(D) = round_to_ruble( min(D × rate_floor, price[ceil]) )`.

Якоря 30→30₽, 90→70₽, 180→100₽ (rate: 30=1.0/д, 90=0.777/д, 180=0.555/д):

| D | floor | D×rate | cap (ceil) | base |
|---|------|--------|-----------|------|
| 50 | 30 | 50 | 70 | **50₽** |
| 80 | 30 | 80 | 70 | 70₽ |
| 120 | 90 | 93.3 | 100 | **93₽** |
| 179 | 90 | 139 | 100 | 100₽ |
| 30/90/180 | — | — | — | 30/70/100₽ (точный якорь) |

Монотонно неубывающая по D, никогда не дороже следующего якоря.

## 4. Модель + миграция

Новое поле `Tariff` (после `device_price_tiers`):
```python
# Произвольный срок с ценой floor-anchor из period_prices. Отдельно от flat custom_days.
flexible_days_enabled = Column(Boolean, default=False, nullable=False, server_default='false')
```
Метод-хелпер (единое место расчёта):
```python
def get_price_for_days_anchored(self, days: int) -> int:
    """База в копейках за произвольный срок по floor-anchor с капом следующим якорем."""
```
Хелперы границ: `Tariff.get_available_periods()` уже даёт отсортированный список —
`min_anchor = periods[0]`, `max_anchor = periods[-1]`.

Миграция `0118_add_flexible_days_enabled_to_tariffs.py` (down_revision `0117`),
идемпотентная (inspector), ADD COLUMN bool default false. По образцу 0117.

## 5. Движок (единый источник цены)

`pricing_engine.calculate_tariff_purchase_price(tariff, period=D, device_limit=N, ...)`:
- Резолв базы периода: если `D` — точный ключ `period_prices` → как сейчас;
  иначе, если `tariff.flexible_days_enabled` и `D` в `[min..max]` →
  `base = tariff.get_price_for_days_anchored(D)`.
- Устройства: `device_extra = extra_per_month(N) × (D/30)` (текущая months-логика движка;
  для произвольного D месяцы = D/30, прорейт). Скидки как есть.
- `result.final_total` = база + устройства (со скидками).

## 6. Flow (бот)

1. Экран периода (`_render_tariff_entry` → `get_tariff_periods_keyboard`):
   если `flexible_days_enabled` — добавить кнопку **«✏️ Свой срок»**
   (callback `nz!_tariff_flexdays:{tariff_id}`).
2. Хендлер «Свой срок»: `state.set_state(SubscriptionStates.selecting_custom_days)`,
   сохранить `selected_tariff_id`, prompt: «Введите число дней (от {min} до {max})».
3. Текстовый хендлер ввода: парс int, clamp `[min_anchor..max_anchor]`, сохранить
   `selected_device_limit`-дефолт по необходимости, затем вызвать
   `select_tariff_period(callback_or_message_ctx, db_user, db, state, tariff_id=ID, period=D)`
   — переиспользуем опц. параметры (добавлены в фикс frozen-callback), которые рендерят
   тот же confirm-экран с device −/＋. Невалидный ввод → сообщение + повтор prompt.
   (Нюанс: текст-хендлер получает `Message`, а `select_tariff_period` ждёт `CallbackQuery`
   для `.message.edit_text`/`.answer` — рендер-часть надо вызвать так, чтобы работать
   с `message.answer`; уточнить в плане: либо общий рендер принимает `message`, либо
   отправляем новое сообщение с confirm-клавиатурой.)
4. Confirm-экран и device −/＋ работают как сейчас (period=D в state/коллбеках).

Кнопка «Назад» из ввода/подтверждения — на экран периода (или меню при одном тарифе).

## 7. Покупка

`confirm_tariff_purchase`: текущая валидация `str(period) not in period_prices → reject`.
Смягчить: разрешить `D`, если `tariff.flexible_days_enabled` и `min_anchor ≤ D ≤ max_anchor`
(иначе — прежняя проверка). Цена через движок (floor-anchor база + устройства). Подписка на `D` дней.
Ветка «недостаточно средств» / корзина — `period_days=D`.

device −/＋ callbacks (`nz!_tariff_dev:{id}:{D}:{N}`) уже несут period — для custom D
тоже работают (D в позиции period).

## 8. Краевые случаи

- < 2 якорей у тарифа → floor/ceil вырождены: при D вне точного якоря и единственном
  якоре — `rate = price/anchor_days`, кап = тот же якорь; `base = min(D×rate, price)`.
  (Практически flexible включают при ≥2 периодах.)
- D вне `[min..max]` → clamp.
- Невалидный/нечисловой ввод → сообщение + повтор prompt, без краша.
- `flexible_days_enabled=false` → кнопки «Свой срок» нет, поведение как сейчас.
- Старый flat custom-days (`custom_days_enabled` + `price_per_day_kopeks`) — отдельный путь, не затронут.

## 9. Тесты

- `get_price_for_days_anchored`: целевая сетка (50/80/120/179 + точные якоря 30/90/180);
  единственный якорь; кап срабатывает.
- Движок: base+device для custom D (N устройств, months=D/30).
- Flow (если харнес): clamp дней, невалидный ввод.
- confirm: пускает D в диапазоне при flexible_days_enabled; отклоняет вне диапазона.

## 10. Объём / файлы

- `app/database/models.py` — поле `flexible_days_enabled` + метод `get_price_for_days_anchored`.
- миграция `0118`.
- `app/database/crud/tariff.py` — параметр create/update.
- `app/services/pricing_engine.py` — floor-anchor база для custom D.
- `app/handlers/subscription/tariff_purchase.py` — кнопка «Свой срок», хендлеры (открытие + текст-ввод), confirm-валидация, рендер.
- `app/keyboards` / локальные keyboard-функции — кнопка в period-клавиатуре.
- состояние `selecting_custom_days` (уже есть в `SubscriptionStates`).
- Тесты.

**MVP-scope (этот план):** модель+миграция, метод цены, движок, flow (кнопка + ввод + рендер),
confirm-валидация, тесты. Флаг `flexible_days_enabled` на тарифе включаем напрямую в БД
(как делали с `device_price_tiers`). **Admin-UI для флага** (бот-админка + cabinet schemas/routes + React) —
**отдельный под-этап после MVP**, не входит в этот план.

## 11. Вне scope

- Совмещение flexible-days с custom-traffic в одном экране.
- Daily-тарифы.
- Старый flat custom-days.
- Полный admin-UI флага (бот+cabinet+React) — выносим в отдельный под-этап, если нужно.
