"""
Email notification templates for different notification types.

Supports multiple languages: ru, en, zh, ua, fa
"""

import html
from typing import Any

from app.config import settings


class EmailNotificationTemplates:
    """HTML email templates for user notifications."""

    def __init__(self):
        self.service_name = settings.SMTP_FROM_NAME or 'VPN Service'
        self.cabinet_url = getattr(settings, 'CABINET_URL', '')

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
        # Import here to avoid circular imports
        from app.services.notification_delivery_service import NotificationType

        template_map = {
            NotificationType.BALANCE_TOPUP: self._balance_topup_template,
            NotificationType.BALANCE_CHANGE: self._balance_change_template,
            NotificationType.SUBSCRIPTION_EXPIRING: self._subscription_expiring_template,
            NotificationType.SUBSCRIPTION_EXPIRED: self._subscription_expired_template,
            NotificationType.SUBSCRIPTION_RENEWED: self._subscription_renewed_template,
            NotificationType.SUBSCRIPTION_ACTIVATED: self._subscription_activated_template,
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
            NotificationType.PARTNER_APPLICATION_APPROVED: self._partner_approved_template,
            NotificationType.PARTNER_APPLICATION_REJECTED: self._partner_rejected_template,
            NotificationType.WITHDRAWAL_APPROVED: self._withdrawal_approved_template,
            NotificationType.WITHDRAWAL_REJECTED: self._withdrawal_rejected_template,
            NotificationType.TRAFFIC_RESET: self._traffic_reset_template,
            NotificationType.PAYMENT_RECEIVED: self._payment_received_template,
            NotificationType.EMAIL_VERIFICATION: self._email_verification_template,
            NotificationType.PASSWORD_RESET: self._password_reset_template,
            NotificationType.GUEST_SUBSCRIPTION_DELIVERED: self._guest_subscription_delivered_template,
            NotificationType.GUEST_ACTIVATION_REQUIRED: self._guest_activation_required_template,
            NotificationType.GUEST_GIFT_RECEIVED: self._guest_gift_received_template,
            NotificationType.GUEST_CABINET_CREDENTIALS: self._guest_cabinet_credentials_template,
        }

        template_func = template_map.get(notification_type)
        if not template_func:
            return None

        return template_func(language, context)

    def _wrap_override_template(self, content: str, language: str = 'ru') -> str:
        """Wrap override template content appropriately based on its structure.

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
        return self._get_base_template(content, language)

    def _get_base_template(self, content: str, language: str = 'ru') -> str:
        """Wrap content in base HTML template."""
        footer_texts = {
            'ru': 'Это автоматическое сообщение. Пожалуйста, не отвечайте на это письмо.',
            'en': 'This is an automated message. Please do not reply to this email.',
            'zh': '这是一封自动发送的邮件，请勿回复。',
            'ua': 'Це автоматичне повідомлення. Будь ласка, не відповідайте на цей лист.',
            'fa': 'این یک پیام خودکار است. لطفاً به این ایمیل پاسخ ندهید.',
        }
        footer_text = footer_texts.get(language, footer_texts['ru'])

        return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            background-color: #f5f5f5;
            margin: 0;
            padding: 0;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
            background-color: #ffffff;
        }}
        .header {{
            text-align: center;
            padding: 20px 0;
            border-bottom: 2px solid #007bff;
        }}
        .header h1 {{
            color: #007bff;
            margin: 0;
            font-size: 24px;
        }}
        .content {{
            padding: 30px 20px;
        }}
        .highlight {{
            background-color: #f8f9fa;
            border-left: 4px solid #007bff;
            padding: 15px;
            margin: 20px 0;
        }}
        .success {{
            border-left-color: #28a745;
        }}
        .warning {{
            border-left-color: #ffc107;
        }}
        .danger {{
            border-left-color: #dc3545;
        }}
        .button {{
            display: inline-block;
            padding: 12px 24px;
            background-color: #007bff;
            color: white !important;
            text-decoration: none;
            border-radius: 5px;
            margin: 20px 0;
            font-weight: bold;
        }}
        .button:hover {{
            background-color: #0056b3;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #eee;
            font-size: 12px;
            color: #666;
            text-align: center;
        }}
        .amount {{
            font-size: 24px;
            font-weight: bold;
            color: #28a745;
        }}
        .amount.negative {{
            color: #dc3545;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{self.service_name}</h1>
        </div>
        <div class="content">
            {content}
        </div>
        <div class="footer">
            <p>&copy; {self.service_name}</p>
            <p>{footer_text}</p>
        </div>
    </div>
</body>
</html>
"""

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
        """Template for referral bonus notification."""
        bonus = context.get('formatted_bonus', f'{context.get("bonus_rubles", 0):.2f} ₽')
        referral_name = html.escape(context.get('referral_name', ''))

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
                </div>
                <p>Продолжайте приглашать друзей и зарабатывайте больше!</p>
                {self._get_cabinet_button(language)}
            """,
            'en': f"""
                <h2>Referral Bonus!</h2>
                <div class="highlight success">
                    <p>You received a referral bonus: <span class="amount">+{bonus}</span></p>
                    {f'<p>Thanks to: {referral_name}</p>' if referral_name else ''}
                </div>
                <p>Keep inviting friends and earn more!</p>
                {self._get_cabinet_button(language)}
            """,
        }

        return {
            'subject': subjects.get(language, subjects['ru']),
            'body_html': self._get_base_template(bodies.get(language, bodies['ru']), language),
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
        }

        greeting = {
            'ru': f'Здравствуйте{", " + username if username else ""}!',
            'en': f'Hello{", " + username if username else ""}!',
            'zh': f'您好{", " + username if username else ""}!',
            'ua': f'Вітаємо{", " + username if username else ""}!',
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
        }

        greeting = {
            'ru': f'Здравствуйте{", " + username if username else ""}!',
            'en': f'Hello{", " + username if username else ""}!',
            'zh': f'您好{", " + username if username else ""}!',
            'ua': f'Вітаємо{", " + username if username else ""}!',
        }

        bodies = {
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
