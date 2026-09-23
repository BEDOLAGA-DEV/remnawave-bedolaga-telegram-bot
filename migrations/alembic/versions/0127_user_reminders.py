"""напоминания пользователям: таблицы и встроенное «привяжите второй способ входа»

Revision ID: 0127
Revises: 0126
Create Date: 2026-09-22

Напоминание создаёт админ в кабинете: условия (способ входа, подписка, дни с
регистрации, неактивность), каналы (бот / кабинет), частота и тексты по языкам.
Состояние по человеку — сколько раз отправлено в бот и закрыл ли карточку.
Встроенное напоминание поставляется ВЫКЛЮЧЕННЫМ.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0127'
down_revision: Union[str, None] = '0126'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BUILTIN_LINK_AUTH = {
    'name': 'Второй способ входа',
    'is_active': False,
    'builtin_key': 'link_auth_method',
    'channels': 'both',
    'category': 'service',
    'conditions': {'auth': 'single_method', 'registered_days_min': 3},
    'repeat_every_days': 14,
    'max_sends': 3,
    'button_kind': 'cabinet',
    'button_target': '/profile/accounts',
    'texts': {
        'ru': {
            'title': 'Добавьте второй способ входа',
            'body': (
                'Сейчас войти в аккаунт можно только одним способом. Привяжите в кабинете почту, '
                'Telegram или аккаунт соцсети — если один способ станет недоступен, вы не потеряете '
                'доступ к подписке.'
            ),
            'button': 'Привязать способ входа',
        },
        'en': {
            'title': 'Add a second sign-in method',
            'body': (
                'Right now you can sign in to your account in only one way. Link an email, Telegram '
                "or a social account in the cabinet — if one method becomes unavailable, you won't "
                'lose access to your subscription.'
            ),
            'button': 'Link a sign-in method',
        },
        'ua': {
            'title': 'Додайте другий спосіб входу',
            'body': (
                'Зараз увійти в акаунт можна лише одним способом. Привʼяжіть у кабінеті пошту, '
                'Telegram або акаунт соцмережі — якщо один спосіб стане недоступним, ви не втратите '
                'доступ до підписки.'
            ),
            'button': 'Привʼязати спосіб входу',
        },
        'zh': {
            'title': '添加第二种登录方式',
            'body': '目前您只能通过一种方式登录账户。请在个人中心绑定邮箱、Telegram 或社交账号——即使某种方式无法使用，您也不会失去订阅的访问权限。',
            'button': '绑定登录方式',
        },
        'fa': {
            'title': 'یک روش ورود دوم اضافه کنید',
            'body': (
                'در حال حاضر فقط با یک روش می‌توانید وارد حساب خود شوید. در پنل کاربری ایمیل، '
                'تلگرام یا یک حساب اجتماعی را متصل کنید — اگر یکی از روش‌ها در دسترس نباشد، '
                'دسترسی به اشتراک خود را از دست نمی‌دهید.'
            ),
            'button': 'اتصال روش ورود',
        },
    },
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if 'user_reminders' not in tables:
        op.create_table(
            'user_reminders',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(120), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('builtin_key', sa.String(64), nullable=True, unique=True),
            sa.Column('channels', sa.String(16), nullable=False),
            sa.Column('category', sa.String(16), nullable=False, server_default='service'),
            sa.Column('conditions', sa.JSON(), nullable=False),
            sa.Column('repeat_every_days', sa.Integer(), nullable=False, server_default='7'),
            sa.Column('max_sends', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('texts', sa.JSON(), nullable=False),
            sa.Column('button_kind', sa.String(16), nullable=False, server_default='none'),
            sa.Column('button_target', sa.String(500), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_user_reminders_id', 'user_reminders', ['id'])
    if 'user_reminder_states' not in tables:
        op.create_table(
            'user_reminder_states',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'reminder_id', sa.Integer(), sa.ForeignKey('user_reminders.id', ondelete='CASCADE'), nullable=False
            ),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('sends_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('reminder_id', 'user_id', name='uq_user_reminder_states_reminder_user'),
        )
        op.create_index('ix_user_reminder_states_id', 'user_reminder_states', ['id'])
        op.create_index('ix_user_reminder_states_user_id', 'user_reminder_states', ['user_id'])
        op.create_index(
            'ix_user_reminder_states_reminder_last_sent', 'user_reminder_states', ['reminder_id', 'last_sent_at']
        )

    bind = op.get_bind()
    exists = bind.execute(
        sa.text('SELECT 1 FROM user_reminders WHERE builtin_key = :key'), {'key': BUILTIN_LINK_AUTH['builtin_key']}
    ).first()
    if exists is None:
        # JSON-колонки объявлены с типом: иначе bulk_insert не сериализует dict.
        table = sa.table(
            'user_reminders',
            sa.column('name', sa.String()),
            sa.column('is_active', sa.Boolean()),
            sa.column('builtin_key', sa.String()),
            sa.column('channels', sa.String()),
            sa.column('category', sa.String()),
            sa.column('conditions', sa.JSON()),
            sa.column('repeat_every_days', sa.Integer()),
            sa.column('max_sends', sa.Integer()),
            sa.column('button_kind', sa.String()),
            sa.column('button_target', sa.String()),
            sa.column('texts', sa.JSON()),
        )
        op.bulk_insert(table, [BUILTIN_LINK_AUTH])


def downgrade() -> None:
    tables = _tables()
    if 'user_reminder_states' in tables:
        op.drop_table('user_reminder_states')
    if 'user_reminders' in tables:
        op.drop_table('user_reminders')
