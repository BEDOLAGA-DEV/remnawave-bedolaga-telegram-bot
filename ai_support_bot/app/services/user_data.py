from datetime import datetime, timezone

import structlog
from sqlalchemy import text

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db.database import get_main_session
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.alerting import alert_admins
from ai_support_bot.app.services.remnawave import get_remnawave_stats


logger = structlog.get_logger(__name__)


def _format_price(kopeks: int | None) -> str:
    value = (kopeks or 0) / 100
    return f'{value:.2f} ₽'


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return None


def _days_left(end_date) -> int | None:
    end = _as_datetime(end_date)
    if end is None:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = end - datetime.now(timezone.utc)
    return int(delta.total_seconds() // 86400)


def _fmt_date(value) -> str:
    dt = _as_datetime(value)
    return dt.strftime('%d.%m.%Y') if dt else str(value)


async def build_user_context(telegram_id: int) -> str:
    lines: list[str] = [f'Telegram ID: {telegram_id}']

    session = await get_main_session()
    user_row = None
    if session is not None:
        try:
            result = await session.execute(
                text(
                    'SELECT id, username, first_name, last_name, language, balance_kopeks, '
                    'has_had_paid_subscription, has_made_first_topup, used_promocodes, '
                    'referral_code, referred_by_id, promo_group_id, created_at, '
                    'restriction_topup, restriction_subscription, restriction_reason, '
                    'remnawave_uuid '
                    'FROM users WHERE telegram_id = :tid LIMIT 1'
                ),
                {'tid': telegram_id},
            )
            user_row = result.mappings().first()

            if user_row:
                name = ' '.join(
                    filter(None, [user_row.get('first_name'), user_row.get('last_name')])
                ) or (user_row.get('username') or '')
                if name:
                    lines.append(f'Имя: {name}')
                if user_row.get('username'):
                    lines.append(f'Username: @{user_row["username"]}')
                lines.append(f'Язык: {user_row.get("language") or "ru"}')
                lines.append(f'Баланс: {_format_price(user_row.get("balance_kopeks"))}')
                lines.append(f'Регистрация: {_fmt_date(user_row.get("created_at"))}')

                status_flags = []
                if user_row.get('has_had_paid_subscription'):
                    status_flags.append('покупал платную подписку')
                else:
                    status_flags.append('платную подписку ещё не покупал')
                if not user_row.get('has_made_first_topup'):
                    status_flags.append('баланс ни разу не пополнял')
                lines.append('Статус клиента: ' + ', '.join(status_flags))

                if user_row.get('restriction_topup') or user_row.get('restriction_subscription'):
                    limits = []
                    if user_row.get('restriction_topup'):
                        limits.append('запрет пополнения')
                    if user_row.get('restriction_subscription'):
                        limits.append('запрет продления/покупки')
                    reason = user_row.get('restriction_reason')
                    reason_str = f' (причина: {reason})' if reason else ''
                    lines.append('Ограничения аккаунта: ' + ', '.join(limits) + reason_str)

                promo_group_id = user_row.get('promo_group_id')
                if promo_group_id:
                    try:
                        pg = await session.execute(
                            text('SELECT name FROM promo_groups WHERE id = :pid LIMIT 1'),
                            {'pid': promo_group_id},
                        )
                        pg_row = pg.mappings().first()
                        if pg_row and pg_row.get('name'):
                            lines.append(f'Промогруппа: {pg_row["name"]}')
                    except Exception:
                        pass

                if user_row.get('referral_code'):
                    try:
                        ref = await session.execute(
                            text('SELECT COUNT(*) AS c FROM users WHERE referred_by_id = :uid'),
                            {'uid': user_row['id']},
                        )
                        ref_count = int(ref.mappings().first().get('c') or 0)
                        lines.append(f'Рефералов приглашено: {ref_count}')
                    except Exception:
                        pass

                subs = await session.execute(
                    text(
                        'SELECT id, status, is_trial, end_date, traffic_limit_gb, traffic_used_gb, '
                        'device_limit, autopay_enabled, subscription_url '
                        'FROM subscriptions WHERE user_id = :uid ORDER BY created_at DESC LIMIT 5'
                    ),
                    {'uid': user_row['id']},
                )
                sub_rows = subs.mappings().all()
                if sub_rows:
                    if len(sub_rows) > 1:
                        lines.append(
                            f'Внимание: у пользователя {len(sub_rows)} подписки. Если вопрос про подписку '
                            'и не ясно, о какой речь — определи по последнему упоминанию в диалоге, '
                            'иначе задай ОДИН уточняющий вопрос.'
                        )
                    lines.append('Подписки:')
                    for index, sub in enumerate(sub_rows, start=1):
                        limit_gb = sub.get('traffic_limit_gb') or 0
                        used_gb = sub.get('traffic_used_gb') or 0
                        traffic = 'безлимит' if not limit_gb else f'{used_gb:.1f}/{limit_gb} ГБ'
                        end_str = _fmt_date(sub.get('end_date'))
                        left = _days_left(sub.get('end_date'))
                        left_str = ''
                        if left is not None:
                            left_str = f' (осталось {left} дн.)' if left >= 0 else f' (истекла {abs(left)} дн. назад)'
                        sub_url = sub.get('subscription_url')
                        url_str = f', ссылка={sub_url}' if sub_url else ''
                        lines.append(
                            f'  • подписка №{index} (id={sub.get("id")}), '
                            f'статус={sub.get("status")}, '
                            f'триал={"да" if sub.get("is_trial") else "нет"}, '
                            f'до {end_str}{left_str}, трафик={traffic}, '
                            f'устройств={sub.get("device_limit")}, '
                            f'автоплатеж={"вкл" if sub.get("autopay_enabled") else "выкл"}{url_str}'
                        )
                else:
                    lines.append('Подписки: отсутствуют')

                tx = await session.execute(
                    text(
                        'SELECT type, amount_kopeks, description, created_at '
                        'FROM transactions WHERE user_id = :uid ORDER BY created_at DESC LIMIT 5'
                    ),
                    {'uid': user_row['id']},
                )
                tx_rows = tx.mappings().all()
                if tx_rows:
                    lines.append('Последние операции:')
                    for row in tx_rows:
                        created_str = _fmt_date(row.get('created_at'))
                        description = (row.get('description') or '')[:60]
                        lines.append(
                            f'  • {created_str} {row.get("type")} '
                            f'{_format_price(row.get("amount_kopeks"))} — {description}'
                        )
            else:
                lines.append('Пользователь не найден в основной базе (возможно, ещё не запускал основного бота).')
        except Exception as error:
            lines.append(
                'ВНИМАНИЕ: данные пользователя из основной базы недоступны. '
                'Не утверждай ничего о подписках, балансе и платежах — уточни у пользователя '
                'или передай вопрос оператору.'
            )
            await alert_admins(
                'main_db_user_context',
                'не читаются данные пользователя из основной БД',
                f'{type(error).__name__}: {error}',
            )
        finally:
            await session.close()

    if settings_store.get_bool('INCLUDE_REMNAWAVE_DATA') and settings.remnawave_enabled:
        remna = await get_remnawave_stats(telegram_id, user_row.get('remnawave_uuid') if user_row else None)
        if remna:
            lines.append('Данные VPN-панели:')
            lines.append(f'  • трафик использован: {remna.get("used_traffic_gb", 0):.2f} ГБ')
            limit_gb = remna.get('traffic_limit_gb', 0)
            lines.append(f'  • лимит трафика: {"безлимит" if not limit_gb else f"{limit_gb:.0f} ГБ"}')

    return '\n'.join(lines)
