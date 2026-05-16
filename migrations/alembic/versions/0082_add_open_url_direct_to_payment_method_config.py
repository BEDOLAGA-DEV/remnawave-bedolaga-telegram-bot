"""add open_url_direct flag to payment_method_configs

Новый флаг для seamless-открытия страницы оплаты внутри Telegram MiniApp:
- False (default) — кабинет показывает панель "Открыть ссылку оплаты" (текущее поведение)
- True — кабинет делает window.location.href сразу после получения payment_url

Полезно для провайдеров типа YooKassa / Plateha — их checkout-страница нормально
рендерится внутри MiniApp WebView, и юзеру не нужно тапать дополнительную кнопку.
После оплаты return_url возвращает юзера на /balance/top-up/result.

По умолчанию False — backwards-compat. Админ включает per-провайдер в UI
платёжных методов. Для t.me/ URL (Stars, CryptoBot) флаг игнорируется — такие
ссылки всегда идут через openTelegramLink / openInvoice native handler.

Revision ID: 0082
Revises: 0081
Create Date: 2026-05-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0082'
down_revision: Union[str, None] = '0081'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'payment_method_configs',
        sa.Column(
            'open_url_direct',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('payment_method_configs', 'open_url_direct')
