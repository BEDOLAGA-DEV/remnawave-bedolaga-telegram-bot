import structlog
from sqlalchemy import text

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db.database import get_main_session
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.remnawave import get_remnawave_stats


logger = structlog.get_logger(__name__)


def _format_price(kopeks: int | None) -> str:
    value = (kopeks or 0) / 100
    return f'{value:.2f} ₽'


async def build_user_context(telegram_id: int) -> str:
    lines: list[str] = [f'Telegram ID: {telegram_id}']

    session = await get_main_session()
    user_row = None
    if session is not None:
        try:
            result = await session.execute(
                text(
                    'SELECT id, username, first_name, last_name, language, balance_kopeks, '
                    'has_had_paid_subscription, remnawave_uuid '
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
                lines.append(f'Язык: {user_row.get("language") or "ru"}')
                lines.append(f'Баланс: {_format_price(user_row.get("balance_kopeks"))}')
                lines.append(
                    f'Была платная подписка: {"да" if user_row.get("has_had_paid_subscription") else "нет"}'
                )

                subs = await session.execute(
                    text(
                        'SELECT status, is_trial, end_date, traffic_limit_gb, device_limit, autopay_enabled, subscription_url '
                        'FROM subscriptions WHERE user_id = :uid ORDER BY created_at DESC LIMIT 5'
                    ),
                    {'uid': user_row['id']},
                )
                sub_rows = subs.mappings().all()
                if sub_rows:
                    lines.append('Подписки:')
                    for sub in sub_rows:
                        limit_gb = sub.get('traffic_limit_gb') or 0
                        traffic = 'безлимит' if not limit_gb else f'{limit_gb} ГБ'
                        end_date = sub.get('end_date')
                        end_str = end_date.strftime('%d.%m.%Y') if hasattr(end_date, 'strftime') else str(end_date)
                        sub_url = sub.get('subscription_url')
                        url_str = f', ссылка={sub_url}' if sub_url else ''
                        lines.append(
                            f'  • статус={sub.get("status")}, '
                            f'триал={"да" if sub.get("is_trial") else "нет"}, '
                            f'до {end_str}, трафик={traffic}, '
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
                        created = row.get('created_at')
                        created_str = created.strftime('%d.%m.%Y') if hasattr(created, 'strftime') else str(created)
                        description = (row.get('description') or '')[:60]
                        lines.append(
                            f'  • {created_str} {row.get("type")} '
                            f'{_format_price(row.get("amount_kopeks"))} — {description}'
                        )
            else:
                lines.append('Пользователь не найден в основной базе.')
        except Exception as error:
            logger.warning('Failed to read main DB user context', error=str(error))
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
