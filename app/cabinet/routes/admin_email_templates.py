"""Admin routes for managing email notification templates."""

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission
from ..services.email_template_overrides import (
    COMMON_CONTEXT_VARS,
    build_common_context,
    delete_template_override,
    get_all_overrides,
    get_overrides_for_type,
    save_template_override,
    substitute_context_vars,
)
from ..services.email_templates import EmailNotificationTemplates


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/email-templates', tags=['Admin Email Templates'])


# ============ Template type metadata ============

TEMPLATE_TYPES = [
    {
        'type': 'balance_topup',
        'label': {'ru': 'Пополнение баланса', 'en': 'Balance Top-up', 'zh': '余额充值', 'ua': 'Поповнення балансу'},
        'description': {
            'ru': 'Уведомление о пополнении баланса',
            'en': 'Balance top-up notification',
            'zh': '余额充值通知',
            'ua': 'Сповіщення про поповнення балансу',
        },
        'context_vars': ['formatted_amount', 'formatted_balance', 'amount_rubles', 'new_balance_rubles'],
    },
    {
        'type': 'balance_change',
        'label': {'ru': 'Изменение баланса', 'en': 'Balance Change', 'zh': '余额变动', 'ua': 'Зміна балансу'},
        'description': {
            'ru': 'Уведомление об изменении баланса',
            'en': 'Balance change notification',
            'zh': '余额变动通知',
            'ua': 'Сповіщення про зміну балансу',
        },
        'context_vars': ['formatted_amount', 'formatted_balance', 'amount_rubles', 'new_balance_rubles'],
    },
    {
        'type': 'subscription_expiring',
        'label': {
            'ru': 'Подписка истекает',
            'en': 'Subscription Expiring',
            'zh': '订阅即将到期',
            'ua': 'Підписка закінчується',
        },
        'description': {
            'ru': 'Предупреждение об истечении подписки',
            'en': 'Subscription expiring warning',
            'zh': '订阅即将到期警告',
            'ua': 'Попередження про закінчення підписки',
        },
        'context_vars': ['days_left', 'expires_at'],
    },
    {
        'type': 'subscription_expired',
        'label': {
            'ru': 'Подписка истекла',
            'en': 'Subscription Expired',
            'zh': '订阅已到期',
            'ua': 'Підписка закінчилась',
        },
        'description': {
            'ru': 'Уведомление об истечении подписки',
            'en': 'Subscription expired notification',
            'zh': '订阅已到期通知',
            'ua': 'Сповіщення про закінчення підписки',
        },
        'context_vars': [],
    },
    {
        'type': 'subscription_renewed',
        'label': {
            'ru': 'Подписка продлена',
            'en': 'Subscription Renewed',
            'zh': '订阅已续期',
            'ua': 'Підписка продовжена',
        },
        'description': {
            'ru': 'Уведомление о продлении подписки',
            'en': 'Subscription renewed notification',
            'zh': '订阅已续期通知',
            'ua': 'Сповіщення про продовження підписки',
        },
        'context_vars': ['new_expires_at', 'tariff_name', 'traffic_limit_gb', 'device_limit'],
    },
    {
        'type': 'subscription_activated',
        'label': {
            'ru': 'Подписка активирована',
            'en': 'Subscription Activated',
            'zh': '订阅已激活',
            'ua': 'Підписка активована',
        },
        'description': {
            'ru': 'Уведомление об активации подписки',
            'en': 'Subscription activated notification',
            'zh': '订阅已激活通知',
            'ua': 'Сповіщення про активацію підписки',
        },
        'context_vars': ['expires_at', 'tariff_name', 'traffic_limit_gb', 'device_limit'],
    },
    {
        'type': 'autopay_success',
        'label': {
            'ru': 'Автоплатёж успешен',
            'en': 'Autopay Success',
            'zh': '自动续费成功',
            'ua': 'Автоплатіж успішний',
        },
        'description': {
            'ru': 'Уведомление об успешном автоплатеже',
            'en': 'Autopay success notification',
            'zh': '自动续费成功通知',
            'ua': 'Сповіщення про успішний автоплатіж',
        },
        'context_vars': ['formatted_amount', 'amount_rubles', 'new_expires_at'],
    },
    {
        'type': 'autopay_failed',
        'label': {
            'ru': 'Автоплатёж не удался',
            'en': 'Autopay Failed',
            'zh': '自动续费失败',
            'ua': 'Автоплатіж не вдався',
        },
        'description': {
            'ru': 'Уведомление о неудачном автоплатеже',
            'en': 'Autopay failed notification',
            'zh': '自动续费失败通知',
            'ua': 'Сповіщення про невдалий автоплатіж',
        },
        'context_vars': ['reason'],
    },
    {
        'type': 'autopay_insufficient_funds',
        'label': {
            'ru': 'Недостаточно средств (автоплатёж)',
            'en': 'Insufficient Funds (Autopay)',
            'zh': '余额不足（自动续费）',
            'ua': 'Недостатньо коштів (автоплатіж)',
        },
        'description': {
            'ru': 'Уведомление о нехватке средств для автоплатежа',
            'en': 'Insufficient funds for autopay notification',
            'zh': '自动续费余额不足通知',
            'ua': 'Сповіщення про нестачу коштів для автоплатежу',
        },
        'context_vars': ['required_amount', 'current_balance'],
    },
    {
        'type': 'daily_debit',
        'label': {'ru': 'Суточное списание', 'en': 'Daily Debit', 'zh': '每日扣费', 'ua': 'Добове списання'},
        'description': {
            'ru': 'Уведомление о суточном списании',
            'en': 'Daily debit notification',
            'zh': '每日扣费通知',
            'ua': 'Сповіщення про добове списання',
        },
        'context_vars': ['formatted_amount', 'formatted_balance', 'amount_rubles', 'new_balance_rubles'],
    },
    {
        'type': 'daily_insufficient_funds',
        'label': {
            'ru': 'Недостаточно средств (суточное)',
            'en': 'Insufficient Funds (Daily)',
            'zh': '余额不足（每日）',
            'ua': 'Недостатньо коштів (добове)',
        },
        'description': {
            'ru': 'Уведомление о нехватке средств для суточного списания',
            'en': 'Insufficient funds for daily debit',
            'zh': '每日扣费余额不足通知',
            'ua': 'Сповіщення про нестачу коштів для добового списання',
        },
        'context_vars': ['required_amount', 'current_balance'],
    },
    {
        'type': 'ban_notification',
        'label': {'ru': 'Блокировка аккаунта', 'en': 'Account Banned', 'zh': '账户被封禁', 'ua': 'Блокування акаунту'},
        'description': {
            'ru': 'Уведомление о блокировке аккаунта',
            'en': 'Account banned notification',
            'zh': '账户被封禁通知',
            'ua': 'Сповіщення про блокування акаунту',
        },
        'context_vars': ['reason'],
    },
    {
        'type': 'unban_notification',
        'label': {
            'ru': 'Разблокировка аккаунта',
            'en': 'Account Unbanned',
            'zh': '账户已解封',
            'ua': 'Розблокування акаунту',
        },
        'description': {
            'ru': 'Уведомление о разблокировке аккаунта',
            'en': 'Account unbanned notification',
            'zh': '账户已解封通知',
            'ua': 'Сповіщення про розблокування акаунту',
        },
        'context_vars': [],
    },
    {
        'type': 'warning_notification',
        'label': {'ru': 'Предупреждение', 'en': 'Warning', 'zh': '警告', 'ua': 'Попередження'},
        'description': {
            'ru': 'Предупреждение пользователю',
            'en': 'Warning notification',
            'zh': '警告通知',
            'ua': 'Попередження користувачу',
        },
        'context_vars': ['message'],
    },
    {
        'type': 'referral_bonus',
        'label': {'ru': 'Реферальный бонус', 'en': 'Referral Bonus', 'zh': '推荐奖励', 'ua': 'Реферальний бонус'},
        'description': {
            'ru': 'Уведомление о начислении реферального бонуса',
            'en': 'Referral bonus notification',
            'zh': '推荐奖励通知',
            'ua': 'Сповіщення про нарахування реферального бонусу',
        },
        'context_vars': ['formatted_bonus', 'bonus_rubles', 'referral_name'],
    },
    {
        'type': 'referral_registered',
        'label': {'ru': 'Новый реферал', 'en': 'New Referral', 'zh': '新推荐用户', 'ua': 'Новий реферал'},
        'description': {
            'ru': 'Уведомление о регистрации реферала',
            'en': 'New referral registered notification',
            'zh': '新推荐用户注册通知',
            'ua': 'Сповіщення про реєстрацію реферала',
        },
        'context_vars': ['referral_name'],
    },
    {
        'type': 'traffic_reset',
        'label': {'ru': 'Сброс трафика', 'en': 'Traffic Reset', 'zh': '流量重置', 'ua': 'Скидання трафіку'},
        'description': {
            'ru': 'Уведомление о сбросе трафика',
            'en': 'Traffic reset notification',
            'zh': '流量重置通知',
            'ua': 'Сповіщення про скидання трафіку',
        },
        'context_vars': ['reset_gb', 'current_limit_gb'],
    },
    {
        'type': 'payment_received',
        'label': {'ru': 'Платёж получен', 'en': 'Payment Received', 'zh': '收到付款', 'ua': 'Платіж отримано'},
        'description': {
            'ru': 'Уведомление о получении платежа',
            'en': 'Payment received notification',
            'zh': '收到付款通知',
            'ua': 'Сповіщення про отримання платежу',
        },
        'context_vars': ['formatted_amount', 'payment_method'],
    },
    {
        'type': 'email_verification',
        'label': {
            'ru': 'Подтверждение email',
            'en': 'Email Verification',
            'zh': '邮箱验证',
            'ua': 'Підтвердження email',
        },
        'description': {
            'ru': 'Письмо для подтверждения email адреса при регистрации',
            'en': 'Email address verification letter sent during registration',
            'zh': '注册时发送的邮箱验证邮件',
            'ua': 'Лист для підтвердження email адреси при реєстрації',
        },
        'context_vars': ['username', 'verification_url', 'expire_hours'],
    },
    {
        'type': 'password_reset',
        'label': {'ru': 'Сброс пароля', 'en': 'Password Reset', 'zh': '重置密码', 'ua': 'Скидання пароля'},
        'description': {
            'ru': 'Письмо для сброса пароля',
            'en': 'Password reset email',
            'zh': '密码重置邮件',
            'ua': 'Лист для скидання пароля',
        },
        'context_vars': ['username', 'reset_url', 'expire_hours'],
    },
    {
        'type': 'email_change_code',
        'label': {
            'ru': 'Код смены email',
            'en': 'Email Change Code',
            'zh': '邮箱更换验证码',
            'ua': 'Код зміни email',
        },
        'description': {
            'ru': 'Письмо с кодом подтверждения для смены email адреса',
            'en': 'Email with a confirmation code for changing the email address',
            'zh': '包含更换邮箱确认码的邮件',
            'ua': 'Лист з кодом підтвердження для зміни email адреси',
        },
        'context_vars': ['username', 'code', 'expire_minutes'],
    },
    {
        'type': 'partner_application_approved',
        'label': {
            'ru': 'Партнёрство одобрено',
            'en': 'Partner Application Approved',
            'zh': '合作伙伴申请已批准',
            'ua': 'Партнерство схвалено',
        },
        'description': {
            'ru': 'Уведомление об одобрении заявки на партнёрство',
            'en': 'Partner application approved notification',
            'zh': '合作伙伴申请获批通知',
            'ua': 'Сповіщення про схвалення заявки на партнерство',
        },
        'context_vars': ['commission_percent', 'comment'],
    },
    {
        'type': 'partner_application_rejected',
        'label': {
            'ru': 'Партнёрство отклонено',
            'en': 'Partner Application Rejected',
            'zh': '合作伙伴申请被拒绝',
            'ua': 'Партнерство відхилено',
        },
        'description': {
            'ru': 'Уведомление об отклонении заявки на партнёрство',
            'en': 'Partner application rejected notification',
            'zh': '合作伙伴申请被拒通知',
            'ua': 'Сповіщення про відхилення заявки на партнерство',
        },
        'context_vars': ['comment'],
    },
    {
        'type': 'withdrawal_approved',
        'label': {
            'ru': 'Вывод средств одобрен',
            'en': 'Withdrawal Approved',
            'zh': '提现已批准',
            'ua': 'Виведення коштів схвалено',
        },
        'description': {
            'ru': 'Уведомление об одобрении запроса на вывод средств',
            'en': 'Withdrawal request approved notification',
            'zh': '提现请求获批通知',
            'ua': 'Сповіщення про схвалення запиту на виведення коштів',
        },
        'context_vars': ['formatted_amount', 'amount_rubles', 'comment'],
    },
    {
        'type': 'withdrawal_rejected',
        'label': {
            'ru': 'Вывод средств отклонён',
            'en': 'Withdrawal Rejected',
            'zh': '提现被拒绝',
            'ua': 'Виведення коштів відхилено',
        },
        'description': {
            'ru': 'Уведомление об отклонении запроса на вывод средств',
            'en': 'Withdrawal request rejected notification',
            'zh': '提现请求被拒通知',
            'ua': 'Сповіщення про відхилення запиту на виведення коштів',
        },
        'context_vars': ['formatted_amount', 'amount_rubles', 'comment'],
    },
    {
        'type': 'guest_subscription_delivered',
        'label': {
            'ru': 'Быстрая покупка: подписка доставлена',
            'en': 'Quick Purchase: Subscription Delivered',
            'zh': '快捷购买：订阅已交付',
            'ua': 'Швидка покупка: підписка доставлена',
        },
        'description': {
            'ru': 'Письмо покупателю после успешной оплаты через лендинг',
            'en': 'Email to buyer after successful landing page payment',
            'zh': '通过落地页成功付款后发送给买家的邮件',
            'ua': 'Лист покупцю після успішної оплати через лендінг',
        },
        'context_vars': ['tariff_name', 'period_days', 'cabinet_url', 'cabinet_email', 'cabinet_password'],
    },
    {
        'type': 'guest_activation_required',
        'label': {
            'ru': 'Быстрая покупка: требуется активация',
            'en': 'Quick Purchase: Activation Required',
            'zh': '快捷购买：需要激活',
            'ua': 'Швидка покупка: потрібна активація',
        },
        'description': {
            'ru': 'Письмо когда у покупателя уже есть активная подписка',
            'en': 'Email when buyer already has an active subscription',
            'zh': '买家已有活跃订阅时发送的邮件',
            'ua': 'Лист коли у покупця вже є активна підписка',
        },
        'context_vars': ['tariff_name', 'period_days', 'success_page_url', 'gift_message', 'is_gift'],
    },
    {
        'type': 'guest_gift_received',
        'label': {
            'ru': 'Быстрая покупка: подарок получен',
            'en': 'Quick Purchase: Gift Received',
            'zh': '快捷购买：收到礼物',
            'ua': 'Швидка покупка: подарунок отримано',
        },
        'description': {
            'ru': 'Письмо получателю подарочной подписки',
            'en': 'Email to gift subscription recipient',
            'zh': '发送给礼物订阅接收者的邮件',
            'ua': 'Лист отримувачу подарункової підписки',
        },
        'context_vars': [
            'tariff_name',
            'period_days',
            'cabinet_url',
            'gift_message',
            'cabinet_email',
            'cabinet_password',
        ],
    },
    {
        'type': 'guest_cabinet_credentials',
        'label': {
            'ru': 'Быстрая покупка: данные для входа',
            'en': 'Quick Purchase: Login Credentials',
            'zh': '快捷购买：登录凭据',
            'ua': 'Швидка покупка: дані для входу',
        },
        'description': {
            'ru': 'Письмо с логином и паролем для личного кабинета',
            'en': 'Email with login credentials for the cabinet',
            'zh': '包含个人中心登录信息的邮件',
            'ua': 'Лист з логіном та паролем для особистого кабінету',
        },
        'context_vars': ['tariff_name', 'period_days', 'cabinet_url', 'cabinet_email', 'cabinet_password'],
    },
]

SAMPLE_CONTEXTS: dict[str, dict[str, Any]] = {
    'balance_topup': {
        'formatted_amount': '500.00 ₽',
        'formatted_balance': '1500.00 ₽',
        'amount_rubles': 500,
        'new_balance_rubles': 1500,
    },
    'balance_change': {
        'formatted_amount': '-200.00 ₽',
        'formatted_balance': '1300.00 ₽',
        'amount_rubles': -200,
        'new_balance_rubles': 1300,
    },
    # Sample dates rendered via format_email_datetime — preview shows
    # what the user actually receives, not raw ISO. If admin changes
    # EMAIL_DATE_FORMAT, these samples auto-adjust on next render.
    'subscription_expiring': {'days_left': 3, 'expires_at': '30.01.2026, 23:59'},
    'subscription_expired': {},
    'subscription_renewed': {
        'new_expires_at': '28.02.2026, 23:59',
        'tariff_name': 'Premium',
        'traffic_limit_gb': 100,
        'device_limit': 3,
    },
    'subscription_activated': {
        'expires_at': '28.02.2026, 23:59',
        'tariff_name': 'Premium',
        'traffic_limit_gb': 100,
        'device_limit': 3,
    },
    'autopay_success': {'formatted_amount': '300.00 ₽', 'amount_rubles': 300, 'new_expires_at': '28.02.2026, 23:59'},
    'autopay_failed': {'reason': 'Card declined'},
    'autopay_insufficient_funds': {'required_amount': '300.00 ₽', 'current_balance': '50.00 ₽'},
    'daily_debit': {
        'formatted_amount': '10.00 ₽',
        'formatted_balance': '490.00 ₽',
        'amount_rubles': 10,
        'new_balance_rubles': 490,
    },
    'daily_insufficient_funds': {'required_amount': '10.00 ₽', 'current_balance': '5.00 ₽'},
    'ban_notification': {'reason': 'Violation of terms of service'},
    'unban_notification': {},
    'warning_notification': {'message': 'Please review our terms of service'},
    'referral_bonus': {'formatted_bonus': '100.00 ₽', 'bonus_rubles': 100, 'referral_name': 'John'},
    'referral_registered': {'referral_name': 'John'},
    'traffic_reset': {'reset_gb': 50, 'current_limit_gb': 100},
    'payment_received': {'formatted_amount': '500.00 ₽', 'amount_rubles': 500, 'payment_method': 'YooKassa'},
    'email_verification': {
        'username': 'John',
        'verification_url': 'https://example.com/verify?token=abc123',
        'expire_hours': 24,
    },
    'password_reset': {'username': 'John', 'reset_url': 'https://example.com/reset?token=abc123', 'expire_hours': 1},
    'email_change_code': {'username': 'John', 'code': '123456', 'expire_minutes': 10},
    'partner_application_approved': {'commission_percent': 20, 'comment': 'Welcome aboard!'},
    'partner_application_rejected': {'comment': 'Not enough details provided'},
    'withdrawal_approved': {'formatted_amount': '1000.00 ₽', 'amount_rubles': 1000, 'comment': 'Processed'},
    'withdrawal_rejected': {'formatted_amount': '1000.00 ₽', 'amount_rubles': 1000, 'comment': 'Invalid requisites'},
    'guest_subscription_delivered': {
        'tariff_name': 'Premium',
        'period_days': 30,
        'cabinet_url': 'https://example.com/cabinet',
        'cabinet_email': 'user@example.com',
        'cabinet_password': 'SecurePass123',
    },
    'guest_activation_required': {
        'tariff_name': 'Premium',
        'period_days': 30,
        'success_page_url': 'https://example.com/cabinet/buy/success/abc123',
        'is_gift': True,
        'gift_message': 'Happy birthday!',
    },
    'guest_gift_received': {
        'tariff_name': 'Premium',
        'period_days': 30,
        'cabinet_url': 'https://example.com/cabinet',
        'gift_message': 'Happy birthday!',
        'cabinet_email': 'recipient@example.com',
        'cabinet_password': 'SecurePass123',
    },
    'guest_cabinet_credentials': {
        'tariff_name': 'Premium',
        'period_days': 30,
        'cabinet_url': 'https://example.com/cabinet',
        'cabinet_email': 'user@example.com',
        'cabinet_password': 'SecurePass123',
    },
}

AVAILABLE_LANGUAGES = ['ru', 'en', 'zh', 'ua', 'fa']

# Recipient-level common vars are empty until the sending code fills them —
# in preview/test we substitute samples so the admin sees realistic values.
COMMON_SAMPLE_CONTEXT = {'username': 'John', 'email': 'user@example.com'}


def _build_sample_context(notification_type: str) -> dict[str, Any]:
    """Common (real instance values) + recipient samples + per-type samples."""
    return {
        **build_common_context(),
        **COMMON_SAMPLE_CONTEXT,
        **SAMPLE_CONTEXTS.get(notification_type, {}),
    }


def _get_type_meta(notification_type: str) -> dict[str, Any] | None:
    return next((t for t in TEMPLATE_TYPES if t['type'] == notification_type), None)


def _validate_template_type(notification_type: str) -> dict[str, Any]:
    type_meta = _get_type_meta(notification_type)
    if type_meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Unknown template type: {notification_type}',
        )
    return type_meta


# Numeric twins of formatted_* vars: templates eagerly evaluate fallbacks like
# f'{context.get("amount_rubles", 0):.2f}', which raises on a string placeholder.
# Omitted from the placeholder context — defaults render via formatted_* anyway,
# and at send time the real numeric values substitute {amount_rubles} fine.
_NUMERIC_FALLBACK_VARS = {'amount_rubles', 'new_balance_rubles', 'bonus_rubles'}


def _placeholder_context(notification_type: str) -> dict[str, Any]:
    """Context that renders default templates with literal {var} placeholders.

    The editor must show editable templates with placeholders intact — if the
    admin saved a template rendered with sample values, production emails would
    contain those samples (e.g. an example.com verification link) instead of
    the real ones.
    """
    type_meta = _get_type_meta(notification_type)
    if not type_meta:
        return {}
    return {var: f'{{{var}}}' for var in type_meta['context_vars'] if var not in _NUMERIC_FALLBACK_VARS}


def _get_default_template(notification_type: str, language: str, context: dict[str, Any]) -> dict[str, str] | None:
    """Render the built-in default template, or None if unavailable."""
    from app.services.notification_delivery_service import NotificationType

    try:
        ntype_enum = NotificationType(notification_type)
    except ValueError:
        return None

    try:
        return EmailNotificationTemplates().get_template(ntype_enum, language, context)
    except Exception as e:
        logger.warning(
            'Не удалось отрендерить дефолтный email шаблон',
            notification_type=notification_type,
            language=language,
            e=e,
        )
        return None


# ============ Schemas ============


class EmailTemplateUpdate(BaseModel):
    """Request to update an email template."""

    subject: str = Field(..., min_length=1, max_length=500)
    body_html: str = Field(..., min_length=1)


class EmailTemplatePreviewRequest(BaseModel):
    """Request to preview an email template."""

    language: str = Field(default='ru')
    subject: str = Field(default='')
    body_html: str = Field(default='')


class EmailTemplateSendTestRequest(BaseModel):
    """Request to send a test email.

    When subject/body_html are provided, the current (possibly unsaved) editor
    content is sent; otherwise the saved override or the default template.
    """

    language: str = Field(default='ru')
    email: str = Field(default='')
    subject: str = Field(default='')
    body_html: str = Field(default='')


# ============ Endpoints ============


@router.get('', summary='List all email template types')
async def list_template_types(
    _admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """List all available email template types with override status."""
    overrides = await get_all_overrides(db)

    # Build a map of overrides by type
    override_map: dict[str, dict[str, bool]] = {}
    for o in overrides:
        ntype = o['notification_type']
        if ntype not in override_map:
            override_map[ntype] = {}
        override_map[ntype][o['language']] = o['is_active']

    result = []
    for tpl_type in TEMPLATE_TYPES:
        type_key = tpl_type['type']
        languages = {}
        for lang in AVAILABLE_LANGUAGES:
            languages[lang] = {
                'has_custom': lang in override_map.get(type_key, {}),
            }
        result.append(
            {
                **tpl_type,
                'languages': languages,
            }
        )

    return {
        'items': result,
        'available_languages': AVAILABLE_LANGUAGES,
        'common_context_vars': COMMON_CONTEXT_VARS,
    }


@router.get('/{notification_type}', summary='Get templates for a notification type')
async def get_templates_for_type(
    notification_type: str,
    _admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get all language templates for a specific notification type.

    Default templates are rendered with literal {var} placeholders (not sample
    values) so the admin can save them as overrides without baking in fake
    URLs, codes, or amounts.
    """
    type_meta = _validate_template_type(notification_type)

    # Get overrides from DB
    overrides = await get_overrides_for_type(notification_type, db)
    override_map = {o['language']: o for o in overrides}

    editor_context = _placeholder_context(notification_type)

    # Build combined result per language
    languages = {}
    for lang in AVAILABLE_LANGUAGES:
        default_template = _get_default_template(notification_type, lang, editor_context)

        default_subject = ''
        default_body_html = ''
        if default_template:
            default_subject = default_template.get('subject', '')
            default_body_html = default_template.get('body_html', '')

        # Check for override
        override = override_map.get(lang)
        if override:
            languages[lang] = {
                'subject': override['subject'],
                'body_html': override['body_html'],
                'is_default': False,
                'default_subject': default_subject,
                'default_body_html': default_body_html,
            }
        else:
            languages[lang] = {
                'subject': default_subject,
                'body_html': default_body_html,
                'is_default': True,
                'default_subject': default_subject,
                'default_body_html': default_body_html,
            }

    return {
        'notification_type': notification_type,
        'label': type_meta['label'],
        'description': type_meta['description'],
        'context_vars': type_meta['context_vars'],
        'common_context_vars': COMMON_CONTEXT_VARS,
        'languages': languages,
    }


@router.put('/{notification_type}/{language}', summary='Save custom template')
async def update_template(
    notification_type: str,
    language: str,
    data: EmailTemplateUpdate,
    admin: User = Depends(require_permission('email_templates:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Save a custom email template override."""
    _validate_template_type(notification_type)

    if language not in AVAILABLE_LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid language: {language}. Available: {AVAILABLE_LANGUAGES}',
        )

    result = await save_template_override(
        notification_type=notification_type,
        language=language,
        subject=data.subject,
        body_html=data.body_html,
        db=db,
    )

    logger.info(
        'Админ обновил email шаблон /', admin_id=admin.id, notification_type=notification_type, language=language
    )

    return {'status': 'ok', 'template': result}


@router.delete('/{notification_type}/{language}', summary='Reset template to default')
async def reset_template(
    notification_type: str,
    language: str,
    admin: User = Depends(require_permission('email_templates:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete custom template override, reverting to default."""
    _validate_template_type(notification_type)

    deleted = await delete_template_override(notification_type, language, db)

    if deleted:
        logger.info(
            'Админ сбросил email шаблон / к дефолту',
            admin_id=admin.id,
            notification_type=notification_type,
            language=language,
        )

    return {'status': 'ok', 'was_custom': deleted}


@router.post('/{notification_type}/preview', summary='Preview rendered template')
async def preview_template(
    notification_type: str,
    data: EmailTemplatePreviewRequest,
    _admin: User = Depends(require_permission('email_templates:read')),
) -> dict[str, Any]:
    """Preview a rendered email template with sample data.

    Custom content arrives with {var} placeholders — they are substituted with
    sample values so the preview matches what the user actually receives.
    """
    _validate_template_type(notification_type)

    language = data.language if data.language in AVAILABLE_LANGUAGES else 'ru'
    sample_context = _build_sample_context(notification_type)

    if data.body_html:
        # Preview custom content — substitute sample values, then wrap
        # (auto-detects styled vs simple HTML)
        body_html = substitute_context_vars(data.body_html, sample_context)
        rendered_html = EmailNotificationTemplates()._wrap_override_template(body_html, language)
        subject = substitute_context_vars(data.subject, sample_context, escape=False) or notification_type
    else:
        # Preview default template
        default_template = _get_default_template(notification_type, language, sample_context)
        if default_template:
            rendered_html = default_template['body_html']
            subject = default_template['subject']
        else:
            rendered_html = '<p>Template not found</p>'
            subject = 'N/A'

    return {
        'subject': subject,
        'body_html': rendered_html,
    }


@router.post('/{notification_type}/test', summary='Send test email')
async def send_test_email(
    notification_type: str,
    data: EmailTemplateSendTestRequest,
    admin: User = Depends(require_permission('email_templates:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Send a test email to the admin's email address."""
    from app.cabinet.services.email_service import email_service

    if not email_service.is_configured():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='SMTP is not configured',
        )

    to_email = data.email or admin.email
    if not to_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No email address provided and admin has no email',
        )

    _validate_template_type(notification_type)

    language = data.language if data.language in AVAILABLE_LANGUAGES else 'ru'
    sample_context = _build_sample_context(notification_type)

    if data.body_html:
        # Test the current editor content (possibly unsaved)
        body_html = substitute_context_vars(data.body_html, sample_context)
        body_html = EmailNotificationTemplates()._wrap_override_template(body_html, language)
        subject = substitute_context_vars(data.subject, sample_context, escape=False) or notification_type
    else:
        # Check for DB override (get_rendered_override substitutes sample context vars)
        from ..services.email_template_overrides import get_rendered_override

        rendered = await get_rendered_override(notification_type, language, sample_context, db)

        if rendered:
            subject, body_html = rendered
        else:
            default_template = _get_default_template(notification_type, language, sample_context)
            if default_template:
                subject = default_template['subject']
                body_html = default_template['body_html']
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail='Template not found',
                )

    subject = f'[TEST] {subject}'

    try:
        success = await asyncio.to_thread(
            email_service.send_email,
            to_email=to_email,
            subject=subject,
            body_html=body_html,
        )
    except Exception as e:
        logger.error('Ошибка отправки тестового email', e=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to send test email: {e!s}',
        )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to send test email',
        )

    logger.info(
        'Админ отправил тестовый email / на',
        admin_id=admin.id,
        notification_type=notification_type,
        language=language,
        to_email=to_email,
    )

    return {'status': 'ok', 'sent_to': to_email}
