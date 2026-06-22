"""Email service for sending verification and password reset emails."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

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

    @property
    def use_ssl(self) -> bool:
        # Port 465 always implies implicit TLS (SMTPS, RFC 8314).
        return settings.SMTP_USE_SSL or self.port == 465

    def is_configured(self) -> bool:
        """Check if SMTP is properly configured."""
        return settings.is_smtp_configured()

    def _get_smtp_connection(self) -> smtplib.SMTP:
        """Create and return SMTP connection."""
        if self.use_ssl:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
            smtp.ehlo()
        else:
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
            msg['From'] = formataddr((safe_from_name, safe_from_email))
            msg['To'] = to_email
            msg['Date'] = formatdate(localtime=False)
            msg['Message-ID'] = make_msgid(domain=safe_from_email.split('@')[-1])

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

    def _render_default_template(
        self,
        notification_type: str,
        language: str,
        context: dict[str, Any],
    ) -> tuple[str, str] | None:
        """
        Render the built-in default template for an auth email.

        Single source of truth: the same EmailNotificationTemplates the admin
        editor and the notification delivery service use — what the admin sees
        in the editor preview is exactly what this service sends.

        Imports are lazy to avoid a module cycle
        (notification_delivery_service imports this module).
        """
        from app.services.notification_delivery_service import NotificationType

        from .email_templates import EmailNotificationTemplates

        try:
            template = EmailNotificationTemplates().get_template(NotificationType(notification_type), language, context)
        except Exception as e:
            logger.error(
                'Не удалось отрендерить дефолтный email шаблон',
                notification_type=notification_type,
                language=language,
                error=e,
            )
            return None
        if not template:
            return None
        return (template['subject'], template['body_html'])

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

        rendered = self._render_default_template(
            'email_verification',
            language,
            {
                'username': username or '',
                'verification_url': f'{verification_url}?token={verification_token}',
                'expire_hours': settings.get_cabinet_email_verification_expire_hours(),
            },
        )
        if not rendered:
            return False
        return self.send_email(to_email, *rendered)

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

        rendered = self._render_default_template(
            'password_reset',
            language,
            {
                'username': username or '',
                'reset_url': f'{reset_url}?token={reset_token}',
                'expire_hours': settings.get_cabinet_password_reset_expire_hours(),
            },
        )
        if not rendered:
            return False
        return self.send_email(to_email, *rendered)

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

        rendered = self._render_default_template(
            'email_change_code',
            language,
            {
                'username': username or '',
                'code': code,
                'expire_minutes': settings.get_cabinet_email_change_code_expire_minutes(),
            },
        )
        if not rendered:
            return False
        return self.send_email(to_email, *rendered)


# Singleton instance
email_service = EmailService()
