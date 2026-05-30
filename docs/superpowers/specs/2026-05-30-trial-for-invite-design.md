# Триал-за-инвайт (trial extension for invites) — design

**Date:** 2026-05-30
**Scope:** продление триала инвайтера, когда приглашённый активировал свой триал
**Status:** Draft
**Feature:** #5 в pipeline

## Проблема

Бесплатный верх воронки слабый: реферал-бонусы (кэш/комиссия) платятся
только когда приглашённый **пополняет баланс**. Юзер на триале (ещё не
платил) не имеет стимула звать друзей прямо сейчас — его награда отложена
до денежной конверсии приглашённого.

Хотим: когда приглашённый **активирует триал**, инвайтер (если он сам ещё
на триале) получает +N дней к своему триалу. Дешёвый (бесплатный для нас —
серверное время) вирусный стимул на самом верху воронки.

## Решение

Хук в момент активации триала приглашённым: если у него есть
`referred_by_id` и инвайтер сам сейчас на **активном триале** — продлить
триал инвайтера на `trial_invite_extend_days`, под годовым/суммарным капом
`trial_invite_max_extension_days`. Идемпотентность по уникальному
приглашённому (один приглашённый = одно продление).

Атрибуция реферала уже есть (`User.referred_by_id`,
`process_referral_registration`). Триал создаётся через единый chokepoint
`create_trial_subscription` (crud/subscription.py:141); пользовательские
точки активации — бот `app/handlers/subscription/purchase.py:977` и кабинет
`app/cabinet/routes/subscription_modules/purchase.py:1367`.

### Компонент 1: миграция + поля User (0098)

```python
op.add_column('users', sa.Column('trial_invite_bonus_days_used', sa.Integer(), nullable=False, server_default='0'))
op.add_column('users', sa.Column('trial_invite_rewarded_count', sa.Integer(), nullable=False, server_default='0'))
```

`Subscription` уже имеет `end_date`, `is_trial`, `status`. Идемпотентность
«один приглашённый = одно продление» обеспечивается уникальностью
приглашённого: приглашённый активирует триал ровно один раз (триал
создаётся один раз на юзера), и хук срабатывает в момент этой активации —
повторно для того же приглашённого не вызовется. Доп. поля-маркера на
приглашённом не требуется; на инвайтере храним только счётчики (для капа
и аналитики).

### Компонент 2: `TrialInviteService`

`app/services/trial_invite_service.py`.

`reward_inviter_on_trial_activation(db, invitee, bot=None) -> None`:
1. Гейт: `settings.TRIAL_INVITE_ENABLED` False → return.
2. `referrer_id = invitee.referred_by_id`; None → return.
3. self-invite (`referrer_id == invitee.id`) → return.
4. Загрузить `referrer`. Нет → return.
5. Инвайтер должен быть сам на активном триале: найти его подписку
   `is_trial == True AND status == ACTIVE AND end_date > now`. Нет →
   return (после конверсии в платную работает обычная реферал-механика —
   не наша забота).
6. Кап: `extend = settings.get_trial_invite_extend_days()`;
   `remaining = max(0, max_extension - referrer.trial_invite_bonus_days_used)`;
   `grant = min(extend, remaining)`; если `grant <= 0` → return (кап
   исчерпан) — опц. нотиф «достигнут лимит».
7. Продлить триал инвайтера: `sub.end_date += timedelta(days=grant)` (под
   row-lock `SELECT ... FOR UPDATE` на подписке инвайтера — против гонки
   нескольких приглашённых одновременно).
8. `referrer.trial_invite_bonus_days_used += grant`;
   `referrer.trial_invite_rewarded_count += 1`.
9. Синк панели: продлить срок на RemnaWave (реюз существующего пути
   обновления подписки — `SubscriptionService` update/renew по uuid;
   если прямого «extend end_date на панели» нет, использовать тот же
   механизм, что и обычное продление триала). Ошибка панели → не
   откатываем БД (время начислено), лог + retry-queue.
10. commit, нотиф инвайтеру «+{grant} дн. триала за приглашённого друга».

`TRIAL_INVITE_ENABLED` дефолт False (env). Конфиг-числа — в `settings`
(env) или admin JSON — см. компонент 4.

### Компонент 3: точки вызова хука

После успешного `create_trial_subscription` для приглашённого:
- бот: `app/handlers/subscription/purchase.py` (~стр. 977, основная
  активация триала). После создания подписки + провижна на панели вызвать
  `await trial_invite_service.reward_inviter_on_trial_activation(db, db_user, bot)`.
- кабинет: `app/cabinet/routes/subscription_modules/purchase.py` (~стр.
  1367). Аналогично (bot=None или общий bot-инстанс для нотифа).

Вызов в try/except — награда инвайтеру не должна ломать активацию триала
приглашённого.

«Активировал триал» = `create_trial_subscription` отработал успешно (для
channel-gate сборок провижн на панели — часть этого пути). Доп. проверка
channel-gate не требуется: триал не создастся, пока gate не пройден.

### Компонент 4: конфиг + admin

- `settings.TRIAL_INVITE_ENABLED: bool = False` (env мастер-флаг).
- `settings.TRIAL_INVITE_EXTEND_DAYS: int = 3` + геттер с клампом.
- `settings.TRIAL_INVITE_MAX_EXTENSION_DAYS: int = 14` + геттер с клампом.

(Конфиг через `settings`/env достаточно — отдельная admin-JSON-панель
опциональна; если нужна runtime-правка без рестарта, добавить
`TrialInviteSettingsService` по образцу прочих. В первой версии — env.)

## Что НЕ входит

- Награда за инвайт платным юзерам (только триал-инвайтер).
- Награда на момент регистрации (только реальная активация триала).
- Ретроактив за прошлые инвайты.
- Замена обычной реферал-механики (кэш/комиссия остаются как есть; это
  дополнительный слой только для триал-фазы инвайтера).
- Отдельная admin-JSON-панель (env-конфиг в первой версии).

## Архитектура

```
invitee activates trial (bot purchase.py:977 / cabinet purchase.py:1367)
  └── create_trial_subscription(...) succeeds
        └── trial_invite_service.reward_inviter_on_trial_activation(db, invitee, bot)  [try/except]
              ├── gate TRIAL_INVITE_ENABLED
              ├── referrer = invitee.referred_by_id  (skip if none/self)
              ├── referrer's own ACTIVE trial sub?  (skip if not)
              ├── grant = min(extend_days, max_extension - used)   (skip if <=0)
              ├── SELECT referrer trial sub FOR UPDATE; end_date += grant
              ├── referrer.trial_invite_bonus_days_used += grant; rewarded_count += 1
              ├── panel extend (retry-queue on failure)
              └── commit + notify inviter
migration 0098: users += trial_invite_bonus_days_used, trial_invite_rewarded_count
config: TRIAL_INVITE_ENABLED / TRIAL_INVITE_EXTEND_DAYS / TRIAL_INVITE_MAX_EXTENSION_DAYS
```

## Поток данных

1. Приглашённый жмёт «активировать триал» → проходит channel-gate →
   `create_trial_subscription` создаёт его триал + провижн на панели.
2. Хук берёт `invitee.referred_by_id`, проверяет что инвайтер сам на
   активном триале.
3. Считает grant под капом, продлевает триал инвайтера (row-lock),
   инкрементит счётчики.
4. Синкает срок на панель, шлёт инвайтеру нотиф.
5. Кап `trial_invite_bonus_days_used` ограничивает суммарное продление.

## Обработка ошибок

- Хук падает → ловим в try/except на стороне активации триала
  приглашённого; его триал НЕ ломается.
- Инвайтер не на триале (уже платный/истёк) → тихий skip (не ошибка).
- Гонка: два приглашённых активируют триал одновременно → row-lock
  `FOR UPDATE` на подписке инвайтера сериализует; счётчик и end_date
  консистентны.
- Панель-ошибка при продлении → БД-время начислено (не откат), enqueue
  retry-queue + лог. Инвайтер получит срок, панель досинкается.
- Кап исчерпан → grant=0 → skip (опц. одноразовый нотиф «лимит триал-
  бонусов достигнут»).

## Тестирование

Юнит (`tests/services/test_trial_invite_service.py`), мок db/settings/
subscription_service/bot:
- happy: приглашённый с referred_by_id, инвайтер на активном триале →
  end_date инвайтера += extend, счётчики += , нотиф.
- нет referred_by_id → no-op.
- self-invite → no-op.
- инвайтер НЕ на триале (платный/нет триала/истёк) → no-op.
- кап исчерпан (used >= max) → grant 0, end_date не меняется.
- кап частичный (remaining < extend) → grant = remaining.
- TRIAL_INVITE_ENABLED False → no-op.
- панель-ошибка → end_date всё равно продлён, retry-queue enqueued.
- row-lock: повторная загрузка подписки инвайтера FOR UPDATE (мок execute).

## Rollback

- За флагом `TRIAL_INVITE_ENABLED` (env, дефолт False).
- Миграция 0098 обратима (drop_column ×2).
- `git revert` + `alembic downgrade 0097`.

## Open questions

Нет. Триггер = реальная активация триала приглашённым (подтверждено).
Получатель = только триал-инвайтер (подтверждено). Конфиг — env в первой
версии.
