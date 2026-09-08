"""
Email notification templates for different notification types.

Supports multiple languages: ru, en, zh, ua, fa
"""

import html
from collections.abc import Callable
from functools import partial
from typing import Any

from app.config import settings
from app.services.notification_types import NotificationType


class EmailNotificationTemplates:
    """HTML email templates for user notifications."""

    # Плейсхолдеры получателя, которые обёртка берёт из контекста письма.
    _LAYOUT_RECIPIENT_VARS = ('username', 'email', 'date')

    def __init__(self):
        self.service_name = settings.SMTP_FROM_NAME or 'VPN Service'
        self.cabinet_url = getattr(settings, 'CABINET_URL', '')
        # Контекст текущего рендера — чтобы обёртка знала получателя
        # ({username}, {email}), не протаскивая его через все 50 билдеров.
        self._render_context: dict[str, Any] | None = None

    # WEBHOOK_* уведомления делят один generic-билдер: значение типа -> ключ
    # копирайта в WEBHOOK_EMAIL_COPY. Ключи — строки, а не enum: NotificationType
    # импортируется лениво (цикл модулей), а редактору этот список нужен на
    # уровне класса, чтобы собирать свои записи из тех же текстов.
    WEBHOOK_EMAIL_KINDS: dict[str, str] = {
        'webhook_sub_expired': 'sub_expired',
        'webhook_sub_disabled': 'sub_disabled',
        'webhook_sub_enabled': 'sub_enabled',
        'webhook_sub_limited': 'sub_limited',
        'webhook_sub_traffic_reset': 'sub_traffic_reset',
        'webhook_sub_deleted': 'sub_deleted',
        'webhook_sub_revoked': 'sub_revoked',
        'webhook_sub_expiring': 'sub_expiring',
        'webhook_sub_first_connected': 'sub_first_connected',
        'webhook_sub_bandwidth_threshold': 'sub_bandwidth_threshold',
        'webhook_user_not_connected': 'user_not_connected',
        'webhook_device_added': 'device_added',
        'webhook_device_deleted': 'device_deleted',
        'webhook_torrent_detected': 'torrent_detected',
    }

    def _template_map(self) -> dict['NotificationType', Callable[[str, dict[str, Any]], dict[str, str]]]:
        """Тип уведомления -> билдер письма. Единственный реестр email-шаблонов.

        Список типов в редакторе админки строится отсюда же (supported_types):
        рукописная копия рядом отставала, и письма уходили со стандартным
        шаблоном, который нельзя было поменять.
        """
        template_map: dict[NotificationType, Callable[[str, dict[str, Any]], dict[str, str]]] = {
            NotificationType.BALANCE_TOPUP: self._balance_topup_template,
            NotificationType.BALANCE_CHANGE: self._balance_change_template,
            NotificationType.SUBSCRIPTION_EXPIRING: self._subscription_expiring_template,
            NotificationType.SUBSCRIPTION_EXPIRED: self._subscription_expired_template,
            NotificationType.SUBSCRIPTION_RENEWED: self._subscription_renewed_template,
            NotificationType.SUBSCRIPTION_ACTIVATED: self._subscription_activated_template,
            NotificationType.WINBACK_EXPIRED_1D: self._winback_expired_1d_template,
            NotificationType.WINBACK_DISCOUNT: self._winback_discount_template,
            NotificationType.WINBACK_TRIAL_ENDING: self._winback_trial_ending_template,
            NotificationType.AUTOPAY_SUCCESS: self._autopay_success_template,
            NotificationType.AUTOPAY_FAILED: self._autopay_failed_template,
            NotificationType.AUTOPAY_INSUFFICIENT_FUNDS: self._autopay_insufficient_funds_template,
            NotificationType.DAILY_DEBIT: self._daily_debit_template,
            NotificationType.DAILY_INSUFFICIENT_FUNDS: self._daily_insufficient_funds_template,
            NotificationType.BAN_NOTIFICATION: self._ban_template,
            NotificationType.UNBAN_NOTIFICATION: self._unban_template,
            NotificationType.WARNING_NOTIFICATION: self._warning_template,
            NotificationType.REFERRAL_BONUS: self._referral_bonus_template,
            NotificationType.REFERRAL_REGISTERED: self._referral_registered_template,
            NotificationType.REFERRAL_WELCOME: self._referral_welcome_template,
            NotificationType.PARTNER_APPLICATION_APPROVED: self._partner_approved_template,
            NotificationType.PARTNER_APPLICATION_REJECTED: self._partner_rejected_template,
            NotificationType.WITHDRAWAL_APPROVED: self._withdrawal_approved_template,
            NotificationType.WITHDRAWAL_REJECTED: self._withdrawal_rejected_template,
            NotificationType.TRAFFIC_RESET: self._traffic_reset_template,
            NotificationType.PAYMENT_RECEIVED: self._payment_received_template,
            NotificationType.NALOGO_RECEIPT: self._nalogo_receipt_template,
            NotificationType.PROMO_OFFER: self._promo_offer_template,
            NotificationType.TICKET_REPLY: self._ticket_reply_template,
            NotificationType.EMAIL_VERIFICATION: self._email_verification_template,
            NotificationType.PASSWORD_RESET: self._password_reset_template,
            NotificationType.EMAIL_CHANGE_CODE: self._email_change_code_template,
            NotificationType.GUEST_SUBSCRIPTION_DELIVERED: self._guest_subscription_delivered_template,
            NotificationType.GUEST_ACTIVATION_REQUIRED: self._guest_activation_required_template,
            NotificationType.GUEST_GIFT_RECEIVED: self._guest_gift_received_template,
            NotificationType.GUEST_CABINET_CREDENTIALS: self._guest_cabinet_credentials_template,
            NotificationType.GUEST_GIFT_LINK_BUYER: self._guest_gift_link_buyer_template,
        }
        webhook_map = {
            NotificationType(type_value): partial(self._webhook_event_email, kind)
            for type_value, kind in self.WEBHOOK_EMAIL_KINDS.items()
        }
        return {**template_map, **webhook_map}

    def supported_types(self) -> list['NotificationType']:
        """Типы, у которых есть email-шаблон, — источник истины для списка редактора."""
        return list(self._template_map())

    def get_template(
        self,
        notification_type: 'NotificationType',
        language: str,
        context: dict[str, Any],
    ) -> dict[str, str] | None:
        """
        Get email template for notification type.

        Args:
            notification_type: Type of notification
            language: Language code (ru, en, zh, ua, fa)
            context: Context data for template rendering

        Returns:
            Dict with 'subject', 'body_html', and optionally 'body_text'
        """
        template_func = self._template_map().get(notification_type)
        if not template_func:
            return None

        self._render_context = context
        try:
            return template_func(language, context)
        finally:
            self._render_context = None

    def _wrap_override_template(
        self,
        content: str,
        language: str = 'ru',
        *,
        unsubscribe_url: str = '',
        context: dict[str, Any] | None = None,
    ) -> str:
        """Wrap override template content appropriately based on its structure.

        ``unsubscribe_url`` и ``context`` (данные получателя) нужны только
        обёртке третьего уровня — у полного документа всё уже внутри.

        Three-tier detection:
        1. Full HTML document (<!DOCTYPE or <html>) — return as-is, no wrapping
        2. Styled content (has <style> tag or background CSS) — minimal HTML wrapper
           without forced colors, headers, or footers
        3. Simple HTML fragment — wrap with base template (header, footer, white bg)
           for backward compatibility
        """
        content_stripped = content.strip()
        content_lower = content_stripped.lower()

        # Tier 1: Full HTML document — return as-is
        if content_lower.startswith('<!doctype') or content_lower.startswith('<html'):
            return content_stripped

        # Tier 2: Styled content — minimal wrapper without forced styling
        if '<style' in content_lower or 'background' in content_lower:
            return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0;">
    {content}
</body>
</html>"""

        # Tier 3: Simple HTML fragment — use base template for structure
        self._render_context = context
        try:
            return self._get_base_template(content, language, unsubscribe_url)
        finally:
            self._render_context = None

    def _get_base_template(self, content: str, language: str = 'ru', unsubscribe_url: str = '') -> str:
        """Wrap content in the email layout — сохранённая в редакторе обёртка, иначе встроенная.

        ``unsubscribe_url`` непустой только у маркетинговых писем — у писем со
        сбросом пароля или чеком отписке взяться неоткуда.
        """
        from .email_layout import render_email_layout, resolve_email_layout

        recipient = {
            key: value
            for key, value in (self._render_context or {}).items()
            if key in self._LAYOUT_RECIPIENT_VARS and value not in (None, '')
        }
        return render_email_layout(
            resolve_email_layout(language),
            language,
            {**recipient, 'content': content, 'unsubscribe_url': unsubscribe_url, 'service_name': self.service_name},
        )

    def _get_cabinet_button(self, language: str) -> str:
        """Get cabinet link button HTML."""
        if not self.cabinet_url:
            return ''

        texts = {
            'ru': 'Открыть личный кабинет',
            'en': 'Open Dashboard',
            'zh': '打开控制面板',
            'ua': 'Відкрити особистий кабінет',
            'fa': 'باز کردن پنل کاربری',
        }
        text = texts.get(language, texts['en'])

        return f'<p style="text-align: center;"><a href="{self.cabinet_url}" class="button">{text}</a></p>'

    def _link_button(self, url: str, language: str, texts: dict[str, str]) -> str:
        """Кнопка с произвольной ссылкой в том же стиле, что кнопка кабинета."""
        if not url:
            return ''
        text = texts.get(language, texts['en'])
        return f'<p style="text-align: center;"><a href="{html.escape(url, quote=True)}" class="button">{text}</a></p>'

    def _nalogo_receipt_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Email: чек NaloGO («Мой налог») по платежу — файл во вложении, ссылка запасная."""
        amount = html.escape(str(context.get('amount', '')))
        receipt_url = html.escape(str(context.get('receipt_url', '')), quote=True)
        has_attachment = bool(context.get('has_attachment'))
        subjects = {
            'ru': 'Чек по вашему платежу',
            'en': 'Receipt for your payment',
            'zh': '您的付款收据',
            'ua': 'Чек за вашим платежем',
        }
        attachment_lines = {
            'ru': '<p>Файл чека — во вложении к этому письму.</p>',
            'en': '<p>The receipt file is attached to this email.</p>',
            'zh': '<p>收据文件已附在本邮件中。</p>',
            'ua': '<p>Файл чека — у вкладенні до цього листа.</p>',
        }
        attachment = {lang: line if has_attachment else '' for lang, line in attachment_lines.items()}
        bodies = {
            'ru': f'<h2>🧾 Чек по вашему платежу сформирован</h2><div class="highlight"><p>💰 Сумма: <strong>{amount}</strong></p><p>Чек зарегистрирован в ФНС через сервис «Мой налог».</p>{attachment["ru"]}</div><p><a href="{receipt_url}">Открыть чек на сайте ФНС</a> (ссылка может не открываться при включённом VPN или из-за рубежа).</p>',
            'en': f'<h2>🧾 Your payment receipt is ready</h2><div class="highlight"><p>💰 Amount: <strong>{amount}</strong></p><p>The receipt is registered with the Russian tax service (“Moy Nalog”).</p>{attachment["en"]}</div><p><a href="{receipt_url}">Open the receipt on the tax service website</a> (the link may not open with VPN on or from abroad).</p>',
            'zh': f'<h2>🧾 您的付款收据已生成</h2><div class="highlight"><p>💰 金额：<strong>{amount}</strong></p><p>收据已通过“Мой налог”服务在俄罗斯税务局登记。</p>{attachment["zh"]}</div><p><a href="{receipt_url}">在税务局网站打开收据</a>（开启 VPN 或在境外时链接可能无法打开）。</p>',
            'ua': f'<h2>🧾 Чек за вашим платежем сформовано</h2><div class="highlight"><p>💰 Сума: <strong>{amount}</strong></p><p>Чек зареєстровано у ФНС через сервіс «Мой налог».</p>{attachment["ua"]}</div><p><a href="{receipt_url}">Відкрити чек на сайті ФНС</a> (посилання може не відкриватися з увімкненим VPN або з-за кордону).</p>',
        }
        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _guest_gift_link_buyer_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Email покупателю подарка: ссылка на активацию, чтобы переслать получателю."""
        claim_url = str(context.get('claim_url', '') or '')
        claim_url_html = html.escape(claim_url, quote=True)
        tariff_name = html.escape(str(context.get('tariff_name', '') or ''))
        period_days = context.get('period_days')
        subjects = {
            'ru': 'Ссылка на ваш подарок',
            'en': 'Your gift link',
            'zh': '您的礼物链接',
            'ua': 'Посилання на ваш подарунок',
        }
        details = {
            'ru': f'<p>Подарок: <strong>{tariff_name}</strong>{f" на {period_days} дн." if period_days else ""}</p>'
            if tariff_name
            else '',
            'en': f'<p>Gift: <strong>{tariff_name}</strong>{f" for {period_days} days" if period_days else ""}</p>'
            if tariff_name
            else '',
            'zh': f'<p>礼物：<strong>{tariff_name}</strong>{f"，{period_days} 天" if period_days else ""}</p>'
            if tariff_name
            else '',
            'ua': f'<p>Подарунок: <strong>{tariff_name}</strong>{f" на {period_days} дн." if period_days else ""}</p>'
            if tariff_name
            else '',
        }
        button = {
            'ru': 'Открыть ссылку на подарок',
            'en': 'Open gift link',
            'zh': '打开礼物链接',
            'ua': 'Відкрити посилання на подарунок',
        }
        link = f'<p><a href="{claim_url_html}">{claim_url_html}</a></p>'
        bodies = {
            'ru': f'<h2>🎁 Спасибо за покупку подарка!</h2><div class="highlight">{details["ru"]}<p>Перешлите эту ссылку тому, кому предназначен подарок, — он активирует его сам:</p>{link}</div>{self._link_button(claim_url, language, button)}',
            'en': f'<h2>🎁 Thanks for your gift purchase!</h2><div class="highlight">{details["en"]}<p>Forward this link to the person the gift is for — they activate it themselves:</p>{link}</div>{self._link_button(claim_url, language, button)}',
            'zh': f'<h2>🎁 感谢您购买礼物！</h2><div class="highlight">{details["zh"]}<p>请将此链接转发给收礼人，由其自行激活：</p>{link}</div>{self._link_button(claim_url, language, button)}',
            'ua': f'<h2>🎁 Дякуємо за покупку подарунка!</h2><div class="highlight">{details["ua"]}<p>Перешліть це посилання тому, кому призначено подарунок, — він активує його сам:</p>{link}</div>{self._link_button(claim_url, language, button)}',
        }
        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Balance Templates
    # ============================================================================

    def _balance_topup_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for balance top-up notification."""
        amount = context.get('formatted_amount', f'{context.get("amount_rubles", 0):.2f} ₽')
        balance = context.get('formatted_balance', f'{context.get("new_balance_rubles", 0):.2f} ₽')

        subjects = {
            'ru': f'Баланс пополнен на {amount}',
            'en': f'Balance topped up by {amount}',
            'zh': f'余额已充值 {amount}',
            'ua': f'Баланс поповнено на {amount}',
        }

        bodies = {
            'ru': f"""
                <h2>Баланс успешно пополнен!</h2>
                <div class="highlight success">
                    <p>Сумма пополнения: <span class="amount">+{amount}</span></p>
                    <p>Текущий баланс: <strong>{balance}</strong></p>
                </div>
                <p>Спасибо за использование нашего сервиса!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Balance Successfully Topped Up!</h2>
                <div class="highlight success">
                    <p>Top-up amount: <span class="amount">+{amount}</span></p>
                    <p>Current balance: <strong>{balance}</strong></p>
                </div>
                <p>Thank you for using our service!</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>充值成功！</h2>
                <div class="highlight success">
                    <p>充值金额: <span class="amount">+{amount}</span></p>
                    <p>当前余额: <strong>{balance}</strong></p>
                </div>
                <p>感谢使用我们的服务！</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Баланс успішно поповнено!</h2>
                <div class="highlight success">
                    <p>Сума поповнення: <span class="amount">+{amount}</span></p>
                    <p>Поточний баланс: <strong>{balance}</strong></p>
                </div>
                <p>Дякуємо за використання нашого сервісу!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _balance_change_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for balance change notification."""
        amount = context.get('formatted_amount', f'{context.get("amount_rubles", 0):.2f} ₽')
        balance = context.get('formatted_balance', f'{context.get("new_balance_rubles", 0):.2f} ₽')

        subjects = {
            'ru': 'Изменение баланса',
            'en': 'Balance Changed',
            'zh': '余额变动',
            'ua': 'Зміна балансу',
        }

        bodies = {
            'ru': f"""
                <h2>Изменение баланса</h2>
                <div class="highlight">
                    <p>Сумма: <strong>{amount}</strong></p>
                    <p>Текущий баланс: <strong>{balance}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Balance Changed</h2>
                <div class="highlight">
                    <p>Amount: <strong>{amount}</strong></p>
                    <p>Current balance: <strong>{balance}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>余额变动</h2>
                <div class="highlight">
                    <p>金额: <strong>{amount}</strong></p>
                    <p>当前余额: <strong>{balance}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Зміна балансу</h2>
                <div class="highlight">
                    <p>Сума: <strong>{amount}</strong></p>
                    <p>Поточний баланс: <strong>{balance}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Subscription Templates
    # ============================================================================

    def _subscription_expiring_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for subscription expiring notification."""
        days_left = context.get('days_left', 0)
        expires_at = context.get('expires_at', '')
        tariff_name = html.escape(context.get('tariff_name', ''))
        tariff_suffix_ru = f' «{tariff_name}»' if tariff_name else ''
        tariff_suffix_en = f' "{tariff_name}"' if tariff_name else ''
        tariff_line_ru = f'<p>Тариф: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_en = f'<p>Plan: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_zh = f'<p>套餐: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_ua = f'<p>Тариф: <strong>{tariff_name}</strong></p>' if tariff_name else ''

        subjects = {
            'ru': f'Подписка{tariff_suffix_ru} истекает через {days_left} дн.',
            'en': f'Subscription{tariff_suffix_en} expires in {days_left} day(s)',
            'zh': f'订阅将在 {days_left} 天后到期',
            'ua': f'Підписка{tariff_suffix_ru} закінчується через {days_left} дн.',
        }

        bodies = {
            'ru': f"""
                <h2>Подписка скоро истекает</h2>
                <div class="highlight warning">
                    {tariff_line_ru}
                    <p>Ваша подписка истекает через <strong>{days_left}</strong> дн.</p>
                    <p>Дата истечения: <strong>{expires_at}</strong></p>
                </div>
                <p>Продлите подписку, чтобы не потерять доступ к сервису.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Subscription Expiring Soon</h2>
                <div class="highlight warning">
                    {tariff_line_en}
                    <p>Your subscription expires in <strong>{days_left}</strong> day(s).</p>
                    <p>Expiration date: <strong>{expires_at}</strong></p>
                </div>
                <p>Renew your subscription to maintain access to our service.</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>订阅即将到期</h2>
                <div class="highlight warning">
                    {tariff_line_zh}
                    <p>您的订阅将在 <strong>{days_left}</strong> 天后到期。</p>
                    <p>到期日期: <strong>{expires_at}</strong></p>
                </div>
                <p>请续订以保持对服务的访问。</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Підписка скоро закінчується</h2>
                <div class="highlight warning">
                    {tariff_line_ua}
                    <p>Ваша підписка закінчується через <strong>{days_left}</strong> дн.</p>
                    <p>Дата закінчення: <strong>{expires_at}</strong></p>
                </div>
                <p>Продовжіть підписку, щоб не втратити доступ до сервісу.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _subscription_expired_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for subscription expired notification."""
        tariff_name = html.escape(context.get('tariff_name', ''))
        tariff_suffix_ru = f' «{tariff_name}»' if tariff_name else ''
        tariff_suffix_en = f' "{tariff_name}"' if tariff_name else ''
        tariff_line_ru = f'<p>Тариф: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_en = f'<p>Plan: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_zh = f'<p>套餐: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_ua = f'<p>Тариф: <strong>{tariff_name}</strong></p>' if tariff_name else ''

        subjects = {
            'ru': f'Подписка{tariff_suffix_ru} истекла',
            'en': f'Subscription{tariff_suffix_en} Expired',
            'zh': '订阅已到期',
            'ua': f'Підписка{tariff_suffix_ru} закінчилась',
        }

        bodies = {
            'ru': f"""
                <h2>Подписка истекла</h2>
                <div class="highlight danger">
                    {tariff_line_ru}
                    <p>Ваша подписка истекла. Доступ к VPN отключён.</p>
                </div>
                <p>Оформите новую подписку, чтобы продолжить использование сервиса.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Subscription Expired</h2>
                <div class="highlight danger">
                    {tariff_line_en}
                    <p>Your subscription has expired. VPN access has been disabled.</p>
                </div>
                <p>Purchase a new subscription to continue using our service.</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>订阅已到期</h2>
                <div class="highlight danger">
                    {tariff_line_zh}
                    <p>您的订阅已到期。VPN访问已被禁用。</p>
                </div>
                <p>请购买新订阅以继续使用我们的服务。</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Підписка закінчилась</h2>
                <div class="highlight danger">
                    {tariff_line_ua}
                    <p>Ваша підписка закінчилась. Доступ до VPN вимкнено.</p>
                </div>
                <p>Оформіть нову підписку, щоб продовжити використання сервісу.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _subscription_renewed_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for subscription renewed notification."""
        new_expires_at = context.get('new_expires_at', '')
        tariff_name = html.escape(context.get('tariff_name', ''))
        tariff_suffix_ru = f' «{tariff_name}»' if tariff_name else ''
        tariff_suffix_en = f' "{tariff_name}"' if tariff_name else ''
        tariff_line_ru = f'<p>Тариф: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_en = f'<p>Plan: <strong>{tariff_name}</strong></p>' if tariff_name else ''

        subjects = {
            'ru': f'Подписка{tariff_suffix_ru} продлена',
            'en': f'Subscription{tariff_suffix_en} Renewed',
            'zh': '订阅已续订',
            'ua': f'Підписку{tariff_suffix_ru} продовжено',
        }

        bodies = {
            'ru': f"""
                <h2>Подписка успешно продлена!</h2>
                <div class="highlight success">
                    {tariff_line_ru}
                    <p>Ваша подписка была успешно продлена.</p>
                    <p>Новая дата истечения: <strong>{new_expires_at}</strong></p>
                </div>
                <p>Спасибо за использование нашего сервиса!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Subscription Successfully Renewed!</h2>
                <div class="highlight success">
                    {tariff_line_en}
                    <p>Your subscription has been successfully renewed.</p>
                    <p>New expiration date: <strong>{new_expires_at}</strong></p>
                </div>
                <p>Thank you for using our service!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    WEBHOOK_EMAIL_COPY = {
        'sub_expired': {
            'zh': (
                '订阅已到期',
                '<p>您的 VPN 订阅已到期，访问已关闭。</p><p>请续订以恢复使用。</p>',
            ),
            'ua': (
                'Підписка закінчилася',
                '<p>Ваша VPN-підписка закінчилася — доступ вимкнено.</p><p>Продовжте підписку, щоб повернутися до сервісу.</p>',
            ),
            'ru': (
                'Подписка закончилась',
                '<p>Ваша VPN-подписка истекла — доступ отключён.</p><p>Продлите подписку, чтобы вернуться в сервис.</p>',
            ),
            'en': (
                'Subscription expired',
                '<p>Your VPN subscription has expired and access is off.</p><p>Renew it to get back online.</p>',
            ),
        },
        'sub_disabled': {
            'zh': (
                '订阅已暂停',
                '<p>您的订阅已被暂时停用。</p><p>请查看个人中心或联系客服。</p>',
            ),
            'ua': (
                'Підписку призупинено',
                '<p>Вашу підписку тимчасово вимкнено.</p><p>Перевірте особистий кабінет або напишіть у підтримку.</p>',
            ),
            'ru': (
                'Подписка приостановлена',
                '<p>Ваша подписка временно отключена.</p><p>Проверьте личный кабинет или напишите в поддержку.</p>',
            ),
            'en': (
                'Subscription suspended',
                '<p>Your subscription has been temporarily disabled.</p><p>Check your dashboard or contact support.</p>',
            ),
        },
        'sub_enabled': {
            'zh': (
                '订阅已恢复',
                '<p>VPN 访问已恢复，可以继续使用。</p>',
            ),
            'ua': (
                'Підписка знову активна',
                '<p>Доступ до VPN відновлено — можна користуватися.</p>',
            ),
            'ru': ('Подписка снова активна', '<p>Доступ к VPN восстановлен — можно пользоваться.</p>'),
            'en': ('Subscription re-activated', '<p>Your VPN access has been restored.</p>'),
        },
        'sub_limited': {
            'zh': (
                '流量已达上限',
                '<p>订阅流量已用完。</p><p>请购买流量或等待重置后继续使用。</p>',
            ),
            'ua': (
                'Досягнуто ліміт трафіку',
                '<p>Трафік за підпискою вичерпано.</p><p>Докупіть трафік або дочекайтеся скидання, щоб продовжити.</p>',
            ),
            'ru': (
                'Достигнут лимит трафика',
                '<p>Трафик по подписке исчерпан.</p><p>Докупите трафик или дождитесь сброса, чтобы продолжить.</p>',
            ),
            'en': (
                'Traffic limit reached',
                '<p>You have used up your subscription traffic.</p><p>Top up traffic or wait for the reset to continue.</p>',
            ),
        },
        'sub_traffic_reset': {
            'zh': (
                '流量已重置',
                '<p>流量计数已重置，限额再次可用。</p>',
            ),
            'ua': (
                'Трафік оновлено',
                '<p>Лічильник трафіку скинуто — ліміт знову доступний.</p>',
            ),
            'ru': ('Трафик обновлён', '<p>Счётчик трафика сброшен — лимит снова доступен.</p>'),
            'en': ('Traffic reset', '<p>Your traffic counter has been reset — the limit is available again.</p>'),
        },
        'sub_deleted': {
            'zh': (
                '订阅已删除',
                '<p>您的订阅已被删除。</p><p>如属误操作，请联系客服。</p>',
            ),
            'ua': (
                'Підписку видалено',
                '<p>Вашу підписку було видалено.</p><p>Якщо це помилка — напишіть у підтримку.</p>',
            ),
            'ru': (
                'Подписка удалена',
                '<p>Ваша подписка была удалена.</p><p>Если это ошибка — напишите в поддержку.</p>',
            ),
            'en': (
                'Subscription deleted',
                '<p>Your subscription has been deleted.</p><p>If this is a mistake, contact support.</p>',
            ),
        },
        'sub_revoked': {
            'zh': (
                '订阅链接已更新',
                '<p>您的订阅链接已重新签发。</p><p>请从个人中心导入新链接。</p>',
            ),
            'ua': (
                'Посилання-підписку оновлено',
                '<p>Ваше посилання-підписку було перевипущено.</p><p>Імпортуйте нове посилання з особистого кабінету.</p>',
            ),
            'ru': (
                'Ссылка-подписка обновлена',
                '<p>Ваша ссылка-подписка была перевыпущена.</p><p>Импортируйте новую ссылку из личного кабинета.</p>',
            ),
            'en': (
                'Subscription link reissued',
                '<p>Your subscription link has been reissued.</p><p>Import the new link from your dashboard.</p>',
            ),
        },
        'sub_expiring': {
            'ru': (
                'Подписка скоро закончится',
                '<p>Срок действия вашей VPN-подписки подходит к концу.</p><p>Продлите её заранее, чтобы не остаться без доступа.</p>',
            ),
            'en': (
                'Subscription expiring soon',
                '<p>Your VPN subscription is about to expire.</p><p>Renew it in advance to avoid losing access.</p>',
            ),
            'zh': (
                '订阅即将到期',
                '<p>您的 VPN 订阅即将到期。</p><p>请提前续订，以免失去访问。</p>',
            ),
            'ua': (
                'Підписка скоро закінчиться',
                '<p>Термін дії вашої VPN-підписки добігає кінця.</p><p>Продовжте її заздалегідь, щоб не залишитися без доступу.</p>',
            ),
        },
        'sub_first_connected': {
            'zh': (
                '连接成功',
                '<p>您已成功连接到 VPN，祝使用愉快！</p>',
            ),
            'ua': (
                'Підключення встановлено',
                '<p>Ви успішно підключилися до VPN. Приємного користування!</p>',
            ),
            'ru': ('Подключение установлено', '<p>Вы успешно подключились к VPN. Приятного пользования!</p>'),
            'en': ('You are connected', '<p>You have successfully connected to the VPN. Enjoy!</p>'),
        },
        'sub_bandwidth_threshold': {
            'zh': (
                '流量即将用完',
                '<p>您已使用大部分流量。</p><p>请及时购买流量，以免失去访问。</p>',
            ),
            'ua': (
                'Трафік закінчується',
                '<p>Ви використали більшу частину трафіку.</p><p>Докупіть трафік, щоб не залишитися без доступу.</p>',
            ),
            'ru': (
                'Трафик на исходе',
                '<p>Вы израсходовали большую часть трафика.</p><p>Докупите трафик, чтобы не остаться без доступа.</p>',
            ),
            'en': (
                'Traffic running low',
                '<p>You have used most of your traffic.</p><p>Top up to avoid losing access.</p>',
            ),
        },
        'user_not_connected': {
            'zh': (
                '需要帮助连接 VPN 吗？',
                '<p>您的订阅<strong>有效</strong>，但应用尚未配置，因此 VPN 无法使用。</p><p>只需 5 分钟：安装应用并导入订阅链接。如有问题，请联系客服。</p>',
            ),
            'ua': (
                'Потрібна допомога з підключенням VPN?',
                '<p>Ваша підписка <strong>активна</strong>, але застосунок ще не налаштовано — тому VPN не працює.</p><p>Це займає 5 хвилин: встановіть застосунок та імпортуйте посилання-підписку. Якщо щось не виходить — напишіть у підтримку, допоможемо.</p>',
            ),
            'ru': (
                'Нужна помощь с подключением VPN?',
                '<p>Ваша подписка <strong>активна</strong>, но приложение пока не настроено — поэтому VPN не работает.</p><p>Это занимает 5 минут: установите приложение и импортируйте ссылку-подписку. Если что-то не выходит — напишите в поддержку, поможем.</p>',
            ),
            'en': (
                'Need help connecting your VPN?',
                '<p>Your subscription is <strong>active</strong>, but the app is not set up yet — so the VPN is not working.</p><p>It takes 5 minutes: install the app and import your subscription link. If anything goes wrong, contact support.</p>',
            ),
        },
        'device_added': {
            'zh': (
                '已连接新设备',
                '<p>您的订阅新增了一台设备{device}。</p><p>如果不是您本人操作，请联系客服。</p>',
            ),
            'ua': (
                'Підключено новий пристрій',
                '<p>До вашої підписки додано пристрій{device}.</p><p>Якщо це були не ви — напишіть у підтримку.</p>',
            ),
            'ru': (
                'Новое устройство подключено',
                '<p>К вашей подписке добавлено устройство{device}.</p><p>Если это были не вы — напишите в поддержку.</p>',
            ),
            'en': (
                'New device connected',
                '<p>A device{device} was added to your subscription.</p><p>If this was not you, contact support.</p>',
            ),
        },
        'device_deleted': {
            'zh': (
                '设备已断开',
                '<p>设备{device}已从您的订阅解绑。</p>',
            ),
            'ua': (
                'Пристрій відключено',
                '<p>Пристрій{device} відв’язано від вашої підписки.</p>',
            ),
            'ru': ('Устройство отключено', '<p>Устройство{device} отвязано от вашей подписки.</p>'),
            'en': ('Device removed', '<p>A device{device} was removed from your subscription.</p>'),
        },
        'torrent_detected': {
            'zh': (
                '检测到种子流量',
                '<p>检测到 torrent 活动。</p><p>VPN 上已封锁此类流量，请使用常规应用。</p>',
            ),
            'ua': (
                'Виявлено торент-трафік',
                '<p>Зафіксовано torrent-активність.</p><p>У VPN її заблоковано — використовуйте звичайні застосунки.</p>',
            ),
            'ru': (
                'Обнаружен торрент-трафик',
                '<p>Зафиксирована torrent-активность.</p><p>На VPN она заблокирована — используйте обычные приложения.</p>',
            ),
            'en': (
                'Torrent traffic detected',
                '<p>Torrent activity was detected.</p><p>It is blocked on the VPN — please use regular apps.</p>',
            ),
        },
    }

    def _webhook_event_email(self, kind: str, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Generic email for Remnawave webhook notifications (email-only users).

        Each WEBHOOK_* notification type routes through notification_delivery_service,
        which sends email to email-only users — but these types had no email template,
        so email was silently skipped. This covers all of them.
        """
        lang = language if language in ('ru', 'en', 'zh', 'ua') else 'ru'
        copy = self.WEBHOOK_EMAIL_COPY.get(kind, self.WEBHOOK_EMAIL_COPY['user_not_connected'])
        subject, body = copy.get(lang, copy['ru'])

        device = str(context.get('device') or context.get('device_name') or '').strip()
        device_suffix = f' — {html.escape(device)}' if device and device != '—' else ''
        body = body.replace('{device}', device_suffix)

        content = f'<h2>{subject}</h2><div class="highlight">{body}</div>{self._get_cabinet_button(language)}'
        return {
            'subject': subject,
            'body_html': self._get_base_template(content, language),
        }

    def _winback_expired_1d_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Email: subscription lapsed 1 day ago (email-only users)."""
        end_date = html.escape(str(context.get('end_date', '')))
        subjects = {
            'ru': 'Подписка закончилась',
            'en': 'Your subscription has ended',
            'zh': '订阅已结束',
            'ua': 'Підписка закінчилася',
        }
        bodies = {
            'ru': f'<h2>Подписка закончилась</h2><div class="highlight danger"><p>Ваша VPN-подписка истекла{f" ({end_date})" if end_date else ""} — доступ отключён.</p><p>Продлите подписку, чтобы вернуться в сервис.</p></div>{self._get_cabinet_button(language)}',
            'en': f'<h2>Your subscription has ended</h2><div class="highlight danger"><p>Your VPN subscription has expired — access is off.</p><p>Renew it to get back online.</p></div>{self._get_cabinet_button(language)}',
            'zh': f'<h2>订阅已结束</h2><div class="highlight danger"><p>您的 VPN 订阅已到期{f"（{end_date}）" if end_date else ""}，访问已关闭。</p><p>请续订以恢复使用。</p></div>{self._get_cabinet_button(language)}',
            'ua': f'<h2>Підписка закінчилася</h2><div class="highlight danger"><p>Ваша VPN-підписка закінчилася{f" ({end_date})" if end_date else ""} — доступ вимкнено.</p><p>Продовжте підписку, щоб повернутися до сервісу.</p></div>{self._get_cabinet_button(language)}',
        }
        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(
                bodies.get(language, bodies['ru']), language, context.get('unsubscribe_url', '')
            ),
        }

    def _winback_discount_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Email: winback discount offer (email-only users). Offer is already on the account."""
        percent = context.get('percent', '')
        expires_at = html.escape(str(context.get('expires_at', '')))
        subjects = {
            'ru': f'Скидка {percent}% на продление',
            'en': f'{percent}% off your renewal',
            'zh': f'续订享 {percent}% 折扣',
            'ua': f'Знижка {percent}% на продовження',
        }
        bodies = {
            'ru': f'<h2>Скидка {percent}% на продление</h2><div class="highlight"><p>Возвращайтесь со скидкой <strong>{percent}%</strong>{f" — действует до {expires_at}" if expires_at else ""}.</p><p>Скидка уже привязана к вашему аккаунту и суммируется с промогруппой — просто продлите подписку в кабинете.</p></div>{self._get_cabinet_button(language)}',
            'en': f'<h2>{percent}% off your renewal</h2><div class="highlight"><p>Come back with a <strong>{percent}%</strong> discount{f", valid until {expires_at}" if expires_at else ""}.</p><p>The discount is already applied to your account and stacks with your promo group — just renew in the dashboard.</p></div>{self._get_cabinet_button(language)}',
            'zh': f'<h2>续订享 {percent}% 折扣</h2><div class="highlight"><p>回来享受 <strong>{percent}%</strong> 折扣{f"（有效期至 {expires_at}）" if expires_at else ""}。</p><p>折扣已绑定到您的账户，并与促销组叠加 — 只需在个人中心续订即可。</p></div>{self._get_cabinet_button(language)}',
            'ua': f'<h2>Знижка {percent}% на продовження</h2><div class="highlight"><p>Повертайтеся зі знижкою <strong>{percent}%</strong>{f" — діє до {expires_at}" if expires_at else ""}.</p><p>Знижка вже прив’язана до вашого акаунта й сумується з промогрупою — просто продовжте підписку в кабінеті.</p></div>{self._get_cabinet_button(language)}',
        }
        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(
                bodies.get(language, bodies['ru']), language, context.get('unsubscribe_url', '')
            ),
        }

    def _winback_trial_ending_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Email: trial ending soon (email-only users)."""
        subjects = {
            'ru': 'Пробная подписка скоро закончится',
            'en': 'Your trial is ending soon',
            'zh': '试用即将结束',
            'ua': 'Пробна підписка скоро закінчиться',
        }
        bodies = {
            'ru': f'<h2>Пробная подписка скоро закончится</h2><div class="highlight warning"><p>Ваша тестовая подписка скоро истекает.</p><p>Оформите подписку, чтобы не остаться без VPN — конфиг и устройства сохранятся.</p></div>{self._get_cabinet_button(language)}',
            'en': f'<h2>Your trial is ending soon</h2><div class="highlight warning"><p>Your trial subscription is about to expire.</p><p>Subscribe to keep your VPN — your config and devices stay.</p></div>{self._get_cabinet_button(language)}',
            'zh': f'<h2>试用即将结束</h2><div class="highlight warning"><p>您的试用订阅即将到期。</p><p>订阅以继续使用 VPN — 配置和设备将保留。</p></div>{self._get_cabinet_button(language)}',
            'ua': f'<h2>Пробна підписка скоро закінчиться</h2><div class="highlight warning"><p>Ваша тестова підписка скоро закінчується.</p><p>Оформіть підписку, щоб не залишитися без VPN — конфіг і пристрої збережуться.</p></div>{self._get_cabinet_button(language)}',
        }
        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(
                bodies.get(language, bodies['ru']), language, context.get('unsubscribe_url', '')
            ),
        }

    def _subscription_activated_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for subscription activated notification."""
        expires_at = context.get('expires_at', '')
        tariff_name = html.escape(context.get('tariff_name', ''))
        tariff_suffix_ru = f' «{tariff_name}»' if tariff_name else ''
        tariff_suffix_en = f' "{tariff_name}"' if tariff_name else ''
        tariff_line_ru = f'<p>Тариф: <strong>{tariff_name}</strong></p>' if tariff_name else ''
        tariff_line_en = f'<p>Plan: <strong>{tariff_name}</strong></p>' if tariff_name else ''

        subjects = {
            'ru': f'Подписка{tariff_suffix_ru} активирована',
            'en': f'Subscription{tariff_suffix_en} Activated',
            'zh': '订阅已激活',
            'ua': f'Підписку{tariff_suffix_ru} активовано',
        }

        bodies = {
            'ru': f"""
                <h2>Подписка активирована!</h2>
                <div class="highlight success">
                    {tariff_line_ru}
                    <p>Ваша VPN подписка успешно активирована.</p>
                    <p>Действует до: <strong>{expires_at}</strong></p>
                </div>
                <p>Теперь вы можете пользоваться VPN сервисом.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Subscription Activated!</h2>
                <div class="highlight success">
                    {tariff_line_en}
                    <p>Your VPN subscription has been successfully activated.</p>
                    <p>Valid until: <strong>{expires_at}</strong></p>
                </div>
                <p>You can now use the VPN service.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Autopay Templates
    # ============================================================================

    def _autopay_success_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for successful autopay notification."""
        amount = context.get('formatted_amount', f'{context.get("amount_rubles", 0):.2f} ₽')
        new_expires_at = context.get('new_expires_at', '')

        subjects = {
            'ru': 'Автопродление выполнено',
            'en': 'Auto-renewal Successful',
            'zh': '自动续订成功',
            'ua': 'Автопродовження виконано',
        }

        bodies = {
            'ru': f"""
                <h2>Автопродление выполнено</h2>
                <div class="highlight success">
                    <p>Ваша подписка была автоматически продлена.</p>
                    <p>Списано с баланса: <strong>{amount}</strong></p>
                    <p>Новая дата истечения: <strong>{new_expires_at}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Auto-renewal Successful</h2>
                <div class="highlight success">
                    <p>Your subscription has been automatically renewed.</p>
                    <p>Charged from balance: <strong>{amount}</strong></p>
                    <p>New expiration date: <strong>{new_expires_at}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _autopay_failed_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for failed autopay notification."""
        reason = html.escape(context.get('reason', ''))

        subjects = {
            'ru': 'Ошибка автопродления',
            'en': 'Auto-renewal Failed',
            'zh': '自动续订失败',
            'ua': 'Помилка автопродовження',
        }

        bodies = {
            'ru': f"""
                <h2>Ошибка автопродления</h2>
                <div class="highlight danger">
                    <p>Не удалось автоматически продлить подписку.</p>
                    {f'<p>Причина: {reason}</p>' if reason else ''}
                </div>
                <p>Пожалуйста, пополните баланс и продлите подписку вручную.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Auto-renewal Failed</h2>
                <div class="highlight danger">
                    <p>Failed to automatically renew your subscription.</p>
                    {f'<p>Reason: {reason}</p>' if reason else ''}
                </div>
                <p>Please top up your balance and renew manually.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _autopay_insufficient_funds_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for autopay insufficient funds notification."""
        required = context.get('required_amount', '')
        balance = context.get('current_balance', '')

        subjects = {
            'ru': 'Недостаточно средств для автопродления',
            'en': 'Insufficient Funds for Auto-renewal',
            'zh': '余额不足无法自动续订',
            'ua': 'Недостатньо коштів для автопродовження',
        }

        bodies = {
            'ru': f"""
                <h2>Недостаточно средств</h2>
                <div class="highlight warning">
                    <p>Недостаточно средств на балансе для автопродления подписки.</p>
                    {f'<p>Требуется: <strong>{required}</strong></p>' if required else ''}
                    {f'<p>На балансе: <strong>{balance}</strong></p>' if balance else ''}
                </div>
                <p>Пополните баланс, чтобы подписка была продлена автоматически.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Insufficient Funds</h2>
                <div class="highlight warning">
                    <p>Insufficient balance for subscription auto-renewal.</p>
                    {f'<p>Required: <strong>{required}</strong></p>' if required else ''}
                    {f'<p>Balance: <strong>{balance}</strong></p>' if balance else ''}
                </div>
                <p>Top up your balance for automatic renewal.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Daily Subscription Templates
    # ============================================================================

    def _daily_debit_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for daily subscription debit notification."""
        amount = context.get('formatted_amount', f'{context.get("amount_rubles", 0):.2f} ₽')
        balance = context.get('formatted_balance', f'{context.get("new_balance_rubles", 0):.2f} ₽')

        subjects = {
            'ru': f'Списание за подписку: {amount}',
            'en': f'Subscription charge: {amount}',
            'zh': f'订阅扣费: {amount}',
            'ua': f'Списання за підписку: {amount}',
        }

        bodies = {
            'ru': f"""
                <h2>Ежедневное списание</h2>
                <div class="highlight">
                    <p>С вашего баланса списано: <strong>{amount}</strong></p>
                    <p>Остаток на балансе: <strong>{balance}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Daily Charge</h2>
                <div class="highlight">
                    <p>Charged from your balance: <strong>{amount}</strong></p>
                    <p>Remaining balance: <strong>{balance}</strong></p>
                </div>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _daily_insufficient_funds_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for daily subscription insufficient funds."""
        subjects = {
            'ru': 'Недостаточно средств для продления',
            'en': 'Insufficient Funds',
            'zh': '余额不足',
            'ua': 'Недостатньо коштів',
        }

        bodies = {
            'ru': f"""
                <h2>Недостаточно средств</h2>
                <div class="highlight danger">
                    <p>На балансе недостаточно средств для продления подписки.</p>
                    <p>Подписка будет приостановлена.</p>
                </div>
                <p>Пополните баланс, чтобы продолжить использование сервиса.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Insufficient Funds</h2>
                <div class="highlight danger">
                    <p>Insufficient balance to continue subscription.</p>
                    <p>Your subscription will be suspended.</p>
                </div>
                <p>Please top up your balance to continue using the service.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _traffic_reset_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for traffic reset notification."""
        subjects = {
            'ru': 'Трафик обновлён',
            'en': 'Traffic Reset',
            'zh': '流量已重置',
            'ua': 'Трафік оновлено',
        }

        bodies = {
            'ru': f"""
                <h2>Трафик обновлён</h2>
                <div class="highlight success">
                    <p>Ваш трафик был сброшен. Вы можете продолжить использование VPN.</p>
                </div>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Traffic Reset</h2>
                <div class="highlight success">
                    <p>Your traffic has been reset. You can continue using the VPN.</p>
                </div>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Account Status Templates
    # ============================================================================

    def _ban_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for ban notification."""
        reason = html.escape(context.get('reason', ''))

        subjects = {
            'ru': 'Аккаунт заблокирован',
            'en': 'Account Suspended',
            'zh': '账户已被封禁',
            'ua': 'Обліковий запис заблоковано',
        }

        bodies = {
            'ru': f"""
                <h2>Аккаунт заблокирован</h2>
                <div class="highlight danger">
                    <p>Ваш аккаунт был заблокирован.</p>
                    {f'<p>Причина: {reason}</p>' if reason else ''}
                </div>
                <p>Если вы считаете, что это ошибка, обратитесь в поддержку.</p>
            """,
            'en': f"""
                <h2>Account Suspended</h2>
                <div class="highlight danger">
                    <p>Your account has been suspended.</p>
                    {f'<p>Reason: {reason}</p>' if reason else ''}
                </div>
                <p>If you believe this is an error, please contact support.</p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _unban_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for unban notification."""
        subjects = {
            'ru': 'Аккаунт разблокирован',
            'en': 'Account Reactivated',
            'zh': '账户已解封',
            'ua': 'Обліковий запис розблоковано',
        }

        bodies = {
            'ru': f"""
                <h2>Аккаунт разблокирован</h2>
                <div class="highlight success">
                    <p>Ваш аккаунт был разблокирован.</p>
                    <p>Вы снова можете пользоваться сервисом.</p>
                </div>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Account Reactivated</h2>
                <div class="highlight success">
                    <p>Your account has been reactivated.</p>
                    <p>You can use the service again.</p>
                </div>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _warning_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for warning notification."""
        message = html.escape(context.get('message', ''))

        subjects = {
            'ru': 'Предупреждение',
            'en': 'Warning',
            'zh': '警告',
            'ua': 'Попередження',
        }

        bodies = {
            'ru': f"""
                <h2>Предупреждение</h2>
                <div class="highlight warning">
                    {f'<p>{message}</p>' if message else '<p>Вы получили предупреждение от администрации.</p>'}
                </div>
            """,
            'en': f"""
                <h2>Warning</h2>
                <div class="highlight warning">
                    {f'<p>{message}</p>' if message else '<p>You have received a warning from the administration.</p>'}
                </div>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Referral Templates
    # ============================================================================

    def _referral_bonus_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for referral bonus notification.

        Награда может быть выдана днями подписки, а не деньгами. В копейках такая
        награда честно нулевая, поэтому текст строится по ``formatted_reward`` —
        единственному полю, верному и для денег, и для дней, и для их сочетания.
        Без него письмо о выданных семи днях уходит как «Реферальный бонус: +0.00 ₽».
        """
        bonus = context.get('formatted_reward') or context.get(
            'formatted_bonus', f'{context.get("bonus_rubles", 0):.2f} ₽'
        )
        referral_name = html.escape(context.get('referral_name', ''))
        raw_level = context.get('level', 1)
        try:
            show_level = int(raw_level or 1) > 1
        except (TypeError, ValueError):
            # Редактор шаблонов рендерит их с токенами вида '{level}'. Уровень там
            # неизвестен, но блок обязан попасть в payload — иначе админ его просто
            # не увидит и не сможет отредактировать.
            show_level = True
        level_label = html.escape(str(raw_level))
        level_note_ru = f'<p>Уровень вашей сети: {level_label}</p>' if show_level else ''
        level_note_en = f'<p>Network level: {level_label}</p>' if show_level else ''

        subjects = {
            'ru': f'Реферальный бонус: +{bonus}',
            'en': f'Referral bonus: +{bonus}',
            'zh': f'推荐奖励: +{bonus}',
            'ua': f'Реферальний бонус: +{bonus}',
        }

        bodies = {
            'ru': f"""
                <h2>Реферальный бонус!</h2>
                <div class="highlight success">
                    <p>Вы получили реферальный бонус: <span class="amount">+{bonus}</span></p>
                    {f'<p>Благодаря пользователю: {referral_name}</p>' if referral_name else ''}
                    {level_note_ru}
                </div>
                <p>Продолжайте приглашать друзей и зарабатывайте больше!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Referral Bonus!</h2>
                <div class="highlight success">
                    <p>You received a referral bonus: <span class="amount">+{bonus}</span></p>
                    {f'<p>Thanks to: {referral_name}</p>' if referral_name else ''}
                    {level_note_en}
                </div>
                <p>Keep inviting friends and earn more!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _promo_offer_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for personal promo offer notification.

        ``message_html`` — уже отрендеренный текст предложения (Telegram-HTML:
        b/i/code — валидный HTML-фрагмент, переносы строк заменены на <br>).
        Пишется админом или собирается дефолтным билдером — доверенный контент,
        не экранируем.
        """
        message_html = context.get('message_html', '')
        valid_hours = int(context.get('valid_hours', 0) or 0)
        discount_percent = int(context.get('discount_percent', 0) or 0)

        if discount_percent > 0:
            subjects = {
                'ru': f'🎁 Персональное предложение: скидка {discount_percent}%',
                'en': f'🎁 Personal offer: {discount_percent}% off',
                'zh': f'🎁 专属优惠：{discount_percent}% 折扣',
                'ua': f'🎁 Персональна пропозиція: знижка {discount_percent}%',
            }
        else:
            subjects = {
                'ru': '🎁 Персональное предложение',
                'en': '🎁 Personal offer',
                'zh': '🎁 专属优惠',
                'ua': '🎁 Персональна пропозиція',
            }

        valid_lines = {
            'ru': f'<p>⏰ Предложение действует <b>{valid_hours} ч.</b></p>' if valid_hours else '',
            'en': f'<p>⏰ The offer is valid for <b>{valid_hours} h.</b></p>' if valid_hours else '',
            'zh': f'<p>⏰ 优惠有效期 <b>{valid_hours} 小时</b></p>' if valid_hours else '',
            'ua': f'<p>⏰ Пропозиція діє <b>{valid_hours} год.</b></p>' if valid_hours else '',
        }

        bodies = {
            'ru': f"""
                <h2>Персональное предложение</h2>
                <div class="highlight">
                    <p>{message_html}</p>
                </div>
                {valid_lines['ru']}
                <p>Активировать предложение можно в личном кабинете.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Personal Offer</h2>
                <div class="highlight">
                    <p>{message_html}</p>
                </div>
                {valid_lines['en']}
                <p>You can activate the offer in your account.</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>专属优惠</h2>
                <div class="highlight">
                    <p>{message_html}</p>
                </div>
                {valid_lines['zh']}
                <p>您可以在个人中心激活该优惠。</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Персональна пропозиція</h2>
                <div class="highlight">
                    <p>{message_html}</p>
                </div>
                {valid_lines['ua']}
                <p>Активувати пропозицію можна в особистому кабінеті.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(
                bodies.get(language, bodies['ru']), language, context.get('unsubscribe_url', '')
            ),
        }

    def _referral_registered_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for new referral registered notification."""
        referral_name = html.escape(context.get('referral_name', ''))

        subjects = {
            'ru': 'Новый реферал зарегистрирован',
            'en': 'New Referral Registered',
            'zh': '新推荐用户已注册',
            'ua': 'Новий реферал зареєстрований',
        }

        bodies = {
            'ru': f"""
                <h2>Новый реферал!</h2>
                <div class="highlight success">
                    <p>По вашей ссылке зарегистрировался новый пользователь{f': <strong>{referral_name}</strong>' if referral_name else ''}.</p>
                </div>
                <p>Вы будете получать бонусы с его пополнений!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>New Referral!</h2>
                <div class="highlight success">
                    <p>A new user registered using your link{f': <strong>{referral_name}</strong>' if referral_name else ''}.</p>
                </div>
                <p>You will receive bonuses from their top-ups!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _referral_welcome_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for the newcomer who registered by a referral link.

        Приглашённому обещают не выплату, а условие («7 дн. подписки», «100 ₽ при
        первом пополнении от 500 ₽») — оно приходит готовой фразой в
        ``bonus_promise``; пустая фраза — блок с бонусом не показывается.
        """
        referrer_name = html.escape(context.get('referrer_name', '') or '')
        bonus_promise = html.escape(context.get('bonus_promise', '') or '')
        by_whom_ru = f' пользователя <strong>{referrer_name}</strong>' if referrer_name else ''
        by_whom_en = f' of <strong>{referrer_name}</strong>' if referrer_name else ''
        promise_ru = f'<p>Ваш бонус: <span class="amount">{bonus_promise}</span></p>' if bonus_promise else ''
        promise_en = f'<p>Your bonus: <span class="amount">{bonus_promise}</span></p>' if bonus_promise else ''

        subjects = {
            'ru': 'Добро пожаловать по приглашению',
            'en': 'Welcome by invitation',
            'zh': '欢迎通过邀请加入',
            'ua': 'Ласкаво просимо за запрошенням',
        }

        bodies = {
            'ru': f"""
                <h2>Добро пожаловать!</h2>
                <div class="highlight success">
                    <p>Вы зарегистрировались по реферальной ссылке{by_whom_ru}.</p>
                    {promise_ru}
                </div>
                <p>Рады видеть вас среди наших пользователей!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Welcome!</h2>
                <div class="highlight success">
                    <p>You signed up using the referral link{by_whom_en}.</p>
                    {promise_en}
                </div>
                <p>Glad to have you with us!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Partner Templates
    # ============================================================================

    def _partner_approved_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for partner application approved notification."""
        commission = context.get('commission_percent', 0)
        comment = html.escape(context.get('comment', ''))

        subjects = {
            'ru': 'Заявка на партнёрство одобрена',
            'en': 'Partner Application Approved',
            'zh': '合作伙伴申请已批准',
            'ua': 'Заявка на партнерство схвалена',
        }

        bodies = {
            'ru': f"""
                <h2>Заявка на партнёрство одобрена!</h2>
                <div class="highlight success">
                    <p>Ваша заявка на партнёрство была одобрена.</p>
                    <p>Ваша комиссия: <strong>{commission}%</strong></p>
                    {f'<p>Комментарий: {comment}</p>' if comment else ''}
                </div>
                <p>Теперь вы можете приглашать пользователей и получать вознаграждение!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Partner Application Approved!</h2>
                <div class="highlight success">
                    <p>Your partner application has been approved.</p>
                    <p>Your commission rate: <strong>{commission}%</strong></p>
                    {f'<p>Comment: {comment}</p>' if comment else ''}
                </div>
                <p>You can now invite users and earn rewards!</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>合作伙伴申请已批准！</h2>
                <div class="highlight success">
                    <p>您的合作伙伴申请已获批准。</p>
                    <p>您的佣金比例: <strong>{commission}%</strong></p>
                    {f'<p>备注: {comment}</p>' if comment else ''}
                </div>
                <p>您现在可以邀请用户并获得奖励！</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Заявка на партнерство схвалена!</h2>
                <div class="highlight success">
                    <p>Вашу заявку на партнерство було схвалено.</p>
                    <p>Ваша комісія: <strong>{commission}%</strong></p>
                    {f'<p>Коментар: {comment}</p>' if comment else ''}
                </div>
                <p>Тепер ви можете запрошувати користувачів та отримувати винагороду!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _partner_rejected_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for partner application rejected notification."""
        comment = html.escape(context.get('comment', ''))

        subjects = {
            'ru': 'Заявка на партнёрство отклонена',
            'en': 'Partner Application Rejected',
            'zh': '合作伙伴申请被拒绝',
            'ua': 'Заявка на партнерство відхилена',
        }

        bodies = {
            'ru': f"""
                <h2>Заявка на партнёрство отклонена</h2>
                <div class="highlight danger">
                    <p>К сожалению, ваша заявка на партнёрство была отклонена.</p>
                    {f'<p>Причина: {comment}</p>' if comment else ''}
                </div>
                <p>Вы можете подать новую заявку позже.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Partner Application Rejected</h2>
                <div class="highlight danger">
                    <p>Unfortunately, your partner application has been rejected.</p>
                    {f'<p>Reason: {comment}</p>' if comment else ''}
                </div>
                <p>You can submit a new application later.</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>合作伙伴申请被拒绝</h2>
                <div class="highlight danger">
                    <p>很抱歉，您的合作伙伴申请已被拒绝。</p>
                    {f'<p>原因: {comment}</p>' if comment else ''}
                </div>
                <p>您可以稍后提交新的申请。</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Заявка на партнерство відхилена</h2>
                <div class="highlight danger">
                    <p>На жаль, вашу заявку на партнерство було відхилено.</p>
                    {f'<p>Причина: {comment}</p>' if comment else ''}
                </div>
                <p>Ви можете подати нову заявку пізніше.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Withdrawal Templates
    # ============================================================================

    def _withdrawal_approved_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for withdrawal approved notification."""
        amount = context.get('formatted_amount', f'{context.get("amount_rubles", 0):.2f} ₽')
        comment = html.escape(context.get('comment', ''))

        subjects = {
            'ru': f'Запрос на вывод {amount} одобрен',
            'en': f'Withdrawal request for {amount} approved',
            'zh': f'提现请求 {amount} 已批准',
            'ua': f'Запит на виведення {amount} схвалено',
        }

        bodies = {
            'ru': f"""
                <h2>Запрос на вывод одобрен!</h2>
                <div class="highlight success">
                    <p>Ваш запрос на вывод средств одобрен.</p>
                    <p>Сумма: <span class="amount">{amount}</span></p>
                    {f'<p>Комментарий: {comment}</p>' if comment else ''}
                </div>
                <p>Средства будут переведены в ближайшее время.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Withdrawal Request Approved!</h2>
                <div class="highlight success">
                    <p>Your withdrawal request has been approved.</p>
                    <p>Amount: <span class="amount">{amount}</span></p>
                    {f'<p>Comment: {comment}</p>' if comment else ''}
                </div>
                <p>Funds will be transferred shortly.</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>提现请求已批准！</h2>
                <div class="highlight success">
                    <p>您的提现请求已获批准。</p>
                    <p>金额: <span class="amount">{amount}</span></p>
                    {f'<p>备注: {comment}</p>' if comment else ''}
                </div>
                <p>资金将很快转入。</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Запит на виведення схвалено!</h2>
                <div class="highlight success">
                    <p>Ваш запит на виведення коштів було схвалено.</p>
                    <p>Сума: <span class="amount">{amount}</span></p>
                    {f'<p>Коментар: {comment}</p>' if comment else ''}
                </div>
                <p>Кошти будуть переведені найближчим часом.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _withdrawal_rejected_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for withdrawal rejected notification."""
        amount = context.get('formatted_amount', f'{context.get("amount_rubles", 0):.2f} ₽')
        comment = html.escape(context.get('comment', ''))

        subjects = {
            'ru': f'Запрос на вывод {amount} отклонён',
            'en': f'Withdrawal request for {amount} rejected',
            'zh': f'提现请求 {amount} 被拒绝',
            'ua': f'Запит на виведення {amount} відхилено',
        }

        bodies = {
            'ru': f"""
                <h2>Запрос на вывод отклонён</h2>
                <div class="highlight danger">
                    <p>Ваш запрос на вывод средств был отклонён.</p>
                    <p>Сумма: <strong>{amount}</strong></p>
                    {f'<p>Причина: {comment}</p>' if comment else ''}
                </div>
                <p>Средства возвращены на ваш баланс.</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Withdrawal Request Rejected</h2>
                <div class="highlight danger">
                    <p>Your withdrawal request has been rejected.</p>
                    <p>Amount: <strong>{amount}</strong></p>
                    {f'<p>Reason: {comment}</p>' if comment else ''}
                </div>
                <p>Funds have been returned to your balance.</p>
                {self._get_cabinet_button(language)}
            """,
            'zh': f"""
                <h2>提现请求被拒绝</h2>
                <div class="highlight danger">
                    <p>您的提现请求已被拒绝。</p>
                    <p>金额: <strong>{amount}</strong></p>
                    {f'<p>原因: {comment}</p>' if comment else ''}
                </div>
                <p>资金已退回您的余额。</p>
                {self._get_cabinet_button(language)}
            """,
            'ua': f"""
                <h2>Запит на виведення відхилено</h2>
                <div class="highlight danger">
                    <p>Ваш запит на виведення коштів було відхилено.</p>
                    <p>Сума: <strong>{amount}</strong></p>
                    {f'<p>Причина: {comment}</p>' if comment else ''}
                </div>
                <p>Кошти повернуто на ваш баланс.</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Payment Templates
    # ============================================================================

    def _payment_received_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for payment received notification."""
        amount = context.get('formatted_amount', f'{context.get("amount_rubles", 0):.2f} ₽')
        payment_method = context.get('payment_method', '')

        subjects = {
            'ru': f'Платёж получен: {amount}',
            'en': f'Payment received: {amount}',
            'zh': f'收到付款: {amount}',
            'ua': f'Платіж отримано: {amount}',
        }

        bodies = {
            'ru': f"""
                <h2>Платёж успешно обработан</h2>
                <div class="highlight success">
                    <p>Сумма: <span class="amount">+{amount}</span></p>
                    {f'<p>Способ оплаты: {payment_method}</p>' if payment_method else ''}
                </div>
                <p>Спасибо за оплату!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Payment Successfully Processed</h2>
                <div class="highlight success">
                    <p>Amount: <span class="amount">+{amount}</span></p>
                    {f'<p>Payment method: {payment_method}</p>' if payment_method else ''}
                </div>
                <p>Thank you for your payment!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Support Ticket Templates
    # ============================================================================

    def _get_support_button(self, language: str) -> str:
        """Get support section link button HTML."""
        if not self.cabinet_url:
            return ''

        texts = {
            'ru': 'Открыть тикет',
            'en': 'Open ticket',
            'zh': '打开工单',
            'ua': 'Відкрити тікет',
            'fa': 'باز کردن تیکت',
        }
        text = texts.get(language, texts['en'])
        url = f'{self.cabinet_url.rstrip("/")}/support'

        return f'<p style="text-align: center;"><a href="{url}" class="button">{text}</a></p>'

    def _ticket_reply_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for a support reply in a ticket."""
        ticket_id = context.get('ticket_id', '')
        # Экранируем ПОСЛЕ обрезки превью на стороне вызывающего кода: ответ
        # поддержки вполне может содержать угловые скобки («откройте <config>»).
        preview = html.escape(str(context.get('reply_preview', '') or '')).replace('\n', '<br>')
        has_photo = bool(context.get('has_photo'))

        subjects = {
            'ru': f'Ответ по тикету #{ticket_id}',
            'en': f'Reply to ticket #{ticket_id}',
            'zh': f'工单 #{ticket_id} 的回复',
            'ua': f'Відповідь по тікету #{ticket_id}',
        }

        photo_notes = {
            'ru': '<p>К ответу приложено изображение — оно доступно в кабинете.</p>',
            'en': '<p>The reply includes an image — it is available in your dashboard.</p>',
        }
        photo_note = photo_notes.get(language, photo_notes['ru']) if has_photo else ''

        bodies = {
            'ru': f"""
                <h2>Ответ поддержки по тикету #{ticket_id}</h2>
                <div class="highlight">
                    {f'<p>{preview}</p>' if preview else '<p>Поддержка ответила на ваше обращение.</p>'}
                </div>
                {photo_note}
                <p>Ответить можно в личном кабинете.</p>
                {self._get_support_button(language)}
            """,
            'en': f"""
                <h2>Support replied to ticket #{ticket_id}</h2>
                <div class="highlight">
                    {f'<p>{preview}</p>' if preview else '<p>Support has replied to your request.</p>'}
                </div>
                {photo_note}
                <p>You can reply from your dashboard.</p>
                {self._get_support_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Auth Email Templates
    # ============================================================================

    def _email_verification_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for email verification."""
        username = html.escape(context.get('username', ''))
        verification_url = html.escape(context.get('verification_url', '#'))
        expire_hours = context.get('expire_hours', 24)

        subjects = {
            'ru': 'Подтверждение email адреса',
            'en': 'Verify your email address',
            'zh': '验证您的邮箱地址',
            'ua': 'Підтвердження email адреси',
            'fa': 'تایید آدرس ایمیل',
        }

        greeting = {
            'ru': f'Здравствуйте{", " + username if username else ""}!',
            'en': f'Hello{", " + username if username else ""}!',
            'zh': f'您好{", " + username if username else ""}!',
            'ua': f'Вітаємо{", " + username if username else ""}!',
            'fa': f'سلام{" " + username if username else ""}!',
        }

        bodies = {
            'ru': f"""
                <h2>{greeting.get('ru')}</h2>
                <p>Спасибо за регистрацию! Пожалуйста, подтвердите ваш email адрес, нажав на кнопку ниже:</p>
                <p style="text-align: center;">
                    <a href="{verification_url}" class="button">Подтвердить email</a>
                </p>
                <p>Или скопируйте и вставьте эту ссылку в браузер:</p>
                <p><a href="{verification_url}">{verification_url}</a></p>
                <p>Ссылка действительна в течение {expire_hours} часов.</p>
                <p style="color: #666;">Если вы не создавали аккаунт, просто проигнорируйте это письмо.</p>
            """,
            'fa': f"""
                <h2>{greeting.get('fa')}</h2>
                <p>از ثبت‌نام شما سپاسگزاریم! لطفاً با کلیک روی دکمه زیر ایمیل خود را تایید کنید:</p>
                <p style="text-align: center;">
                    <a href="{verification_url}" class="button">تایید ایمیل</a>
                </p>
                <p>یا این لینک را در مرورگر خود کپی و باز کنید:</p>
                <p><a href="{verification_url}">{verification_url}</a></p>
                <p>این لینک تا {expire_hours} ساعت معتبر است.</p>
                <p style="color: #666;">اگر شما این حساب را ایجاد نکرده‌اید، این ایمیل را نادیده بگیرید.</p>
            """,
            'en': f"""
                <h2>{greeting.get('en')}</h2>
                <p>Thank you for registering! Please verify your email address by clicking the button below:</p>
                <p style="text-align: center;">
                    <a href="{verification_url}" class="button">Verify Email</a>
                </p>
                <p>Or copy and paste this link in your browser:</p>
                <p><a href="{verification_url}">{verification_url}</a></p>
                <p>This link will expire in {expire_hours} hours.</p>
                <p style="color: #666;">If you didn't create an account, you can safely ignore this email.</p>
            """,
            'zh': f"""
                <h2>{greeting.get('zh')}</h2>
                <p>感谢您的注册！请点击下方按钮验证您的邮箱地址：</p>
                <p style="text-align: center;">
                    <a href="{verification_url}" class="button">验证邮箱</a>
                </p>
                <p>或将此链接复制并粘贴到浏览器中：</p>
                <p><a href="{verification_url}">{verification_url}</a></p>
                <p>此链接将在 {expire_hours} 小时后过期。</p>
                <p style="color: #666;">如果您没有创建账户，请忽略此邮件。</p>
            """,
            'ua': f"""
                <h2>{greeting.get('ua')}</h2>
                <p>Дякуємо за реєстрацію! Будь ласка, підтвердіть вашу email адресу, натиснувши на кнопку нижче:</p>
                <p style="text-align: center;">
                    <a href="{verification_url}" class="button">Підтвердити email</a>
                </p>
                <p>Або скопіюйте та вставте це посилання в браузер:</p>
                <p><a href="{verification_url}">{verification_url}</a></p>
                <p>Посилання дійсне протягом {expire_hours} годин.</p>
                <p style="color: #666;">Якщо ви не створювали акаунт, просто проігноруйте цей лист.</p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _password_reset_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for password reset."""
        username = html.escape(context.get('username', ''))
        reset_url = html.escape(context.get('reset_url', '#'))
        expire_hours = context.get('expire_hours', 1)

        subjects = {
            'ru': 'Сброс пароля',
            'en': 'Reset your password',
            'zh': '重置您的密码',
            'ua': 'Скидання пароля',
            'fa': 'بازنشانی رمز عبور',
        }

        greeting = {
            'ru': f'Здравствуйте{", " + username if username else ""}!',
            'en': f'Hello{", " + username if username else ""}!',
            'zh': f'您好{", " + username if username else ""}!',
            'ua': f'Вітаємо{", " + username if username else ""}!',
            'fa': f'سلام{" " + username if username else ""}!',
        }

        bodies = {
            'fa': f"""
                <h2>{greeting.get('fa')}</h2>
                <p>درخواستی برای بازنشانی رمز عبور شما دریافت شد. برای تعیین رمز جدید روی دکمه زیر بزنید:</p>
                <p style="text-align: center;">
                    <a href="{reset_url}" class="button" style="background-color: #dc3545;">بازنشانی رمز عبور</a>
                </p>
                <p>یا این لینک را در مرورگر خود کپی و باز کنید:</p>
                <p><a href="{reset_url}">{reset_url}</a></p>
                <p>این لینک تا {expire_hours} ساعت معتبر است.</p>
                <p class="warning" style="color: #dc3545; font-weight: bold;">اگر شما درخواست بازنشانی رمز عبور نداده‌اید، این ایمیل را نادیده بگیرید یا با پشتیبانی تماس بگیرید.</p>
            """,
            'ru': f"""
                <h2>{greeting.get('ru')}</h2>
                <p>Мы получили запрос на сброс вашего пароля. Нажмите на кнопку ниже, чтобы установить новый пароль:</p>
                <p style="text-align: center;">
                    <a href="{reset_url}" class="button" style="background-color: #dc3545;">Сбросить пароль</a>
                </p>
                <p>Или скопируйте и вставьте эту ссылку в браузер:</p>
                <p><a href="{reset_url}">{reset_url}</a></p>
                <p>Ссылка действительна в течение {expire_hours} часов.</p>
                <p class="warning" style="color: #dc3545; font-weight: bold;">Если вы не запрашивали сброс пароля, проигнорируйте это письмо или свяжитесь с поддержкой.</p>
            """,
            'en': f"""
                <h2>{greeting.get('en')}</h2>
                <p>We received a request to reset your password. Click the button below to set a new password:</p>
                <p style="text-align: center;">
                    <a href="{reset_url}" class="button" style="background-color: #dc3545;">Reset Password</a>
                </p>
                <p>Or copy and paste this link in your browser:</p>
                <p><a href="{reset_url}">{reset_url}</a></p>
                <p>This link will expire in {expire_hours} hour(s).</p>
                <p class="warning" style="color: #dc3545; font-weight: bold;">If you didn't request a password reset, please ignore this email or contact support.</p>
            """,
            'zh': f"""
                <h2>{greeting.get('zh')}</h2>
                <p>我们收到了重置您密码的请求。点击下方按钮设置新密码：</p>
                <p style="text-align: center;">
                    <a href="{reset_url}" class="button" style="background-color: #dc3545;">重置密码</a>
                </p>
                <p>或将此链接复制并粘贴到浏览器中：</p>
                <p><a href="{reset_url}">{reset_url}</a></p>
                <p>此链接将在 {expire_hours} 小时后过期。</p>
                <p class="warning" style="color: #dc3545; font-weight: bold;">如果您没有请求重置密码，请忽略此邮件或联系客服。</p>
            """,
            'ua': f"""
                <h2>{greeting.get('ua')}</h2>
                <p>Ми отримали запит на скидання вашого пароля. Натисніть на кнопку нижче, щоб встановити новий пароль:</p>
                <p style="text-align: center;">
                    <a href="{reset_url}" class="button" style="background-color: #dc3545;">Скинути пароль</a>
                </p>
                <p>Або скопіюйте та вставте це посилання в браузер:</p>
                <p><a href="{reset_url}">{reset_url}</a></p>
                <p>Посилання дійсне протягом {expire_hours} годин.</p>
                <p class="warning" style="color: #dc3545; font-weight: bold;">Якщо ви не запитували скидання пароля, проігноруйте цей лист або зв'яжіться з підтримкою.</p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _email_change_code_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for email change verification code."""
        username = html.escape(context.get('username', ''))
        code = html.escape(str(context.get('code', '')))
        expire_minutes = context.get('expire_minutes', 10)

        subjects = {
            'ru': 'Код подтверждения для смены email',
            'en': 'Email change verification code',
            'zh': '邮箱更换验证码',
            'ua': 'Код підтвердження для зміни email',
            'fa': 'کد تایید تغییر ایمیل',
        }

        greeting = {
            'ru': f'Здравствуйте{", " + username if username else ""}!',
            'en': f'Hello{", " + username if username else ""}!',
            'zh': f'您好{", " + username if username else ""}!',
            'ua': f'Вітаємо{", " + username if username else ""}!',
            'fa': f'سلام{" " + username if username else ""}!',
        }

        code_box = f"""
                <div class="highlight" style="text-align: center;">
                    <p style="font-size: 32px; font-weight: bold; letter-spacing: 8px; font-family: monospace; margin: 10px 0;">{code}</p>
                </div>
        """

        bodies = {
            'ru': f"""
                <h2>{greeting.get('ru')}</h2>
                <p>Вы запросили смену email адреса. Используйте код ниже для подтверждения:</p>
                {code_box}
                <p>Код действителен в течение {expire_minutes} минут.</p>
                <p style="color: #666;">Если вы не запрашивали смену email, просто проигнорируйте это письмо.</p>
            """,
            'en': f"""
                <h2>{greeting.get('en')}</h2>
                <p>You requested to change your email address. Use the code below to confirm:</p>
                {code_box}
                <p>This code will expire in {expire_minutes} minutes.</p>
                <p style="color: #666;">If you didn't request an email change, you can safely ignore this email.</p>
            """,
            'zh': f"""
                <h2>{greeting.get('zh')}</h2>
                <p>您请求更换邮箱地址。请使用以下验证码确认：</p>
                {code_box}
                <p>此验证码将在 {expire_minutes} 分钟后过期。</p>
                <p style="color: #666;">如果您没有请求更换邮箱，请忽略此邮件。</p>
            """,
            'ua': f"""
                <h2>{greeting.get('ua')}</h2>
                <p>Ви запросили зміну email адреси. Використовуйте код нижче для підтвердження:</p>
                {code_box}
                <p>Код дійсний протягом {expire_minutes} хвилин.</p>
                <p style="color: #666;">Якщо ви не запитували зміну email, просто проігноруйте цей лист.</p>
            """,
            'fa': f"""
                <h2>{greeting.get('fa')}</h2>
                <p>شما درخواست تغییر ایمیل داده‌اید. برای تایید از کد زیر استفاده کنید:</p>
                {code_box}
                <p>این کد تا {expire_minutes} دقیقه معتبر است.</p>
                <p style="color: #666;">اگر شما درخواست تغییر ایمیل نداده‌اید، این ایمیل را نادیده بگیرید.</p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    # ============================================================================
    # Guest Purchase Templates
    # ============================================================================

    def _guest_subscription_delivered_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for guest subscription delivered notification."""
        tariff_name = html.escape(context.get('tariff_name', ''))
        period_days = context.get('period_days', 0)
        cabinet_url = html.escape(context.get('cabinet_url', ''))
        cabinet_email = html.escape(context.get('cabinet_email', ''))
        cabinet_password = context.get('cabinet_password', '')

        subjects = {
            'ru': 'Ваша VPN подписка готова',
            'en': 'Your VPN subscription is ready',
            'zh': '您的VPN订阅已准备就绪',
            'ua': 'Ваша VPN підписка готова',
            'fa': 'اشتراک VPN شما آماده است',
        }

        creds_block_ru = (
            f"""
                <div class="highlight">
                    <p><strong>Данные для входа в личный кабинет:</strong></p>
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>Пароль:</strong> <code>{cabinet_password}</code></p>
                </div>
        """
            if cabinet_password
            else ''
        )

        creds_block_en = (
            f"""
                <div class="highlight">
                    <p><strong>Your cabinet login credentials:</strong></p>
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>Password:</strong> <code>{cabinet_password}</code></p>
                </div>
        """
            if cabinet_password
            else ''
        )

        creds_block_zh = (
            f"""
                <div class="highlight">
                    <p><strong>个人中心登录信息：</strong></p>
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>密码:</strong> <code>{cabinet_password}</code></p>
                </div>
        """
            if cabinet_password
            else ''
        )

        creds_block_ua = (
            f"""
                <div class="highlight">
                    <p><strong>Дані для входу в особистий кабінет:</strong></p>
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>Пароль:</strong> <code>{cabinet_password}</code></p>
                </div>
        """
            if cabinet_password
            else ''
        )

        creds_block_fa = (
            f"""
                <div class="highlight">
                    <p><strong>اطلاعات ورود به پنل کاربری:</strong></p>
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>رمز عبور:</strong> <code>{cabinet_password}</code></p>
                </div>
        """
            if cabinet_password
            else ''
        )

        bodies = {
            'ru': f"""
                <h2>Ваша VPN подписка готова!</h2>
                <div class="highlight success">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Период: <strong>{period_days} дней</strong></p>
                </div>
                {creds_block_ru}
                <p>Подписка активирована в вашем личном кабинете.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти в личный кабинет</a></p>
            """,
            'en': f"""
                <h2>Your VPN subscription is ready!</h2>
                <div class="highlight success">
                    <p>Plan: <strong>{tariff_name}</strong></p>
                    <p>Period: <strong>{period_days} days</strong></p>
                </div>
                {creds_block_en}
                <p>Your subscription has been activated in your cabinet.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Go to Cabinet</a></p>
            """,
            'zh': f"""
                <h2>您的VPN订阅已准备就绪！</h2>
                <div class="highlight success">
                    <p>套餐: <strong>{tariff_name}</strong></p>
                    <p>期限: <strong>{period_days} 天</strong></p>
                </div>
                {creds_block_zh}
                <p>订阅已在您的个人中心激活。</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">前往个人中心</a></p>
            """,
            'ua': f"""
                <h2>Ваша VPN підписка готова!</h2>
                <div class="highlight success">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Період: <strong>{period_days} днів</strong></p>
                </div>
                {creds_block_ua}
                <p>Підписка активована у вашому особистому кабінеті.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти до кабінету</a></p>
            """,
            'fa': f"""
                <h2>اشتراک VPN شما آماده است!</h2>
                <div class="highlight success">
                    <p>طرح: <strong>{tariff_name}</strong></p>
                    <p>مدت: <strong>{period_days} روز</strong></p>
                </div>
                {creds_block_fa}
                <p>اشتراک شما در پنل کاربری فعال شده است.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">رفتن به پنل کاربری</a></p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _guest_activation_required_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for guest purchase pending activation (user already has a subscription)."""
        tariff_name = html.escape(context.get('tariff_name', ''))
        period_days = context.get('period_days', 0)
        success_page_url = html.escape(context.get('success_page_url', ''))
        gift_message = context.get('gift_message')
        is_gift = context.get('is_gift', False)

        gift_block_ru = ''
        gift_block_en = ''
        gift_block_zh = ''
        gift_block_ua = ''
        gift_block_fa = ''
        if is_gift and gift_message:
            escaped_msg = html.escape(gift_message)
            gift_block_ru = f'<div class="highlight"><p><em>Сообщение: {escaped_msg}</em></p></div>'
            gift_block_en = f'<div class="highlight"><p><em>Message: {escaped_msg}</em></p></div>'
            gift_block_zh = f'<div class="highlight"><p><em>留言: {escaped_msg}</em></p></div>'
            gift_block_ua = f'<div class="highlight"><p><em>Повідомлення: {escaped_msg}</em></p></div>'
            gift_block_fa = f'<div class="highlight"><p><em>پیام: {escaped_msg}</em></p></div>'

        subjects = {
            'ru': 'Требуется активация подписки',
            'en': 'Subscription activation required',
            'zh': '需要激活订阅',
            'ua': 'Потрібна активація підписки',
            'fa': 'فعال‌سازی اشتراک لازم است',
        }

        bodies = {
            'ru': f"""
                <h2>Требуется активация подписки</h2>
                {gift_block_ru}
                <div class="highlight">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Период: <strong>{period_days} дней</strong></p>
                </div>
                <p class="warning">У вас уже есть активная подписка. Активация новой заменит текущую.</p>
                <p style="text-align: center;"><a href="{success_page_url}" class="button">Активировать подписку</a></p>
            """,
            'en': f"""
                <h2>Subscription activation required</h2>
                {gift_block_en}
                <div class="highlight">
                    <p>Plan: <strong>{tariff_name}</strong></p>
                    <p>Period: <strong>{period_days} days</strong></p>
                </div>
                <p class="warning">You already have an active subscription. Activating will replace your current one.</p>
                <p style="text-align: center;"><a href="{success_page_url}" class="button">Activate subscription</a></p>
            """,
            'zh': f"""
                <h2>需要激活订阅</h2>
                {gift_block_zh}
                <div class="highlight">
                    <p>套餐: <strong>{tariff_name}</strong></p>
                    <p>期限: <strong>{period_days} 天</strong></p>
                </div>
                <p class="warning">您已有活跃订阅。激活新订阅将替换当前订阅。</p>
                <p style="text-align: center;"><a href="{success_page_url}" class="button">激活订阅</a></p>
            """,
            'ua': f"""
                <h2>Потрібна активація підписки</h2>
                {gift_block_ua}
                <div class="highlight">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Період: <strong>{period_days} днів</strong></p>
                </div>
                <p class="warning">У вас вже є активна підписка. Активація нової замінить поточну.</p>
                <p style="text-align: center;"><a href="{success_page_url}" class="button">Активувати підписку</a></p>
            """,
            'fa': f"""
                <h2>فعال‌سازی اشتراک لازم است</h2>
                {gift_block_fa}
                <div class="highlight">
                    <p>طرح: <strong>{tariff_name}</strong></p>
                    <p>مدت: <strong>{period_days} روز</strong></p>
                </div>
                <p class="warning">شما از قبل اشتراک فعالی دارید. فعال‌سازی اشتراک جدید جایگزین فعلی خواهد شد.</p>
                <p style="text-align: center;"><a href="{success_page_url}" class="button">فعال‌سازی اشتراک</a></p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _guest_gift_received_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for gift subscription received notification."""
        tariff_name = html.escape(context.get('tariff_name', ''))
        period_days = context.get('period_days', 0)
        gift_message = context.get('gift_message')
        cabinet_password = context.get('cabinet_password')
        cabinet_email = html.escape(context.get('cabinet_email', ''))
        cabinet_url = html.escape(context.get('cabinet_url', ''))

        # Credentials block for gift recipients who got a new cabinet account
        cred_block = {'ru': '', 'en': '', 'zh': '', 'ua': '', 'fa': ''}
        if cabinet_password and cabinet_email:
            escaped_pw = html.escape(cabinet_password)
            cred_block = {
                'ru': f"""
                    <div class="highlight">
                        <p><strong>Данные для входа в личный кабинет:</strong></p>
                        <p>Email: <code>{cabinet_email}</code></p>
                        <p>Пароль: <code>{escaped_pw}</code></p>
                    </div>
                    <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти в личный кабинет</a></p>
                """,
                'en': f"""
                    <div class="highlight">
                        <p><strong>Your cabinet login credentials:</strong></p>
                        <p>Email: <code>{cabinet_email}</code></p>
                        <p>Password: <code>{escaped_pw}</code></p>
                    </div>
                    <p style="text-align: center;"><a href="{cabinet_url}" class="button">Go to Cabinet</a></p>
                """,
                'zh': f"""
                    <div class="highlight">
                        <p><strong>个人中心登录信息：</strong></p>
                        <p>邮箱: <code>{cabinet_email}</code></p>
                        <p>密码: <code>{escaped_pw}</code></p>
                    </div>
                    <p style="text-align: center;"><a href="{cabinet_url}" class="button">前往个人中心</a></p>
                """,
                'ua': f"""
                    <div class="highlight">
                        <p><strong>Дані для входу в особистий кабінет:</strong></p>
                        <p>Email: <code>{cabinet_email}</code></p>
                        <p>Пароль: <code>{escaped_pw}</code></p>
                    </div>
                    <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти до кабінету</a></p>
                """,
                'fa': f"""
                    <div class="highlight">
                        <p><strong>اطلاعات ورود به پنل کاربری:</strong></p>
                        <p>ایمیل: <code dir="ltr">{cabinet_email}</code></p>
                        <p>رمز عبور: <code dir="ltr">{escaped_pw}</code></p>
                    </div>
                    <p style="text-align: center;"><a href="{cabinet_url}" class="button">رفتن به پنل کاربری</a></p>
                """,
            }

        gift_block_ru = ''
        gift_block_en = ''
        gift_block_zh = ''
        gift_block_ua = ''
        gift_block_fa = ''
        if gift_message:
            escaped_msg = html.escape(gift_message)
            gift_block_ru = f'<div class="highlight"><p><em>Сообщение: {escaped_msg}</em></p></div>'
            gift_block_en = f'<div class="highlight"><p><em>Message: {escaped_msg}</em></p></div>'
            gift_block_zh = f'<div class="highlight"><p><em>留言: {escaped_msg}</em></p></div>'
            gift_block_ua = f'<div class="highlight"><p><em>Повідомлення: {escaped_msg}</em></p></div>'
            gift_block_fa = f'<div class="highlight"><p><em>پیام: {escaped_msg}</em></p></div>'

        subjects = {
            'ru': 'Вам подарили VPN подписку!',
            'en': "You've been gifted a VPN subscription!",
            'zh': '您收到了VPN订阅礼物！',
            'ua': 'Вам подарували VPN підписку!',
            'fa': 'یک اشتراک VPN به شما هدیه داده شده است!',
        }

        bodies = {
            'ru': f"""
                <h2>Вам подарили VPN подписку!</h2>
                {gift_block_ru}
                <div class="highlight success">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Период: <strong>{period_days} дней</strong></p>
                </div>
                <p>Подписка активирована в личном кабинете.</p>
                {cred_block['ru']}
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти в личный кабинет</a></p>
            """,
            'en': f"""
                <h2>You've been gifted a VPN subscription!</h2>
                {gift_block_en}
                <div class="highlight success">
                    <p>Plan: <strong>{tariff_name}</strong></p>
                    <p>Period: <strong>{period_days} days</strong></p>
                </div>
                <p>Your subscription has been activated in the cabinet.</p>
                {cred_block['en']}
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Go to Cabinet</a></p>
            """,
            'zh': f"""
                <h2>您收到了VPN订阅礼物！</h2>
                {gift_block_zh}
                <div class="highlight success">
                    <p>套餐: <strong>{tariff_name}</strong></p>
                    <p>期限: <strong>{period_days} 天</strong></p>
                </div>
                <p>订阅已在个人中心激活。</p>
                {cred_block['zh']}
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">前往个人中心</a></p>
            """,
            'ua': f"""
                <h2>Вам подарували VPN підписку!</h2>
                {gift_block_ua}
                <div class="highlight success">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Період: <strong>{period_days} днів</strong></p>
                </div>
                <p>Підписка активована в особистому кабінеті.</p>
                {cred_block['ua']}
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти до кабінету</a></p>
            """,
            'fa': f"""
                <h2>یک اشتراک VPN به شما هدیه داده شده است!</h2>
                {gift_block_fa}
                <div class="highlight success">
                    <p>طرح: <strong>{tariff_name}</strong></p>
                    <p>مدت: <strong>{period_days} روز</strong></p>
                </div>
                <p>اشتراک در پنل کاربری فعال شده است.</p>
                {cred_block['fa']}
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">رفتن به پنل کاربری</a></p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }

    def _guest_cabinet_credentials_template(self, language: str, context: dict[str, Any]) -> dict[str, str]:
        """Template for cabinet login credentials email (sent separately from subscription)."""
        cabinet_email = html.escape(context.get('cabinet_email', ''))
        cabinet_password = html.escape(context.get('cabinet_password', ''))
        cabinet_url = html.escape(context.get('cabinet_url', ''))
        tariff_name = html.escape(context.get('tariff_name', ''))
        period_days = context.get('period_days', 0)

        subjects = {
            'ru': 'Данные для входа в личный кабинет',
            'en': 'Your cabinet login credentials',
            'zh': '您的个人中心登录信息',
            'ua': 'Дані для входу в особистий кабінет',
            'fa': 'اطلاعات ورود به پنل کاربری',
        }

        bodies = {
            'ru': f"""
                <h2>Данные для входа в личный кабинет</h2>
                <div class="highlight success">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Период: <strong>{period_days} дней</strong></p>
                </div>
                <div class="highlight">
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>Пароль:</strong> <code>{cabinet_password}</code></p>
                </div>
                <p>Сохраните эти данные для входа. Вы можете изменить пароль в настройках кабинета.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти в личный кабинет</a></p>
            """,
            'en': f"""
                <h2>Your cabinet login credentials</h2>
                <div class="highlight success">
                    <p>Plan: <strong>{tariff_name}</strong></p>
                    <p>Period: <strong>{period_days} days</strong></p>
                </div>
                <div class="highlight">
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>Password:</strong> <code>{cabinet_password}</code></p>
                </div>
                <p>Save these credentials. You can change your password in cabinet settings.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Go to Cabinet</a></p>
            """,
            'zh': f"""
                <h2>您的个人中心登录信息</h2>
                <div class="highlight success">
                    <p>套餐: <strong>{tariff_name}</strong></p>
                    <p>期限: <strong>{period_days} 天</strong></p>
                </div>
                <div class="highlight">
                    <p><strong>邮箱:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>密码:</strong> <code>{cabinet_password}</code></p>
                </div>
                <p>请保存这些登录信息。您可以在个人中心设置中更改密码。</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">前往个人中心</a></p>
            """,
            'ua': f"""
                <h2>Дані для входу в особистий кабінет</h2>
                <div class="highlight success">
                    <p>Тариф: <strong>{tariff_name}</strong></p>
                    <p>Період: <strong>{period_days} днів</strong></p>
                </div>
                <div class="highlight">
                    <p><strong>Email:</strong> <code>{cabinet_email}</code></p>
                    <p><strong>Пароль:</strong> <code>{cabinet_password}</code></p>
                </div>
                <p>Збережіть ці дані. Ви можете змінити пароль у налаштуваннях кабінету.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">Перейти до кабінету</a></p>
            """,
            'fa': f"""
                <h2>اطلاعات ورود به پنل کاربری</h2>
                <div class="highlight success">
                    <p>طرح: <strong>{tariff_name}</strong></p>
                    <p>مدت: <strong>{period_days} روز</strong></p>
                </div>
                <div class="highlight">
                    <p><strong>ایمیل:</strong> <code dir="ltr">{cabinet_email}</code></p>
                    <p><strong>رمز عبور:</strong> <code dir="ltr">{cabinet_password}</code></p>
                </div>
                <p>این اطلاعات را ذخیره کنید. می‌توانید رمز عبور خود را در تنظیمات پنل تغییر دهید.</p>
                <p style="text-align: center;"><a href="{cabinet_url}" class="button">رفتن به پنل کاربری</a></p>
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
        }


# Singleton instance
email_notification_templates = EmailNotificationTemplates()
