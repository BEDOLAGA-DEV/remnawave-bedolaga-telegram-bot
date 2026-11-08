"""Read personal support evidence using the authenticated Telegram identity only."""
from datetime import UTC, datetime
from html import escape
from urllib.parse import urlsplit

import structlog
from sqlalchemy import text

from ai_support_bot.app.db.database import get_main_session


logger = structlog.get_logger(__name__)


def _date(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def read_account_support(telegram_id: int, kind: str, email: str | None = None) -> dict:
    session = await get_main_session()
    if session is None:
        return {'status': 'unavailable'}
    try:
        user = (await session.execute(text(
            'SELECT id FROM users WHERE telegram_id = :tid '
            + ('AND lower(email) = lower(:email) ' if email else '') + 'LIMIT 1'
        ), {'tid': telegram_id, **({'email': email} if email else {})})).mappings().first()
        if not user:
            return {'status': 'unverified'}
        if kind == 'identity':
            rows = (await session.execute(text(
                'SELECT subscription_url FROM subscriptions WHERE user_id = :uid'
            ), {'uid': user['id']})).mappings().all()
            hosts = set()
            for row in rows:
                try:
                    parsed = urlsplit(row['subscription_url'] or '')
                    if parsed.scheme == 'https' and parsed.hostname and not parsed.username:
                        hosts.add(parsed.hostname.lower().rstrip('.'))
                except ValueError:
                    continue
            return {'status': 'ok', 'subscription_hosts': sorted(hosts)}
        if kind == 'wheel':
            rows = (await session.execute(text(
                'SELECT s.id AS spin_id, s.created_at AS won_at, s.is_applied, '
                'p.code, p.is_active, p.current_uses, p.max_uses, p.valid_from, p.valid_until '
                'FROM wheel_spins s LEFT JOIN promocodes p ON p.id = s.generated_promocode_id '
                "WHERE s.user_id = :uid AND s.prize_type = 'promocode' "
                'ORDER BY s.created_at DESC, s.id DESC LIMIT 10'
            ), {'uid': user['id']})).mappings().all()
            rewards = []
            incomplete = False
            now = datetime.now(UTC)
            for row in rows:
                # The spin foreign key proves ownership; a name/prefix never does.
                if not row['is_applied'] or not row['code']:
                    incomplete = True
                    continue
                start, end = _date(row['valid_from']), _date(row['valid_until'])
                usable = bool(row['is_active']) and (row['current_uses'] or 0) < (row['max_uses'] or 0)
                usable = usable and start is not None and start <= now and (
                    row['valid_until'] is None or (end is not None and end >= now)
                )
                rewards.append({
                    'code': row['code'], 'won_at': str(row['won_at']),
                    'valid_until': str(row['valid_until']) if row['valid_until'] else None,
                    'usable': usable,
                })
            return {'status': 'ok', 'rewards': rewards, 'incomplete_rewards': incomplete}
        rows = (await session.execute(text(
            'SELECT id, status, end_date, is_trial FROM subscriptions '
            'WHERE user_id = :uid ORDER BY created_at DESC LIMIT 6'
        ), {'uid': user['id']})).mappings().all()
        active_paid = [row for row in rows if row['status'] == 'active' and not row['is_trial']
                       and (end := _date(row['end_date'])) and end > datetime.now(UTC)]
        return {'status': 'ok', 'active_paid_subscription': len(rows) == 1 and bool(active_paid),
                'multiple_subscriptions': len(rows) > 1}
    except Exception as error:
        logger.warning('Personal support evidence unavailable', kind=kind, error=type(error).__name__)
        return {'status': 'unavailable'}
    finally:
        await session.close()


def wheel_reward_answer(result: dict) -> tuple[str, bool]:
    rewards = result.get('rewards', [])
    if result.get('status') != 'ok' or not rewards:
        return ('Не удалось подтвердить доступный промокод из вашего выигрыша. '
                'Передаю обращение команде для проверки; присылать потерянный код или его скриншот не нужно.'), True
    lines = ['Ваши последние промокоды, выигранные в колесе:']
    for reward in rewards:
        status = 'доступен' if reward['usable'] else 'сейчас недоступен для активации'
        expiry = f", срок до {escape(reward['valid_until'])}" if reward['valid_until'] else ''
        lines.append(f"• <code>{escape(reward['code'])}</code> — выигран {escape(reward['won_at'])}; {status}{expiry}.")
    if result.get('incomplete_rewards') or not any(reward['usable'] for reward in rewards):
        lines.append('Передаю обращение команде для проверки. Не выпускаю и не заменяю промокоды самостоятельно.')
        return '\n'.join(lines), True
    lines.append('Для ввода кода откройте «Промокод» в боте или кабинете. Это ввод кода, а не история выигрышей.')
    return '\n'.join(lines), False
