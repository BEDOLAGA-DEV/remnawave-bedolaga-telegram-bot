# B7 — Trial onboarding nudge (design spec)

**Дата:** 2026-06-13
**Статус:** одобрено к реализации

## Проблема
Пользователь активирует триал, но не подключается (не ставит конфиг). Триал
«сгорает» впустую → потерянная конверсия. Нужно мягко напомнить и помочь.

## Решение
Если триал выдан, но юзер **не подключился**, бот пишет:
- через **3 часа** — nudge #1,
- через **12 часов** (если всё ещё не подключился) — nudge #2.
Каждый шлётся один раз. Сообщение: «почему ещё не подключился?» + инструкция +
кнопка «🔗 Подключиться». Фича **по умолчанию ВЫКЛЮЧЕНА** (как все B-фичи).

## Сигнал «не подключился» (комбо)
Юзер считается НЕ подключившимся, если ВЫПОЛНЕНЫ ОБА условия:
1. `lifetime_used_traffic_bytes <= TRIAL_ONBOARD_USED_BYTES_THRESHOLD` (порог,
   дефолт 1 МБ = 1_048_576 — отсекает служебный handshake-трафик), И
2. число активных hwid-устройств в панели == 0.

Источники (готовые методы):
- трафик: `RemnaWaveService.get_user_traffic_stats_by_uuid(remnawave_uuid)` ->
  `lifetime_used_traffic_bytes`.
- устройства: `api.get_user_devices(remnawave_uuid)` (app/external/remnawave_api.py)
  -> `{'total': N, 'devices': [...]}`; смотрим `total`.

Если хоть один сигнал говорит «подключился» (трафик > порога ИЛИ есть
устройство) → nudge НЕ шлём и помечаем оба ключа отправленными (чтобы при
последующих чеках не дёргать панель повторно для этой подписки).

remnawave_uuid берём из `Subscription.remnawave_uuid` (или
`subscription.user.remnawave_uuid` как fallback для single-tariff).

## Таймер
- Старт от `Subscription.created_at` (момент выдачи триала).
- nudge #1: `created_at` в окне `[now-12h, now-3h]` (прошло >=3ч, но <12ч) и не
  отправлен ключ `trial_onboard_3h`.
- nudge #2: `created_at <= now-12h` и не отправлен ключ `trial_onboard_12h`.
- Подписка должна быть ещё активна и триальна (истёкшую/отменённую не трогаем).

## Где исполняется
Новый метод `_check_trial_onboarding_nudge(db)` в
`app/services/monitoring_service.py`, вызывается из главного monitoring-loop
рядом с `_check_trial_expiring_soon` (строка ~288). Батч-выборка:
```
select(Subscription).join(user).options(selectinload tariff/user)
.where(status==ACTIVE, is_trial==True, end_date>now,
       created_at <= now - 3h, user.status==ACTIVE)
```
Для каждого кандидата:
1. определить окно (3h vs 12h) и нужный ключ; если ключ уже отправлен — skip;
2. проверить notify-флаги (см. ниже) — если выкл, skip;
3. дёрнуть панель (traffic + devices) — комбо-сигнал;
4. подключился -> record оба ключа (чтобы не дёргать панель снова), skip отправки;
5. не подключился -> отправить сообщение -> `record_notification(key)`.

Idempotency: `notification_sent / record_notification` (app/database/crud/notification.py)
с ключами `trial_onboard_3h`, `trial_onboard_12h` per (user_id, subscription_id).

## Сообщение
`_send_trial_onboarding_notification(user, subscription, stage)` по образцу
`_send_trial_ending_notification`:
- текст (редактируемый шаблон, см. админку): объясняет, что триал получен, но
  не активирован; короткая инструкция подключения; приглашает нажать кнопку.
- клавиатура: кнопка **🔗 Подключиться** -> `nz!_subscription_connect` (готовый
  connect-flow, `handle_connect_subscription`); опц. вторая кнопка «❓ Помощь/
  инструкция» -> существующий help (`show_device_connection_help`).
- доставка `_send_message_with_logo`, обработка `TelegramForbiddenError/
  BadRequest` через `_handle_unreachable_user` (как в trial_ending).

## Настройки (админка: Мониторинг -> Настройки мониторинга -> 🔔 Уведомления)
Новая секция `trial_onboard` в `NotificationSettingsService._DEFAULTS`:
```
'trial_onboard': {
    'enabled': False,
    'first_hours': 3,
    'second_hours': 12,
}
```
Геттеры/сеттеры по образцу `prerenew_save`:
`is_trial_onboard_enabled / set_…`, `get_trial_onboard_first_hours / set_…`,
`get_trial_onboard_second_hours / set_…` (клампы 1..168).
Гейтинг отправки: `are_notifications_globally_enabled()` AND
`is_trial_onboard_enabled()` AND user-pref (если для юзера уведомления
отключены — не шлём; использовать тот же механизм, что соседние чеки).

Env-флаг НЕ требуется — управление как у `prerenew_save` (через БД-настройку).
Порог трафика — настройка/константа `TRIAL_ONBOARD_USED_BYTES_THRESHOLD` в
config.py (дефолт 1_048_576), env-переопределяемая.

## Админ-UI (app/handlers/admin/monitoring.py)
В экран `admin_mon_notify_settings` добавить блок «Trial-онбординг»:
- toggle `admin_mon_notify_toggle_trial_onboard`,
- edit `admin_mon_notify_edit_trial_onboard_first` / `…_second` (часы),
- кнопка 🧪 тест-превью `admin_mon_notify_preview_trial_onboard`.
Зарегистрировать соответствующие `@router.callback_query` хендлеры.

## Что НЕ входит
- Миграции БД (используем существующий SentNotification-трекинг).
- Кабинет (фича чисто бот-сайд).
- Push/email-каналы (только Telegram-сообщение).
- Замер «частично подключился» — бинарно: трафик+устройства.

## Обработка ошибок / edge cases
- Панель недоступна (API упал) -> логируем, НЕ шлём, НЕ записываем ключ
  (повторим в следующем тике). Никогда не валим весь monitoring-loop (try/except
  на кандидата, как в trial_ending).
- remnawave_uuid пуст -> skip (нечего проверять).
- Юзер заблокировал бота -> `_handle_unreachable_user`, ключ записываем как
  «отправлено» (повторять некуда).
- Рестарт бота -> idempotent ключи в БД защищают от дублей.
- Юзер подключился между 3h и 12h -> на 12h-чеке сигнал «подключился» -> nudge #2
  не шлётся.

## Тестирование
- unit: окно-логика (3h/12h выбор ключа), комбо-сигнал (трафик>порог ИЛИ
  devices>0 => подключился), idempotency (повторный чек не шлёт).
- mock: panel traffic/devices, bot.send; проверить что при «подключился» оба
  ключа записаны и сообщение НЕ отправлено.
- settings: дефолты, клампы часов, toggle.

## Объём
~1 метод-чек + 1 метод-отправка в monitoring_service, ~6 геттеров/сеттеров в
notification_settings_service, ~4 admin-хендлера + кнопки, 1 константа config,
текст-шаблон. Без миграций. Доп. нагрузка на панель: 2 API-вызова (traffic+
devices) на свежий неактивный триал в окне — немного.

## Open questions
Нет. Сигнал = комбо (трафик<=порог И 0 устройств). Оба nudge (3h+12h, пока не
подключился). Сообщение = инструкция + кнопка «Подключиться». Default OFF,
idempotent, настройки в админ notify-разделе.
