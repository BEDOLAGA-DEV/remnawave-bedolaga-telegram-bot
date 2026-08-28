"""Email service for sending verification and password reset emails."""

import re
import smtplib
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


# Сколько молчать после отказа установить соединение. Один упавший коннект
# доказывает недоступность сервера для всей пачки: рассылка на N адресов иначе
# ждёт таймаут N раз подряд и пишет N одинаковых трейсбеков. Держим окно
# коротким — примерно один таймаут коннекта: транзакционное письмо (код входа),
# отправленное сразу после сорвавшейся рассылки, всё равно не дошло бы, но и
# зависать в этом состоянии дольше необходимого не должно.
_CONNECTION_FAILURE_COOLDOWN_SECONDS = 30.0


class EmailService:
    """Service for sending emails via SMTP."""

    def __init__(self) -> None:
        # Момент, до которого считаем сервер недоступным. Гонки между потоками
        # исполнителя безобидны: худшее — лишняя попытка соединения.
        self._unreachable_until: float = 0.0
        self._unreachable_reason: str = ''

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
    def reply_to(self) -> str:
        return (settings.SMTP_REPLY_TO or '').strip()

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

    @staticmethod
    def _html_to_plain_text(body_html: str) -> str:
        """Грубая конвертация HTML в text/plain для multipart/alternative.

        Блоки <style>/<script> удаляются ЦЕЛИКОМ до вырезания тегов: сами теги
        регулярка убирала и раньше, а их содержимое (CSS/JS-правила) утекало в
        текстовую версию письма перед основным текстом (#2974).

        &amp; расшифровывается ПОСЛЕДНИМ: иначе "&amp;lt;" проходит двойную
        расшифровку и превращается в "<" вместо "&lt;".
        """
        text = re.sub(r'<(style|script)\b[^>]*>.*?</\1\s*>', '', body_html, flags=re.DOTALL | re.IGNORECASE)
        # Запасной проход для битого шаблона (кастомные письма из админки): открытый
        # <style>/<script> без закрывающего тега иначе утёк бы телом CSS/JS в текст —
        # срезаем висячий блок до конца ввода.
        text = re.sub(r'<(style|script)\b[^>]*>.*', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^<>]+>', '', text)
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&amp;', '&')
        # После удаления блоков и тегов остаются простыни пустых строк —
        # схлопываем, чтобы текст не начинался с десятков переносов.
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        return text.strip()

    def _endpoint(self) -> dict[str, Any]:
        """Куда именно шли — без этого «Network is unreachable» ничего не говорит."""
        mode = 'ssl' if self.use_ssl else ('starttls' if self.use_tls else 'plain')
        return {'smtp_host': self.host, 'smtp_port': self.port, 'smtp_mode': mode}

    def _cooldown_left(self) -> float:
        return max(0.0, self._unreachable_until - time.monotonic())

    def _note_connection_failure(self, error: BaseException) -> None:
        self._unreachable_until = time.monotonic() + _CONNECTION_FAILURE_COOLDOWN_SECONDS
        self._unreachable_reason = f'{type(error).__name__}: {error}'

    def _note_success(self) -> None:
        self._unreachable_until = 0.0
        self._unreachable_reason = ''

    def _log_connection_failure(self, to_email: str, error: BaseException) -> None:
        """Причина строкой, а не объектом исключения.

        Объект в kwarg заставляет логгер приложить трейсбек, а он здесь всегда
        одинаковый (три кадра внутри smtplib) и не добавляет ничего к «куда
        шли и что ответила сеть». На рассылке это N одинаковых полотен в логе.
        """
        logger.warning(
            'Не удалось соединиться с SMTP-сервером, письмо не отправлено',
            to_email=to_email,
            reason=f'{type(error).__name__}: {error}',
            retry_after_seconds=_CONNECTION_FAILURE_COOLDOWN_SECONDS,
            **self._endpoint(),
        )

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
        attachments: list[tuple[str, bytes, str]] | None = None,
        unsubscribe_url: str | None = None,
    ) -> bool:
        """
        Send an email.

        Args:
            to_email: Recipient email address
            subject: Email subject
            body_html: HTML body content
            body_text: Plain text body (optional, generated from HTML if not provided)
            attachments: Optional list of (filename, content, mimetype) tuples
            unsubscribe_url: One-click unsubscribe URL. Задаётся ТОЛЬКО для
                маркетинговых писем — на транзакционных (код входа, чек об
                оплате) List-Unsubscribe не ставят.

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

        cooldown_left = self._cooldown_left()
        if cooldown_left:
            # Соединение только что не состоялось — ждать таймаут ещё раз незачем.
            logger.debug(
                'SMTP недоступен, письмо пропущено без попытки соединения',
                to_email=to_email,
                retry_in_seconds=round(cooldown_left, 1),
                last_failure=self._unreachable_reason,
                **self._endpoint(),
            )
            return False

        try:
            # С вложениями письмо становится multipart/mixed: внутри него
            # обычная alternative-пара text/html плюс файлы.
            alternative = MIMEMultipart('alternative')
            msg = MIMEMultipart('mixed') if attachments else alternative
            msg['Subject'] = subject
            safe_from_name = self.from_name.replace('\n', '').replace('\r', '') if self.from_name else ''
            safe_from_email = sender_email.replace('\n', '').replace('\r', '')
            msg['From'] = formataddr((safe_from_name, safe_from_email)) if safe_from_name else safe_from_email
            msg['To'] = to_email
            # Адрес из .env: перенос строки в нём дописал бы произвольный
            # заголовок в письмо, поэтому кривое значение не чиним, а
            # выбрасываем — письмо важнее обратного канала.
            if reply_to := self.reply_to:
                if any(ch in reply_to for ch in '\r\n') or '@' not in reply_to:
                    logger.warning('Некорректный SMTP_REPLY_TO — заголовок Reply-To пропущен')
                else:
                    msg['Reply-To'] = formataddr((safe_from_name, reply_to))

            msg['Date'] = formatdate(localtime=False)
            msg['Message-ID'] = make_msgid(domain=safe_from_email.split('@')[-1])

            # RFC 8058: пара List-Unsubscribe + List-Unsubscribe-Post — это то, из
            # чего Gmail/Yahoo рисуют свою кнопку «Отписаться» рядом с адресом
            # отправителя. Без -Post заголовок считается «старым» и кнопку дают
            # не всегда.
            if unsubscribe_url:
                safe_unsubscribe = unsubscribe_url.strip()
                # URL приходит из настроек/БД: перенос строки в нём дописал бы
                # произвольный заголовок в письмо, поэтому такой URL не чиним, а
                # выбрасываем целиком вместе с заголовками.
                if any(ch in safe_unsubscribe for ch in '\r\n<>') or not safe_unsubscribe.startswith(
                    ('http://', 'https://')
                ):
                    logger.warning('Некорректный unsubscribe_url — заголовки отписки пропущены')
                else:
                    from .email_unsubscribe import build_unsubscribe_mailto

                    targets = [f'<{safe_unsubscribe}>']
                    if mailto := build_unsubscribe_mailto():
                        targets.append(f'<{mailto}>')
                    msg['List-Unsubscribe'] = ', '.join(targets)
                    msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'

            # Plain text version
            if body_text is None:
                body_text = self._html_to_plain_text(body_html)

            part1 = MIMEText(body_text, 'plain', 'utf-8')
            part2 = MIMEText(body_html, 'html', 'utf-8')

            alternative.attach(part1)
            alternative.attach(part2)

            if attachments:
                msg.attach(alternative)
                for filename, content, mimetype in attachments:
                    maintype, _, subtype = (mimetype or 'application/octet-stream').partition('/')
                    attachment_part = MIMEBase(maintype or 'application', subtype or 'octet-stream')
                    attachment_part.set_payload(content)
                    encoders.encode_base64(attachment_part)
                    safe_filename = filename.replace('\n', '').replace('\r', '')
                    attachment_part.add_header('Content-Disposition', 'attachment', filename=safe_filename)
                    msg.attach(attachment_part)

            try:
                with self._get_smtp_connection() as smtp:
                    smtp.sendmail(safe_from_email, to_email, msg.as_string())
            # Порядок веток задан иерархией smtplib: SMTPException наследуется
            # от OSError, поэтому широкий except OSError выше перехватывал бы и
            # отказ по одному адресу — и глушил бы почту всем на время остывания.
            except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected) as connection_error:
                self._note_connection_failure(connection_error)
                self._log_connection_failure(to_email, connection_error)
                return False
            except smtplib.SMTPException as smtp_error:
                # Сервер ответил отказом: отклонён адрес, не прошла авторизация,
                # превышен лимит. Соединение при этом рабочее, и остывание не
                # объявляется: следующему адресу письмо может уйти.
                logger.warning(
                    'SMTP-сервер отклонил письмо',
                    to_email=to_email,
                    reason=f'{type(smtp_error).__name__}: {smtp_error}',
                    **self._endpoint(),
                )
                return False
            except OSError as connection_error:
                # Сеть: недоступный маршрут, таймаут, отказ в соединении, DNS.
                self._note_connection_failure(connection_error)
                self._log_connection_failure(to_email, connection_error)
                return False

            self._note_success()
            logger.info('Email sent successfully to', to_email=to_email)
            return True

        except Exception as e:
            # Сюда попадает уже не работа сети, а ошибка сборки письма — такой
            # трейсбек нужен.
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
