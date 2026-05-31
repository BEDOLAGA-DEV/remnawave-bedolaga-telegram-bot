# Реферальные милстоуны — design

**Date:** 2026-05-31
**Scope:** накопительные награды за N оплативших рефералов (поверх существующей рефералки)
**Status:** Draft
**Feature:** #8 (последняя в pipeline). Дополнительный слой — НЕ замена флэт-бонуса (100₽) + 25% пожизненной комиссии.

## Проблема

Текущая рефералка платит per-head (100₽ инвайтеру + 100₽ за первый топ-ап +
25% пожизненно). Нет накопительной геймификации: «пригласи 5/10/25
оплативших → доп. награда». Прогресс-лесенка усиливает мотивацию звать
многих. Low-priority (флэт+25% уже сильны), делаем как опциональный слой.

## Решение

Admin-CMS лесенка `ReferralMilestone(threshold, reward_type, reward_value)`.
Метрика — **оплатившие рефералы** (DISTINCT referral_id с ≥1 ReferralEarning,
антифрод). При платеже реферала (хук в `process_referral_topup`) пересчитать
число оплативших у реферера и выдать все невыданные милстоуны с
`threshold <= count`. Идемпотентность через `UserReferralMilestoneClaim`.

Награды: **balance** (разовое начисление) или **promo_group** (перевод в
промогруппу = постоянная скидка). Оба механизма существуют:
`add_user_balance`, `add_user_to_promo_group`.

### Компонент 1: миграция + модели (0100)

```python
op.create_table('referral_milestones',
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('threshold', sa.Integer(), nullable=False),          # N оплативших рефералов
    sa.Column('reward_type', sa.String(20), nullable=False),       # 'balance' | 'promo_group'
    sa.Column('reward_value', sa.Integer(), nullable=False),       # kopeks (balance) | promo_group_id
    sa.Column('title', postgresql.JSONB(), nullable=False, server_default='{}'),  # multilingual
    sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint('threshold', name='uq_referral_milestone_threshold'),
)
op.create_table('user_referral_milestone_claims',
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
    sa.Column('milestone_id', sa.Integer(), sa.ForeignKey('referral_milestones.id', ondelete='CASCADE'), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint('user_id', 'milestone_id', name='uq_user_milestone_claim'),
)
```
Уникальность `(user_id, milestone_id)` = жёсткая идемпотентность (даже при
гонке двойной insert упадёт на constraint).

### Компонент 2: метрика — оплатившие рефералы

`crud/referral.py`:
```python
async def count_paid_referrals(db, referrer_id) -> int:
    """DISTINCT рефералы, у которых есть хотя бы одно начисление (= платили)."""
    result = await db.execute(
        select(func.count(func.distinct(ReferralEarning.referral_id)))
        .where(ReferralEarning.user_id == referrer_id)
        .where(ReferralEarning.referral_id.isnot(None))
        .where(ReferralEarning.amount_kopeks > 0)
    )
    return int(result.scalar() or 0)
```
(Считаем по ReferralEarning — реальные деньги, не регистрации. amount>0
исключает pending-маркеры типа `referral_registration_pending` с amount=0.)

### Компонент 3: `ReferralMilestoneService`

`reward_milestones(db, referrer_id, bot=None) -> list[granted]`:
1. Гейт `settings.REFERRAL_MILESTONES_ENABLED` → [].
2. `count = await count_paid_referrals(db, referrer_id)`.
3. Загрузить активные милстоуны `threshold <= count`, отсортированные.
4. Загрузить уже claimed `milestone_id` юзера (set).
5. Для каждого незаклейменного:
   - insert `UserReferralMilestoneClaim` (uniqueness защищает от гонки):
     try insert; IntegrityError → already claimed → rollback savepoint, skip.
   - выдать награду по reward_type:
     - `balance` → `add_user_balance(db, referrer, reward_value, description, commit=False)`.
     - `promo_group` → `add_user_to_promo_group(db, referrer_id, reward_value)`.
   - commit, нотиф «🎉 Милстоун N рефералов — награда …».
6. Вернуть список выданных.

Идемпотентность: claim-таблица + uniqueness. Повторный платёж того же
реферала не дважды засчитает (count по DISTINCT referral_id), и уже
выданные милстоуны в claimed-set.

### Компонент 4: точка вызова (хук)

`process_referral_topup` (`referral_service.py:316`) — после успешного
начисления комиссии реферреру вызвать
`await referral_milestone_service.reward_milestones(db, referrer.id, bot)`
в try/except (награда-милстоун не должна ломать основной реферал-флоу).

Момент платежа реферала → count оплативших мог вырасти → проверяем новые
милстоуны.

### Компонент 5: показ прогресса

- **Бот**: в реферальном меню (`handlers/referral.py`) — строка «Милстоуны:
  {count} оплативших · до следующей ({next_threshold}): N». Опц., минимально
  текст в существующем реферальном экране.
- **Кабинет**: `Referral.tsx` — прогресс + список (claimed/locked). Endpoint
  `GET /cabinet/referral/milestones` → `{count, milestones: [{threshold,
  title, reward_type, reward_value, claimed}]}`.
- Гейт `REFERRAL_MILESTONES_ENABLED`.

### Компонент 6: admin-CRUD

`admin_referral_milestones.py` (mirror admin_partner_promos): list/create/
update/delete/toggle. RBAC. React admin-UI — follow-up (v1 = бэк-API).

### Компонент 7: конфиг

`settings.REFERRAL_MILESTONES_ENABLED: bool = False` (env). Дефолт OFF.

## Что НЕ входит

- Замена флэт-бонуса/комиссии (милстоуны — поверх).
- Метрика по регистрациям (только оплатившие — антифрод).
- Backfill-скрипт за прошлых рефералов (при первом хуке после включения
  count уже отражает всех оплативших → выдаст заслуженные — это корректно).
- Снятие promo_group при «откате» (не отзываем награды).
- React admin-UI (v1 = бэк-CRUD).
- Reward-типы «дни подписки»/«устройства» (follow-up; v1 = balance/promo_group).

## Архитектура

```
referral pays → process_referral_topup (referral_service.py:316)
  └── referral_milestone_service.reward_milestones(db, referrer_id, bot)  [try/except]
        ├── gate REFERRAL_MILESTONES_ENABLED
        ├── count = count_paid_referrals(referrer)  (DISTINCT referral_id, earning>0)
        ├── active milestones threshold<=count, minus already-claimed
        ├── per milestone: insert claim (unique→idempotent) → grant (balance|promo_group) → commit → notify
        └── return granted[]
progress: bot referral menu + cabinet GET /cabinet/referral/milestones
admin: admin_referral_milestones CRUD (RBAC)
migration 0100: referral_milestones + user_referral_milestone_claims
config: REFERRAL_MILESTONES_ENABLED (env, default OFF)
```

## Поток данных

1. Реферал платит → process_referral_topup начисляет комиссию → хук.
2. count_paid_referrals(referrer) пересчитан.
3. Невыданные милстоуны ≤ count → insert claim (uniqueness) → награда → нотиф.
4. Прогресс виден в боте/кабинете.

## Обработка ошибок

- Хук падает → try/except в process_referral_topup, основной флоу цел.
- Гонка (два платежа разных рефералов реферера одновременно) → claim insert
  unique(user_id,milestone_id); дубль → IntegrityError → rollback savepoint,
  skip (награда один раз).
- promo_group reward с несуществующим id → лог + skip этого милстоуна (не
  валим остальные). Admin-CRUD валидирует при создании.
- balance reward через add_user_balance(commit=False) + наш commit —
  атомарно с claim (как birthday-фикс).

## Тестирование

Юнит (`tests/services/test_referral_milestone_service.py`):
- count_paid_referrals: DISTINCT, только earning>0 (мок).
- happy: count=5, милстоуны [1,3,5] активны, 0 claimed → 3 выданы, claims
  созданы, награды (balance/promo_group) вызваны, нотиф.
- идемпотентность: 1 уже claimed → не выдаётся повторно.
- частично: count=3, милстоуны [1,3,5] → выданы [1,3], не 5.
- gate OFF → [].
- balance → add_user_balance(commit=False); promo_group → add_user_to_promo_group.
- claim IntegrityError (гонка) → skip без падения.
- crud milestone: list_active, threshold-unique.

## Rollback

- За `REFERRAL_MILESTONES_ENABLED` (env, дефолт OFF).
- Миграция 0100 обратима (drop 2 таблицы).
- `git revert` + `alembic downgrade 0099`.

## Open questions

Решено: метрика = оплатившие (DISTINCT, антифрод); награды = balance |
promo_group (admin-лесенка); показ = бот + кабинет; дефолт OFF. React
admin-UI + reward-типы дни/устройства — follow-up.
