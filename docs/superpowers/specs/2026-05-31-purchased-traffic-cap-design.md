# Кап накопления докупленного трафика (анти-абуз) — design

**Date:** 2026-05-31
**Scope:** ограничение суммарного активного докупленного трафика на подписку
**Status:** Draft
**Feature:** B4-доработка B (анти-абуз). Часть A (показ остатка+expiry) — УЖЕ реализована (bot `purchase.py:535`, cabinet `status.py:103`), не трогаем.

## Проблема

`add_subscription_traffic` (`crud/subscription.py:835`) не имеет потолка:
`purchased_traffic_gb` накапливается без ограничения. Юзер может докупать
трафик бесконечно, раздувая `traffic_limit_gb` (base + purchased) до
абсурда. Нужен конфигурируемый кап на суммарный **активный** докупленный
трафик.

## Решение

Конфиг `MAX_PURCHASED_TRAFFIC_GB` (env, дефолт `0` = без лимита =
текущее поведение). Когда `> 0`: перед покупкой проверяем, что
`active_purchased + new_gb <= cap`. Если превышает — **отклонить покупку**
(до списания денег) с понятным сообщением. Поведение при достижении —
reject (подтверждено).

«Активный докупленный» = сумма `traffic_gb` неистёкших `TrafficPurchase`
(`expires_at > now`) — НЕ `purchased_traffic_gb` (тот может расходиться с
реально активными после sweep'а). Считаем по таблице — источник истины.

### Компонент 1: конфиг

`settings.MAX_PURCHASED_TRAFFIC_GB: int = 0` + геттер с клампом
(`max(0, int(...))`, дефолт 0). 0 = выключено.

### Компонент 2: pre-check helper

В `crud/subscription.py`:

```python
async def get_active_purchased_traffic_gb(db, subscription_id) -> int:
    now = datetime.now(UTC)
    result = await db.execute(
        select(func.coalesce(func.sum(TrafficPurchase.traffic_gb), 0))
        .where(TrafficPurchase.subscription_id == subscription_id)
        .where(TrafficPurchase.expires_at > now)
    )
    return int(result.scalar() or 0)


async def can_add_purchased_traffic(db, subscription_id, gb) -> tuple[bool, int]:
    """Returns (allowed, remaining_headroom_gb). headroom = -1 when unlimited."""
    cap = settings.get_max_purchased_traffic_gb()
    if cap <= 0:
        return True, -1
    active = await get_active_purchased_traffic_gb(db, subscription_id)
    remaining = max(0, cap - active)
    return (gb <= remaining), remaining
```

### Компонент 3: проверка в точках покупки (ДО списания)

Кап-проверка ставится **перед** `subtract_user_balance` / charge, чтобы
не списать деньги и потом отклонить (избежать refund-мессы):

1. **Бот** `app/handlers/subscription/traffic.py` — перед `subtract_user_balance`
   (~стр. 639) в `add_traffic`. Если `not allowed` → alert «Достигнут
   лимит докупленного трафика ({cap} ГБ). Доступно ещё: {remaining} ГБ.» +
   return (без списания). Желательно также скрыть/дизейблить недоступные
   пакеты на экране выбора (`select_traffic` ~стр. 490) — опц., минимально
   достаточно блокировки перед оплатой.
2. **Кабинет/webapi** `app/webapi/routes/subscriptions.py:283` +
   `app/cabinet/routes/subscription_modules/traffic.py` (если есть аналог
   покупки) — перед charge/add → HTTP 400/409 `{code:'traffic_cap', ...}`.
3. **Admin bulk** `admin_bulk_actions.py:359` — админ-операция; кап НЕ
   применяем (админ осознанно начисляет), но логируем если превышает.
   (Кап — анти-абуз юзеров, не админа.)

### Компонент 4: `add_subscription_traffic` остаётся как есть

Функция общая (и для админа). Guard внутрь НЕ ставим (иначе сломаем
админ-bulk). Cap enforcement = на уровне юзер-flow (компонент 3), не
примитива. Документируем это решение комментарием.

## Что НЕ входит

- A (показ остатка+expiry) — уже есть в боте и кабинете.
- Изменение rollover-логики (сохранение докупленного при продлении) — уже
  работает.
- Кап на WL-докупки (`wl_purchased_traffic_gb`) — симметричная доработка,
  опц. follow-up (та же схема, отдельный конфиг). В v1 — только main.
- Кламп-до-остатка / частичная продажа — отклонено (reject проще).
- Блокировка админ-начисления.

## Архитектура

```
user buys traffic (bot traffic.py / cabinet / webapi)
  └── can_add_purchased_traffic(db, sub_id, gb)  ← BEFORE charge
        ├── cap = get_max_purchased_traffic_gb()  (0 = unlimited → allow)
        ├── active = SUM(TrafficPurchase.traffic_gb WHERE expires_at>now)
        └── allowed = (gb <= cap - active)
  ├── allowed=False → reject (no charge), message with remaining headroom
  └── allowed=True  → charge → add_subscription_traffic (unchanged)
admin bulk → no cap (logged if exceeds)
config: MAX_PURCHASED_TRAFFIC_GB (env, default 0 = off)
```

## Поток данных

1. Юзер выбирает пакет докупки → flow считает `active_purchased` по
   неистёкшим TrafficPurchase.
2. Если cap>0 и active+gb>cap → отклонить до оплаты, показать остаток.
3. Иначе списать + `add_subscription_traffic` (как сейчас).
4. Sweep истёкших докупок (daily) освобождает headroom естественно.

## Обработка ошибок

- cap=0 → helper всегда allow (нулевой риск регрессии в проде).
- Гонка (два параллельных докупа у одного юзера) → теоретически оба
  пройдут pre-check и суммарно чуть превысят cap. Низкий риск (юзер сам с
  собой), допустимо. Жёсткая сериализация (FOR UPDATE) — overkill для
  анти-абуз-капа; не делаем. Документируем как принятый компромисс.
- Helper падает (БД) → пробрасываем; charge не происходит (fail-safe:
  лучше не продать, чем продать сверх капа). try/except в flow покажет
  generic-ошибку.

## Тестирование

Юнит (`tests/services/test_purchased_traffic_cap.py`), мок db:
- cap=0 → can_add allow всегда (-1 headroom).
- cap=100, active=0, gb=50 → allow, remaining 100.
- cap=100, active=80, gb=50 → reject, remaining 20.
- cap=100, active=80, gb=20 → allow (ровно по границе).
- cap=100, active=100 → reject любой gb>0, remaining 0.
- get_active_purchased_traffic_gb суммирует только неистёкшие (мок execute
  scalar).
- get_max_purchased_traffic_gb клампит мусор → 0.

Bot-flow тест опц. (точка вызова) — если есть харнес; иначе юнит на helper
достаточно.

## Rollback

- За `MAX_PURCHASED_TRAFFIC_GB` (env, дефолт 0 = выкл). В проде ничего не
  меняется пока админ не выставит >0.
- Изменения: config + helper в crud + pre-check в 1-3 юзер-точках. Миграции
  НЕТ.
- `git revert`.

## Open questions

Нет. Поведение = reject (подтверждено). Cap = env, дефолт 0. A не входит
(уже есть). WL-кап — follow-up.
