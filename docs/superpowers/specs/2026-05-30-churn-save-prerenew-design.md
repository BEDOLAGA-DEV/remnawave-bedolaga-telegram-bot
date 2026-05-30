# Churn-save: pre-expiry скидка-удержание — design

**Date:** 2026-05-30
**Scope:** `app/services/monitoring_service.py` + `NotificationSettingsService` + admin-настройки уведомлений
**Status:** Draft
**Feature:** #1 в pipeline (B5)

## Проблема

Сейчас скидочные офферы шлются только **после** истечения подписки —
win-back через `_check_expired_subscription_followups`: D+1 напоминание,
2-3 день (wave2, скидка), N день (wave3, скидка). Все три волны
post-expiry: пользователь уже потерял доступ.

Пробел: нет оффера **до** разрыва. Подписчик на грани отвала (подписка
истекает через сутки, автопродление не сработает — нет сохранённой карты)
не получает стимула продлить. Удержать активного дешевле, чем вернуть
ушедшего.

`_check_expiring_subscriptions` шлёт pre-expiry **предупреждения** (без
скидки) и уже вычисляет `has_saved_card` — то есть код знает, кто
авто-продлится, а кто нет. Но скидку-удержание не предлагает.

## Решение

Новый метод-сиблинг `_check_prerenew_save_offers(db)` в
`MonitoringService`, зеркалит паттерн `_check_expired_subscription_followups`,
но триггерится **до** expiry для at-risk сегмента: подписка истекает в окне
`trigger_hours`, и пользователь **не** авто-продлится (нет активной
автоплатёжной карты). Таким юзерам шлётся один discount-оффер «продли
сейчас -X%, не теряй доступ» через существующую `DiscountOffer` +
claim-механику.

Всё уже существует: источник подписок, dedup, `upsert_discount_offer`,
claim-flow, конфиг-сервис. Добавляем один метод + одно уведомление + набор
настроек. Низкие усилия.

### Компонент 1: `_check_prerenew_save_offers(db)`

Регистрируется в цикле мониторинга рядом с остальными чеками (после
`_check_expiring_subscriptions`, ~стр. 245-249).

Логика (зеркалит `_check_expired_subscription_followups`):

```
if not NotificationSettingsService.are_notifications_globally_enabled(): return
if not NotificationSettingsService.is_prerenew_save_enabled(): return
if not self.bot: return

trigger_hours = NotificationSettingsService.get_prerenew_save_trigger_hours()  # напр. 36
trigger_days = ceil(trigger_hours / 24)  # для запроса
subs = await self._get_expiring_paid_subscriptions(db, days_before=trigger_days)

# batch: какие user_id имеют активную автоплатёжную карту (как в _check_expiring_subscriptions)
users_with_cards = ... (get_user_ids_with_active_payment_methods)

for sub in subs:
    # окно: истекает в ближайшие trigger_hours (но ещё не истёк)
    hours_left = (sub.end_date - now).total_seconds() / 3600
    if not (0 < hours_left <= trigger_hours): continue

    # at-risk: НЕ авто-продлится
    will_autopay = sub.autopay_enabled and sub.user_id in users_with_cards
    if will_autopay: continue

    # multi-tariff: пропустить если есть другая активная подписка (как в followups)
    # daily-тарифы исключены (_get_expiring_paid_subscriptions уже фильтрует)
    # respect per-user notification prefs (is_subscription_expiry_enabled)

    if await notification_sent(db, user.id, sub.id, 'prerenew_save'): continue

    percent = NotificationSettingsService.get_prerenew_save_discount_percent()
    valid_hours = NotificationSettingsService.get_prerenew_save_valid_hours()
    offer = await upsert_discount_offer(
        db, user_id=user.id, subscription_id=sub.id,
        notification_type='prerenew_save',
        discount_percent=percent, bonus_amount_kopeks=0,
        valid_hours=valid_hours, effect_type='percent_discount',
    )
    if await self._send_prerenew_save_notification(user, sub, percent, offer.expires_at, offer.id):
        await record_notification(db, user.id, sub.id, 'prerenew_save')
```

Дедуп-ключ `notification_type='prerenew_save'` — отдельный от `expiring`
и от post-expiry волн. Один оффер на подписку за цикл жизни (до
следующего продления; `record_notification` гарантирует один раз).

### Компонент 2: `_send_prerenew_save_notification(...)`

Зеркалит `_send_expired_discount_notification`, но pre-expiry формулировка.
Текст-шаблон через `get_texts(user.language)` с ключом
`SUBSCRIPTION_PRERENEW_SAVE` (+ дефолт inline):

```
⏳ <b>Подписка истекает через {hours_left} ч</b>

Продлите сейчас со скидкой {percent}% — не теряйте доступ.
Скидка суммируется с промогруппой, действует до {expires_at}.
```

Клавиатура (как в `_send_expired_discount_notification`): «🎁 Получить
скидку» (`nz!_claim_discount_{offer_id}`), «💎 Продлить», «💳 Пополнить
баланс», «🆘 Поддержка». Та же обработка `TelegramForbidden/BadRequest/
Network` + `_handle_unreachable_user`.

### Компонент 3: настройки `NotificationSettingsService`

Новые геттеры, по образцу `second_wave`/`third_wave`:

- `is_prerenew_save_enabled() -> bool` (дефолт: выкл, чтобы не менять
  поведение прода без явного включения админом)
- `get_prerenew_save_trigger_hours() -> int` (дефолт 36)
- `get_prerenew_save_discount_percent() -> int` (дефолт 15)
- `get_prerenew_save_valid_hours() -> int` (дефолт 24)

Хранение — там же, где настройки волн (system settings / БД), + поля в
админ-UI редактирования уведомлений (тот же экран, где wave2/wave3).

## Что НЕ входит

- Расчёт «недостаточно баланса» как доп. критерий at-risk. В первой
  версии at-risk = «нет автоплатёжной карты». Баланс-критерий хрупкий
  (нужна точная цена продления с учётом промогрупп/тарифа) — отдельная
  итерация при необходимости.
- Изменение post-expiry волн и `_check_expiring_subscriptions` (не
  трогаем; churn-save — независимый канал).
- A/B вариаций текста/процента.
- Множественные pre-expiry волны (только один save-оффер).

## Архитектура

```
monitoring loop (~стр. 245)
  ├── _check_expiring_subscriptions      (warn, без скидки — как есть)
  ├── _check_traffic_usage_warnings       (как есть)
  ├── _check_prerenew_save_offers   ← НОВОЕ
  │     ├── _get_expiring_paid_subscriptions(trigger_days)
  │     ├── фильтр: 0 < hours_left <= trigger_hours
  │     ├── фильтр: will_autopay == False  (at-risk)
  │     ├── фильтр: prefs + multi-active + dedup('prerenew_save')
  │     ├── upsert_discount_offer(notification_type='prerenew_save')
  │     ├── _send_prerenew_save_notification(...)
  │     └── record_notification('prerenew_save')
  └── _check_expired_subscription_followups (win-back post-expiry — как есть)
```

## Поток данных

1. Cron-цикл мониторинга вызывает `_check_prerenew_save_offers`.
2. Метод тянет ACTIVE-подписки, истекающие в ближайшие `trigger_days`.
3. Отсекает тех, кто авто-продлится (autopay + карта), daily, multi-active,
   отключивших уведомления, уже получивших `prerenew_save`.
4. Для оставшихся at-risk: создаёт `DiscountOffer` (percent_discount,
   valid_hours) и шлёт TG-сообщение с кнопкой claim.
5. Пользователь жмёт «Получить скидку» → существующий claim-flow
   (`nz!_claim_discount_{id}`) применяет скидку к продлению.
6. `record_notification` исключает повторную отправку.

## Обработка ошибок

- Send падает (Telegram) → `_handle_unreachable_user` + не вызываем
  `record_notification` (повтор в следующем цикле, если ещё в окне).
- `upsert_discount_offer` идемпотентен по `(user, subscription,
  notification_type)` — повторный вызов до `record_notification` не плодит
  дубликаты.
- Любая ошибка в цикле обёрнута в try/except (как в соседних методах),
  логируется, не валит весь monitoring-проход.
- Если подписка успеет истечь между циклами — попадёт в post-expiry
  followups (другой канал), не теряется.

## Тестирование

Юнит-тесты (новый `tests/services/test_monitoring_prerenew_save.py`),
мок `db`/`bot`/`NotificationSettingsService`/`upsert_discount_offer`:

- at-risk (нет карты), в окне `trigger_hours` → оффер создан + send +
  `record_notification` вызваны.
- autopay + карта → пропущен (will_autopay), оффер НЕ создан.
- вне окна (истекает позже `trigger_hours`) → пропущен.
- уже отправлено (`notification_sent` → True) → пропущен.
- `is_prerenew_save_enabled() == False` → ранний выход, ничего.
- send падает → `record_notification` НЕ вызван.
- daily-тариф → не в выборке (фильтр `_get_expiring_paid_subscriptions`).
- multi-active (есть вторая активная подписка) → пропущен.

## Rollback

- Фича за флагом `is_prerenew_save_enabled` (дефолт OFF) — в проде ничего
  не меняется до явного включения.
- Изменения локализованы в `monitoring_service.py` +
  `NotificationSettingsService` + локали + admin-UI.
- `git revert <commit>` откатывает целиком.

## Open questions

Нет. Дизайн согласован в диалоге: churn-save = pre-expiry скидка для
at-risk, реюз DiscountOffer/wave-машинерии, отдельный метод + флаг.
