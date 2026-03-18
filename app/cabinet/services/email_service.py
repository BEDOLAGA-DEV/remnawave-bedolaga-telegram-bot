"""Email service for sending verification and password reset emails."""

import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class EmailService:
    """Service for sending emails via SMTP."""

    @property
    def host(self) -> str | None:
        return settings.SMTP_HOST

    @property
    def port(self) -> int:
        return settings.SMTP_PORT

    @property
    def user(self) -> str | None:
        return settings.SMTP_USER

    @property
    def password(self) -> str | None:
        return settings.SMTP_PASSWORD

    @property
    def from_email(self) -> str | None:
        return settings.get_smtp_from_email()

    @property
    def from_name(self) -> str:
        return settings.SMTP_FROM_NAME

    @property
    def use_tls(self) -> bool:
        return settings.SMTP_USE_TLS

    def is_configured(self) -> bool:
        """Check if SMTP is properly configured."""
        return settings.is_smtp_configured()

    def _get_smtp_connection(self) -> smtplib.SMTP:
        """Create and return SMTP connection."""
        smtp = smtplib.SMTP(self.host, self.port, timeout=30)
        smtp.ehlo()

        if self.use_tls:
            smtp.starttls()
            smtp.ehlo()

        # Only attempt login if credentials are provided AND server supports AUTH
        if self.user and self.password:
            if smtp.has_extn('auth'):
                smtp.login(self.user, self.password)
            else:
                logger.debug('SMTP server does not support AUTH, skipping authentication', host=self.host)

        return smtp

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: str | None = None,
    ) -> bool:
        """
        Send an email.

        Args:
            to_email: Recipient email address
            subject: Email subject
            body_html: HTML body content
            body_text: Plain text body (optional, generated from HTML if not provided)

        Returns:
            True if email was sent successfully, False otherwise
        """
        if not self.is_configured():
            logger.warning('SMTP is not configured, cannot send email')
            return False

        sender_email = self.from_email
        if not sender_email or '@' not in sender_email:
            logger.error('Invalid or missing SMTP from_email, cannot send email', from_email=sender_email)
            return False

        # Defensive: strip newlines to prevent header injection
        to_email = to_email.strip().replace('\n', '').replace('\r', '')
        subject = subject.replace('\n', '').replace('\r', '')

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            safe_from_name = self.from_name.replace('\n', '').replace('\r', '') if self.from_name else ''
            safe_from_email = sender_email.replace('\n', '').replace('\r', '')
            msg['From'] = f'{safe_from_name} <{safe_from_email}>'
            msg['To'] = to_email
            msg['Date'] = formatdate(localtime=False)
            msg['Message-ID'] = make_msgid(domain=safe_from_email.split('@')[-1])
            msg['List-Unsubscribe'] = f'<mailto:{safe_from_email}?subject=unsubscribe>'
            msg['Precedence'] = 'bulk'

            # Plain text version
            if body_text is None:
                # Simple HTML to text conversion
                import re

                body_text = re.sub(r'<[^>]+>', '', body_html)
                body_text = body_text.replace('&nbsp;', ' ')
                body_text = body_text.replace('&amp;', '&')
                body_text = body_text.replace('&lt;', '<')
                body_text = body_text.replace('&gt;', '>')

            part1 = MIMEText(body_text, 'plain', 'utf-8')
            part2 = MIMEText(body_html, 'html', 'utf-8')

            msg.attach(part1)
            msg.attach(part2)

            with self._get_smtp_connection() as smtp:
                smtp.sendmail(safe_from_email, to_email, msg.as_string())

            logger.info('Email sent successfully to', to_email=to_email)
            return True

        except Exception as e:
            logger.error('Failed to send email to', to_email=to_email, error=e)
            return False

    def send_verification_email(
        self,
        to_email: str,
        verification_token: str,
        verification_url: str,
        username: str | None = None,
        language: str = 'ru',
        custom_subject: str | None = None,
        custom_body_html: str | None = None,
    ) -> bool:
        """
        Send email verification email.

        Args:
            to_email: Recipient email address
            verification_token: Verification token
            verification_url: Base URL for verification (token will be appended)
            username: User's name for personalization
            language: Language code (ru, en, zh, ua, fa)
            custom_subject: Override subject from admin template
            custom_body_html: Override body HTML from admin template (already wrapped in base template)

        Returns:
            True if email was sent successfully, False otherwise
        """
        if custom_subject and custom_body_html:
            return self.send_email(to_email, custom_subject, custom_body_html)

        full_url = f'{verification_url}?token={verification_token}'
        expire_hours = settings.get_cabinet_email_verification_expire_hours()

        # Escape user-provided values for HTML context
        safe_username = html.escape(username) if username else None

        # Localized content
        texts = {
            'ru': {
                'greeting': f'Здравствуйте{", " + safe_username if safe_username else ""}!',
                'subject': 'Подтверждение email адреса',
                'intro': 'Спасибо за регистрацию! Пожалуйста, подтвердите ваш email адрес, нажав на кнопку ниже:',
                'button': 'Подтвердить email',
                'or_copy': 'Или скопируйте и вставьте эту ссылку в браузер:',
                'expires': f'Ссылка действительна в течение {expire_hours} часов.',
                'ignore': 'Если вы не создавали аккаунт, просто проигнорируйте это письмо.',
                'regards': 'С уважением,',
            },
            'en': {
                'greeting': f'Hello{", " + safe_username if safe_username else ""}!',
                'subject': 'Verify your email address',
                'intro': 'Thank you for registering! Please verify your email address by clicking the button below:',
                'button': 'Verify Email',
                'or_copy': 'Or copy and paste this link in your browser:',
                'expires': f'This link will expire in {expire_hours} hours.',
                'ignore': "If you didn't create an account, you can safely ignore this email.",
                'regards': 'Best regards,',
            },
            'zh': {
                'greeting': f'您好{", " + safe_username if safe_username else ""}!',
                'subject': '验证您的邮箱地址',
                'intro': '感谢您的注册！请点击下方按钮验证您的邮箱地址：',
                'button': '验证邮箱',
                'or_copy': '或将此链接复制并粘贴到浏览器中：',
                'expires': f'此链接将在 {expire_hours} 小时后过期。',
                'ignore': '如果您没有创建账户，请忽略此邮件。',
                'regards': '此致,',
            },
            'ua': {
                'greeting': f'Вітаємо{", " + safe_username if safe_username else ""}!',
                'subject': 'Підтвердження email адреси',
                'intro': 'Дякуємо за реєстрацію! Будь ласка, підтвердіть вашу email адресу, натиснувши на кнопку нижче:',
                'button': 'Підтвердити email',
                'or_copy': 'Або скопіюйте та вставте це посилання в браузер:',
                'expires': f'Посилання дійсне протягом {expire_hours} годин.',
                'ignore': 'Якщо ви не створювали акаунт, просто проігноруйте цей лист.',
                'regards': 'З повагою,',
            },
            'fa': {
                'greeting': f'سلام{", " + safe_username if safe_username else ""}!',
                'subject': 'تایید آدرس ایمیل',
                'intro': 'از ثبت‌نام شما سپاسگزاریم! لطفاً با کلیک روی دکمه زیر ایمیل خود را تایید کنید:',
                'button': 'تایید ایمیل',
                'or_copy': 'یا این لینک را در مرورگر خود کپی و باز کنید:',
                'expires': f'این لینک تا {expire_hours} ساعت معتبر است.',
                'ignore': 'اگر شما این حساب را ایجاد نکرده‌اید، این ایمیل را نادیده بگیرید.',
                'regards': 'با احترام،',
            },
        }

        t = texts.get(language, texts['ru'])

        subject = t['subject']
        body_html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.6;color:#222;background:#f5f5f5">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5"><tr><td align="center" style="padding:30px 10px">
<table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;border:1px solid #e0e0e0">
<tr><td style="padding:32px 36px">
<p style="margin:0 0 18px;font-size:16px">{t['greeting']}</p>
<p style="margin:0 0 24px">{t['intro']}</p>
<table cellpadding="0" cellspacing="0"><tr><td style="background:#2563eb;border-radius:6px;padding:12px 28px">
<a href="{full_url}" style="color:#ffffff;text-decoration:none;font-weight:600;font-size:15px">{t['button']}</a>
</td></tr></table>
<p style="margin:24px 0 8px;font-size:13px;color:#666">{t['or_copy']}</p>
<p style="margin:0 0 20px;font-size:13px;word-break:break-all"><a href="{full_url}" style="color:#2563eb">{full_url}</a></p>
<p style="margin:0 0 8px;font-size:13px;color:#888">{t['expires']}</p>
<p style="margin:0;font-size:13px;color:#888">{t['ignore']}</p>
</td></tr>
<tr><td style="padding:16px 36px;border-top:1px solid #eee;font-size:12px;color:#999">
{t['regards']} {self.from_name}
</td></tr>
</table>
</td></tr></table>
</body></html>'''

        return self.send_email(to_email, subject, body_html)

    def send_password_reset_email(
        self,
        to_email: str,
        reset_token: str,
        reset_url: str,
        username: str | None = None,
        language: str = 'ru',
        custom_subject: str | None = None,
        custom_body_html: str | None = None,
    ) -> bool:
        """
        Send password reset email.

        Args:
            to_email: Recipient email address
            reset_token: Password reset token
            reset_url: Base URL for password reset (token will be appended)
            username: User's name for personalization
            language: Language code (ru, en, zh, ua, fa)
            custom_subject: Override subject from admin template
            custom_body_html: Override body HTML from admin template (already wrapped in base template)

        Returns:
            True if email was sent successfully, False otherwise
        """
        if custom_subject and custom_body_html:
            return self.send_email(to_email, custom_subject, custom_body_html)

        full_url = f'{reset_url}?token={reset_token}'
        expire_hours = settings.get_cabinet_password_reset_expire_hours()

        # Escape user-provided values for HTML context
        safe_username = html.escape(username) if username else None

        # Localized content
        texts = {
            'ru': {
                'greeting': f'Здравствуйте{", " + safe_username if safe_username else ""}!',
                'subject': 'Сброс пароля',
                'intro': 'Мы получили запрос на сброс вашего пароля. Нажмите на кнопку ниже, чтобы установить новый пароль:',
                'button': 'Сбросить пароль',
                'or_copy': 'Или скопируйте и вставьте эту ссылку в браузер:',
                'expires': f'Ссылка действительна в течение {expire_hours} часов.',
                'warning': 'Если вы не запрашивали сброс пароля, проигнорируйте это письмо или свяжитесь с поддержкой.',
                'regards': 'С уважением,',
            },
            'en': {
                'greeting': f'Hello{", " + safe_username if safe_username else ""}!',
                'subject': 'Reset your password',
                'intro': 'We received a request to reset your password. Click the button below to set a new password:',
                'button': 'Reset Password',
                'or_copy': 'Or copy and paste this link in your browser:',
                'expires': f'This link will expire in {expire_hours} hour(s).',
                'warning': "If you didn't request a password reset, please ignore this email or contact support if you're concerned.",
                'regards': 'Best regards,',
            },
            'zh': {
                'greeting': f'您好{", " + safe_username if safe_username else ""}!',
                'subject': '重置您的密码',
                'intro': '我们收到了重置您密码的请求。点击下方按钮设置新密码：',
                'button': '重置密码',
                'or_copy': '或将此链接复制并粘贴到浏览器中：',
                'expires': f'此链接将在 {expire_hours} 小时后过期。',
                'warning': '如果您没有请求重置密码，请忽略此邮件或联系客服。',
                'regards': '此致,',
            },
            'ua': {
                'greeting': f'Вітаємо{", " + safe_username if safe_username else ""}!',
                'subject': 'Скидання пароля',
                'intro': 'Ми отримали запит на скидання вашого пароля. Натисніть на кнопку нижче, щоб встановити новий пароль:',
                'button': 'Скинути пароль',
                'or_copy': 'Або скопіюйте та вставте це посилання в браузер:',
                'expires': f'Посилання дійсне протягом {expire_hours} годин.',
                'warning': "Якщо ви не запитували скидання пароля, проігноруйте цей лист або зв'яжіться з підтримкою.",
                'regards': 'З повагою,',
            },
            'fa': {
                'greeting': f'سلام{", " + safe_username if safe_username else ""}!',
                'subject': 'بازنشانی رمز عبور',
                'intro': 'درخواستی برای بازنشانی رمز عبور شما دریافت شد. برای تعیین رمز جدید روی دکمه زیر بزنید:',
                'button': 'بازنشانی رمز عبور',
                'or_copy': 'یا این لینک را در مرورگر خود کپی و باز کنید:',
                'expires': f'این لینک تا {expire_hours} ساعت معتبر است.',
                'warning': 'اگر شما درخواست بازنشانی رمز عبور نداده‌اید، این ایمیل را نادیده بگیرید یا با پشتیبانی تماس بگیرید.',
                'regards': 'با احترام،',
            },
        }

        t = texts.get(language, texts['ru'])

        subject = t['subject']
        body_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .button {{
                    display: inline-block;
                    padding: 12px 24px;
                    background-color: #dc3545;
                    color: white !important;
                    text-decoration: none;
                    border-radius: 5px;
                    margin: 20px 0;
                }}
                .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
                .warning {{ color: #dc3545; font-weight: bold; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2>{t['greeting']}</h2>
                <p>{t['intro']}</p>
                <a href="{full_url}" class="button">{t['button']}</a>
                <p>{t['or_copy']}</p>
                <p><a href="{full_url}">{full_url}</a></p>
                <p>{t['expires']}</p>
                <p class="warning">{t['warning']}</p>
                <div class="footer">
                    <p>{t['regards']}<br>{self.from_name}</p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, body_html)

    def send_email_change_code(
        self,
        to_email: str,
        code: str,
        username: str | None = None,
        language: str = 'ru',
        custom_subject: str | None = None,
        custom_body_html: str | None = None,
    ) -> bool:
        """
        Send email change verification code.

        Args:
            to_email: New email address
            code: 6-digit verification code
            username: User's name for personalization
            language: Language code (ru, en, zh, ua, fa)
            custom_subject: Override subject from admin template
            custom_body_html: Override body HTML from admin template

        Returns:
            True if email was sent successfully, False otherwise
        """
        if custom_subject and custom_body_html:
            return self.send_email(to_email, custom_subject, custom_body_html)

        expire_minutes = settings.get_cabinet_email_change_code_expire_minutes()

        # Escape user-provided values for HTML context
        safe_username = html.escape(username) if username else None

        texts = {
            'ru': {
                'greeting': f'Здравствуйте{", " + safe_username if safe_username else ""}!',
                'subject': 'Код подтверждения для смены email',
                'intro': 'Вы запросили смену email адреса. Используйте код ниже для подтверждения:',
                'code_label': 'Ваш код подтверждения:',
                'expires': f'Код действителен в течение {expire_minutes} минут.',
                'ignore': 'Если вы не запрашивали смену email, просто проигнорируйте это письмо.',
                'regards': 'С уважением,',
            },
            'en': {
                'greeting': f'Hello{", " + safe_username if safe_username else ""}!',
                'subject': 'Email change verification code',
                'intro': 'You requested to change your email address. Use the code below to confirm:',
                'code_label': 'Your verification code:',
                'expires': f'This code will expire in {expire_minutes} minutes.',
                'ignore': "If you didn't request an email change, you can safely ignore this email.",
                'regards': 'Best regards,',
            },
            'zh': {
                'greeting': f'您好{", " + safe_username if safe_username else ""}!',
                'subject': '邮箱更换验证码',
                'intro': '您请求更换邮箱地址。请使用以下验证码确认：',
                'code_label': '您的验证码：',
                'expires': f'此验证码将在 {expire_minutes} 分钟后过期。',
                'ignore': '如果您没有请求更换邮箱，请忽略此邮件。',
                'regards': '此致,',
            },
            'ua': {
                'greeting': f'Вітаємо{", " + safe_username if safe_username else ""}!',
                'subject': 'Код підтвердження для зміни email',
                'intro': 'Ви запросили зміну email адреси. Використовуйте код нижче для підтвердження:',
                'code_label': 'Ваш код підтвердження:',
                'expires': f'Код дійсний протягом {expire_minutes} хвилин.',
                'ignore': 'Якщо ви не запитували зміну email, просто проігноруйте цей лист.',
                'regards': 'З повагою,',
            },
            'fa': {
                'greeting': f'سلام{", " + safe_username if safe_username else ""}!',
                'subject': 'کد تایید تغییر ایمیل',
                'intro': 'شما درخواست تغییر ایمیل داده‌اید. برای تایید از کد زیر استفاده کنید:',
                'code_label': 'کد تایید شما:',
                'expires': f'این کد تا {expire_minutes} دقیقه معتبر است.',
                'ignore': 'اگر شما درخواست تغییر ایمیل نداده‌اید، این ایمیل را نادیده بگیرید.',
                'regards': 'با احترام،',
            },
        }

        t = texts.get(language, texts['ru'])

        subject = t['subject']
        body_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .code-box {{
                    background-color: #f8f9fa;
                    border: 2px solid #007bff;
                    border-radius: 8px;
                    padding: 20px;
                    text-align: center;
                    margin: 20px 0;
                }}
                .code {{
                    font-size: 32px;
                    font-weight: bold;
                    letter-spacing: 8px;
                    color: #007bff;
                    font-family: monospace;
                }}
                .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2>{t['greeting']}</h2>
                <p>{t['intro']}</p>
                <div class="code-box">
                    <p>{t['code_label']}</p>
                    <p class="code">{code}</p>
                </div>
                <p>{t['expires']}</p>
                <p>{t['ignore']}</p>
                <div class="footer">
                    <p>{t['regards']}<br>{self.from_name}</p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, body_html)


# Singleton instance
email_service = EmailService()
