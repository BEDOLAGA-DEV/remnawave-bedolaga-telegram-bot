# Proactive traffic upsell в Telegram — design

**Date:** 2026-05-30
**Scope:** `app/services/monitoring_service.py` (`_check_traffic_usage_warnings`)
**Status:** Draft
**Feature:** B2 (#2 в pipeline)

## Проблема

`_check_traffic_usage_warnings` шлёт предупреждение о трафике (80%/95%)
**только в web-каналы** кабинета: `_deliver_web_notification` (inbox + WS +
Web Push). Пользователь, который сидит в Telegram-боте и не открывает
кабинет, предупреждение не видит — и не докупает трафик до того, как он
кончится. Upsell-момент (трафик на исходе, готовность доплатить высокая)
теряется.

Дополнительно: web-путь **не уважает** пользовательскую настройку
`is_traffic_warning_enabled(user)` (она определена в
`app/utils/notification_prefs.py`, но в этом методе не проверяется).

## Решение

В `_check_traffic_usage_warnings`, для пользователей с `telegram_id`,
дополнительно к web-уведомлению слать **Telegram-сообщение** с прямыми
кнопками докупки/апгрейда. Реюз существующего порога-расчёта, dedup
(`check_recent_traffic_warning`, 7 дней) и сендера `_send_message_with_logo`.

Web-доставка остаётся как есть (для email-only и кабинет-юзеров). Telegram
добавляется как параллельный канал, под общим dedup-ключом (один и тот же
`highest_threshold` не шлётся повторно ни в одном канале в течение 7 дней).

### Компонент 1: TG-сообщение в `_check_traffic_usage_warnings`

После `_deliver_web_notification(...)` (внутри того же `try` на подписку),
если `user.telegram_id` и `self.bot`:

```python
if user.telegram_id and self.bot and is_traffic_warning_enabled(user):
    await self._send_traffic_upsell_notification(
        user, subscription, highest_threshold, used_gb, limit_gb,
    )
```

`is_traffic_warning_enabled` импортируется из `app.utils.notification_prefs`
(локальный импорт внутри метода, как принято в этом файле).

Dedup остаётся через `check_recent_traffic_warning` ДО отправки любого
канала — то есть TG и web шлются в одном проходе под одной dedup-записью.
Запись о факте уведомления уже делается текущим кодом (web-путь);
проверяем, что `_deliver_web_notification` либо сам пишет
`UserNotification`, либо запись делается рядом — TG-отправка НЕ должна
вводить вторую dedup-запись (иначе следующий цикл будет думать, что уже
слали, и пропустит web — но это и так один и тот же ключ). Telegram-
отправка происходит под тем же `highest_threshold`, без отдельного
`record`.

### Компонент 2: `_send_traffic_upsell_notification(...)`

Новый метод, мирроринг `_send_prerenew_save_notification` (структура,
обработка ошибок Telegram, `_handle_unreachable_user`):

```python
async def _send_traffic_upsell_notification(
    self, user, subscription, threshold, used_gb, limit_gb,
) -> bool:
    texts = get_texts(user.language)
    emoji = '🚨' if threshold >= 95 else '⚠️'
    template = texts.get('TRAFFIC_UPSELL_PUSH', (
        '{emoji} <b>Трафик заканчивается: {threshold}%</b>\n\n'
        'Использовано {used:.1f} / {limit} ГБ. '
        'Докупите пакет или поднимите тариф, чтобы не остаться без доступа.'
    ))
    message = template.format(emoji=emoji, threshold=threshold, used=used_gb, limit=limit_gb)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [build_miniapp_or_callback_button(text='➕ Докупить трафик', callback_data='nz!_buy_traffic')],
        [build_miniapp_or_callback_button(text=texts.t('SUBSCRIPTION_UPGRADE', '⬆️ Поднять тариф'), callback_data='nz!_menu_subscription')],
        [build_miniapp_or_callback_button(text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='nz!_balance_topup')],
    ])
    # try/except Telegram как в _send_prerenew_save_notification
```

Кнопка докупки → `nz!_buy_traffic` (зарегистрирован
`app/handlers/menu.py:1606` → `handle_add_traffic`). Кнопка тарифа →
`nz!_menu_subscription` (главное меню подписки; точный callback уточнить
при импле — взять тот, что реально открывает экран подписки/апгрейда).

### Компонент 3: respect pref на web-пути (мелкий фикс смежности)

Текущий web-путь шлёт независимо от `is_traffic_warning_enabled`. Чтобы
toggle реально работал, оборачиваем ВЕСЬ блок отправки (web + TG) проверкой
`is_traffic_warning_enabled(user)` — если выключено, пропускаем оба канала.
Это исправляет существующее несоответствие, не меняя дефолт (pref дефолт
True).

## Что НЕ входит

- Новые пороги/уровни (используем существующие `_parse_traffic_warning_thresholds`).
- Отдельный per-channel dedup (web и TG под общим ключом).
- Изменение текста web-уведомления.
- WL-трафик отдельной кнопкой (общий `nz!_buy_traffic` ведёт в существующий
  flow выбора трафика; WL-разветвление — вне scope).

## Архитектура

```
_check_traffic_usage_warnings (как есть)
  ├── thresholds = _parse_traffic_warning_thresholds()
  ├── для каждой active подписки с limit>0:
  │     ├── percent, highest_threshold
  │     ├── check_recent_traffic_warning (7d dedup) — как есть
  │     ├── if not is_traffic_warning_enabled(user): continue   ← НОВОЕ (фикс)
  │     ├── _deliver_web_notification(...)  — как есть
  │     └── if user.telegram_id and self.bot:                    ← НОВОЕ
  │           _send_traffic_upsell_notification(...)
  └── (запись dedup — существующая, общая на threshold)
```

## Поток данных

1. Цикл мониторинга вызывает `_check_traffic_usage_warnings`.
2. Для каждой подписки считается % трафика, берётся max пройденный порог.
3. Dedup-проверка (7 дней на пару user+sub+threshold).
4. Если pref выключен — пропуск (новый фикс).
5. Web-уведомление (как раньше) + если есть telegram_id — TG-сообщение с
   кнопками докупки/апгрейда/пополнения.
6. Dedup-запись (существующая) гарантирует один цикл уведомлений на порог.

## Обработка ошибок

- TG-send падает → `_handle_unreachable_user` + лог; web уже доставлен,
  не критично. TG-ошибка НЕ ломает web и не валит цикл.
- Метод-обёртка на подписку уже в `try/except` (есть) — изолирует сбой
  одной подписки.
- `is_traffic_warning_enabled` на None-pref → дефолт True (как в utils).

## Тестирование

Юнит-тесты (`tests/services/test_monitoring_traffic_upsell.py`), мок
`db`/`bot`/`_deliver_web_notification`/`check_recent_traffic_warning`/
`is_traffic_warning_enabled`:

- порог пройден (80%), есть telegram_id, pref on → web доставлен И
  `_send_traffic_upsell_notification` вызван.
- email-only (telegram_id=None) → web доставлен, TG НЕ вызван.
- pref off (`is_traffic_warning_enabled=False`) → ни web, ни TG.
- dedup hit (`check_recent_traffic_warning=True`) → ничего не шлётся.
- порог не пройден (percent<80) → ничего.
- TG-send падает → web всё равно доставлен, исключение не всплывает.
- кнопка докупки имеет callback_data `nz!_buy_traffic`.

## Rollback

- Изменения локализованы в `monitoring_service.py` + новый тест + опц.
  локаль-ключ `TRAFFIC_UPSELL_PUSH` (inline-дефолт).
- Web-поведение не меняется (кроме pref-gate, который и должен был
  действовать). `git revert <commit>` откатывает.
- Pref-gate можно считать bugfix; если нежелателен — вынести в отдельный
  коммит для раздельного отката.

## Open questions

- Точный callback апгрейда тарифа (`nz!_menu_subscription` vs spec'ный
  `se:`/`subscription_extend`) — уточнить при импле, взять реально
  работающий открыватель экрана подписки. Не блокирует дизайн.
