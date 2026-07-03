# BIO-reward free sub: убрать _wl и исправить баги жизненного цикла

**Дата:** 2026-07-03
**Статус:** утверждено пользователем (все 6 фиксов)
**Контекст деплоя:** single-mode (`MULTI_TARIFF_ENABLED=False`), `SALES_MODE=tariffs`, панельный username-шаблон `u_{telegram_id}`, платные подписки и триалы всегда имеют парный `_wl`-аккаунт. `_wl`-аккаунтов у BIO-участников на панели ещё нет (функция была только в тесте).

## Проблема

Бесплатная подписка за текст в BIO (`is_bio_reward=True`, создаётся в
`BioRewardService._create_free_sub`) хранит в БД `wl_traffic_limit_gb=NULL`
(конвенция «NULL = WL отключён»), но панельный синк это не учитывает, а
жизненный цикл BIO-подписки имеет пять дефектов, включая один критичный
(отключение оплаченной подписки при revoke).

## Фиксы

### Fix 1 — не создавать `_wl` на панели для bio-подписки (основной запрос)

`SubscriptionService._ensure_wl_user_synced` (app/services/subscription_service.py:846):

- Ранний выход, если `subscription.is_bio_reward` — до формирования
  `wl_kwargs` и любых create/update вызовов панели.
- Перед выходом: lookup `_wl` по производному имени
  (`_derive_wl_username`); если аккаунт существует (остатки тестов) —
  удалить его с панели (тем же API, что использует
  `_cleanup_wl_duplicates`), залогировать. 404 при lookup/delete — не
  ошибка.
- Платные и триальные подписки не затрагиваются: guard только по флагу
  `is_bio_reward`; текущая трактовка `wl_traffic_limit_gb=NULL → дефолт`
  для них сохраняется (в этом деплое у платных/триалов `_wl` обязателен).

Самолечение в `_extend_free_sub` (WL-clear + push) сохраняется — после
Fix 1 его push больше не пересоздаёт `_wl`.

### Fix 2 — конверсия bio→paid отвязывает free sub

`subscription_purchase_service.py` (~строка 1100): в блоке, где снимается
`is_bio_reward`, дополнительно найти `BioRewardParticipant` по
`user.id` (`bio_crud.get_participant_by_user_id`) и очистить
`participant.free_subscription_id = None`. Коммитится существующим
`db.commit()` этого потока. Участник остаётся `ACTIVE` (скидка
продолжает действовать); просто ссылка «моя бесплатная подписка»
перестаёт указывать на теперь-платную строку.

### Fix 3 — guard в `_extend_free_sub`

После загрузки sub по `participant.free_subscription_id`: если
`not sub.is_bio_reward` (строка была конвертирована в платную) —
очистить `participant.free_subscription_id`, закоммитить, выйти.
Не продлевать end_date, не форсить `status=ACTIVE`. Устраняет вечное
бесплатное продление коротких/суточных тарифов тиком планировщика.

### Fix 4 — guard + push в `_revoke`

- Отключать подписку по `free_subscription_id` только если
  `sub.is_bio_reward` всё ещё `True`; иначе очистить ссылку и не трогать.
- После установки `DISABLED`/`end_date=now` — пушить изменение в панель
  через `SubscriptionService.update_remnawave_user(db, sub,
  reset_traffic=False, sync_squads=False)` (best-effort, try/except с
  warning-логом). Без push юзер сохраняет VPN до панельного expire_at, а
  двунаправленный panel-sync может откатить локальный `end_date`
  (известное поведение этого деплоя).

### Fix 5 — push продления end_date в панель

`_extend_free_sub`: сейчас панель получает push только при WL-clear.
`end_date` продлевается только в БД → панельный `expire_at` остаётся
`created + window_days` (по умолчанию 3 дня) → активный BIO-участник
теряет VPN на 4-й день, а panel-sync может откатить БД.

Фикс: пушить `update_remnawave_user(db, sub, reset_traffic=False,
sync_squads=False)` когда изменился `end_date` ИЛИ был WL-clear (один
push на оба случая). Частота: 1 вызов на участника за тик
(`check_interval_minutes`, дефолт 60 мин) — приемлемо.

### Fix 6 — transient fetch-fail не запускает grace/revoke

`_fetch_bio` возвращает `None` при исключении (ошибка Telegram API,
сеть, flood-limit) и `''` при реально пустом bio. `check_user` сейчас
трактует оба как «текста нет» → transient-ошибка уводит ACTIVE в GRACE,
а 3 часа ошибок подряд → revoke со списанием с баланса.

Фикс в `check_user`: если `bio is None` и `not participant.bypass_check`
— обновить только `last_check_at`, не трогать `bio_snapshot` и статус,
закоммитить, вернуть outcome `'fetch_failed'`. Для `bypass_check`
поведение прежнее (матч принудительный, fetch не важен).

Обработчики (`app/handlers/bio_reward.py`), показывающие outcome
пользователю (кнопка «Проверить сейчас» / opt-in), получают текст для
`fetch_failed`: «Не удалось проверить профиль, попробуйте позже» —
без смены состояния.

## Не входит в объём (предложения на будущее)

- Авто-создание free sub, когда платная подписка ACTIVE-участника истекла
  (сейчас участник со скидкой остаётся без подписки до ручного повторного
  opt-in).
- Напоминание за N часов до конца grace.
- Rate-limit кнопки «Проверить сейчас» (анти-спам get_chat).
- Алерт/метрика на массовые fetch-fail (деградация Telegram API).

## Тестирование

TDD, расширение `tests/test_bio_reward.py` (venv:
`.venv\Scripts\python.exe -m pytest tests/test_bio_reward.py`):

1. bio-подписка: `_ensure_wl_user_synced` не создаёт `_wl`; существующий
   `_wl` удаляется; платная подписка — поведение без изменений.
2. Конверсия bio→paid очищает `free_subscription_id`.
3. `_extend_free_sub` с конвертированной (не-bio) sub: ссылка очищена,
   end_date/status не тронуты.
4. `_revoke` с конвертированной sub: платная не отключается; с bio-sub:
   отключается и пушится в панель (mock).
5. `_extend_free_sub` при изменении end_date пушит в панель (mock).
6. `check_user` при `bio=None`: outcome `fetch_failed`, статус не
   изменился, `bio_snapshot` сохранён; при `bio=''`: прежнее поведение
   (grace для ACTIVE).
