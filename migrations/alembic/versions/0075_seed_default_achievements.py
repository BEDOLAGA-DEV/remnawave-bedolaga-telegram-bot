"""seed default achievement templates

Inserts a starter pack of achievements only if the table is empty,
so existing customizations are preserved.

Revision ID: 0075
Revises: 0074
Create Date: 2026-04-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0075'
down_revision: Union[str, None] = '0074'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Reward types: balance_kopeks (in kopeks), traffic_gb, subscription_days, none
# Condition types: total_spent_kopeks, days_active, referral_count,
#                  traffic_gb, topup_count, review_left
#
# Multi-level chains share group_name; level 2+ unlocks only after the previous level.
DEFAULT_ACHIEVEMENTS = [
    # ── Loyalty: time with us ────────────────────────────────────────
    {
        'name': 'Новичок',
        'description': 'Зарегистрировался и сделал первый шаг',
        'emoji': '🌱',
        'condition_type': 'days_active', 'condition_value': 1,
        'reward_type': 'balance_kopeks', 'reward_value': 5000,
        'hint': 'Просто зарегистрируйтесь',
        'group_name': 'loyalty', 'level': 1, 'display_order': 10,
    },
    {
        'name': 'Постоянный клиент',
        'description': '30 дней с нами',
        'emoji': '📅',
        'condition_type': 'days_active', 'condition_value': 30,
        'reward_type': 'balance_kopeks', 'reward_value': 10000,
        'hint': 'Оставайтесь с нами 30 дней',
        'group_name': 'loyalty', 'level': 2, 'display_order': 11,
    },
    {
        'name': 'Ветеран',
        'description': '180 дней с нами',
        'emoji': '🎖️',
        'condition_type': 'days_active', 'condition_value': 180,
        'reward_type': 'balance_kopeks', 'reward_value': 30000,
        'hint': 'Используйте сервис полгода',
        'group_name': 'loyalty', 'level': 3, 'display_order': 12,
    },
    {
        'name': 'Старожил',
        'description': 'Год вместе!',
        'emoji': '👑',
        'condition_type': 'days_active', 'condition_value': 365,
        'reward_type': 'balance_kopeks', 'reward_value': 100000,
        'hint': 'Используйте сервис 365 дней',
        'group_name': 'loyalty', 'level': 4, 'display_order': 13,
    },

    # ── Spending tier ────────────────────────────────────────────────
    {
        'name': 'Первая покупка',
        'description': 'Первое пополнение баланса',
        'emoji': '💳',
        'condition_type': 'topup_count', 'condition_value': 1,
        'reward_type': 'balance_kopeks', 'reward_value': 3000,
        'hint': 'Сделайте первое пополнение',
        'group_name': 'spender', 'level': 1, 'display_order': 20,
    },
    {
        'name': 'Активный пользователь',
        'description': '5 пополнений баланса',
        'emoji': '💼',
        'condition_type': 'topup_count', 'condition_value': 5,
        'reward_type': 'balance_kopeks', 'reward_value': 10000,
        'hint': 'Пополните баланс 5 раз',
        'group_name': 'spender', 'level': 2, 'display_order': 21,
    },
    {
        'name': 'Премиум клиент',
        'description': '20 пополнений баланса',
        'emoji': '💎',
        'condition_type': 'topup_count', 'condition_value': 20,
        'reward_type': 'balance_kopeks', 'reward_value': 50000,
        'hint': 'Пополните баланс 20 раз',
        'group_name': 'spender', 'level': 3, 'display_order': 22,
    },

    # ── Total spent ──────────────────────────────────────────────────
    {
        'name': 'Бронзовый статус',
        'description': 'Потратил 1 000 ₽',
        'emoji': '🥉',
        'condition_type': 'total_spent_kopeks', 'condition_value': 100000,
        'reward_type': 'balance_kopeks', 'reward_value': 5000,
        'hint': 'Потратьте 1000 ₽ суммарно',
        'group_name': 'tier', 'level': 1, 'display_order': 30,
    },
    {
        'name': 'Серебряный статус',
        'description': 'Потратил 5 000 ₽',
        'emoji': '🥈',
        'condition_type': 'total_spent_kopeks', 'condition_value': 500000,
        'reward_type': 'balance_kopeks', 'reward_value': 25000,
        'hint': 'Потратьте 5000 ₽ суммарно',
        'group_name': 'tier', 'level': 2, 'display_order': 31,
    },
    {
        'name': 'Золотой статус',
        'description': 'Потратил 15 000 ₽',
        'emoji': '🥇',
        'condition_type': 'total_spent_kopeks', 'condition_value': 1500000,
        'reward_type': 'balance_kopeks', 'reward_value': 100000,
        'hint': 'Потратьте 15 000 ₽ суммарно',
        'group_name': 'tier', 'level': 3, 'display_order': 32,
    },

    # ── Referrals ────────────────────────────────────────────────────
    {
        'name': 'Первый друг',
        'description': 'Пригласил 1 пользователя',
        'emoji': '🤝',
        'condition_type': 'referral_count', 'condition_value': 1,
        'reward_type': 'balance_kopeks', 'reward_value': 10000,
        'hint': 'Пригласите 1 друга по реферальной ссылке',
        'group_name': 'referrer', 'level': 1, 'display_order': 40,
    },
    {
        'name': 'Душа компании',
        'description': 'Пригласил 5 пользователей',
        'emoji': '🎉',
        'condition_type': 'referral_count', 'condition_value': 5,
        'reward_type': 'balance_kopeks', 'reward_value': 50000,
        'hint': 'Пригласите 5 друзей',
        'group_name': 'referrer', 'level': 2, 'display_order': 41,
    },
    {
        'name': 'Амбассадор',
        'description': 'Пригласил 25 пользователей',
        'emoji': '📣',
        'condition_type': 'referral_count', 'condition_value': 25,
        'reward_type': 'balance_kopeks', 'reward_value': 200000,
        'hint': 'Пригласите 25 друзей',
        'group_name': 'referrer', 'level': 3, 'display_order': 42,
    },

    # ── Traffic ──────────────────────────────────────────────────────
    {
        'name': 'Первый гигабайт',
        'description': 'Использовал 1 ГБ трафика',
        'emoji': '📡',
        'condition_type': 'traffic_gb', 'condition_value': 1,
        'reward_type': 'none', 'reward_value': 0,
        'hint': 'Используйте VPN на 1 ГБ',
        'group_name': 'traffic', 'level': 1, 'display_order': 50,
    },
    {
        'name': 'Любитель сериалов',
        'description': 'Использовал 100 ГБ трафика',
        'emoji': '🎬',
        'condition_type': 'traffic_gb', 'condition_value': 100,
        'reward_type': 'traffic_gb', 'reward_value': 10,
        'reward_duration_days': 30,
        'hint': 'Прокачайте 100 ГБ через VPN',
        'group_name': 'traffic', 'level': 2, 'display_order': 51,
    },
    {
        'name': 'Качаю всё',
        'description': 'Использовал 1 ТБ трафика',
        'emoji': '🚀',
        'condition_type': 'traffic_gb', 'condition_value': 1024,
        'reward_type': 'subscription_days', 'reward_value': 7,
        'hint': 'Прокачайте 1 ТБ через VPN',
        'group_name': 'traffic', 'level': 3, 'display_order': 52,
    },

    # ── Reviews / community ──────────────────────────────────────────
    {
        'name': 'Поделился мнением',
        'description': 'Оставил первый отзыв',
        'emoji': '✍️',
        'condition_type': 'review_left', 'condition_value': 1,
        'reward_type': 'balance_kopeks', 'reward_value': 5000,
        'hint': 'Оставьте отзыв через /review',
        'display_order': 60,
    },

    # ── Hidden / easter egg ──────────────────────────────────────────
    {
        'name': 'Терпение и труд',
        'description': 'Год активности и 50 пополнений',
        'emoji': '🦉',
        'condition_type': 'days_active', 'condition_value': 365,
        'reward_type': 'balance_kopeks', 'reward_value': 50000,
        'is_hidden': True,
        'display_order': 99,
    },
]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'achievement_templates' not in inspector.get_table_names():
        return

    # Skip seeding if any template already exists — admin may have customized.
    existing = conn.execute(sa.text('SELECT COUNT(*) FROM achievement_templates')).scalar() or 0
    if existing > 0:
        return

    table = sa.table(
        'achievement_templates',
        sa.column('name', sa.String),
        sa.column('description', sa.Text),
        sa.column('emoji', sa.String),
        sa.column('condition_type', sa.String),
        sa.column('condition_value', sa.Integer),
        sa.column('reward_type', sa.String),
        sa.column('reward_value', sa.Integer),
        sa.column('reward_duration_days', sa.Integer),
        sa.column('is_active', sa.Boolean),
        sa.column('is_hidden', sa.Boolean),
        sa.column('hint', sa.String),
        sa.column('group_name', sa.String),
        sa.column('level', sa.Integer),
        sa.column('display_order', sa.Integer),
    )

    rows = []
    for a in DEFAULT_ACHIEVEMENTS:
        rows.append({
            'name': a['name'],
            'description': a.get('description'),
            'emoji': a.get('emoji', '🏆'),
            'condition_type': a['condition_type'],
            'condition_value': a['condition_value'],
            'reward_type': a['reward_type'],
            'reward_value': a.get('reward_value', 0),
            'reward_duration_days': a.get('reward_duration_days'),
            'is_active': True,
            'is_hidden': a.get('is_hidden', False),
            'hint': a.get('hint'),
            'group_name': a.get('group_name'),
            'level': a.get('level', 1),
            'display_order': a.get('display_order', 0),
        })

    op.bulk_insert(table, rows)


def downgrade() -> None:
    # Best-effort removal: only delete templates that match seeded names AND have no user unlocks.
    conn = op.get_bind()
    names = tuple(a['name'] for a in DEFAULT_ACHIEVEMENTS)
    if not names:
        return
    conn.execute(
        sa.text(
            'DELETE FROM achievement_templates '
            'WHERE name IN :names '
            'AND id NOT IN (SELECT template_id FROM user_achievements)'
        ).bindparams(sa.bindparam('names', expanding=True)),
        {'names': list(names)},
    )
