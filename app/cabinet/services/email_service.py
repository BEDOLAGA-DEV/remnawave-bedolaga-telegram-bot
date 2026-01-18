"""Email service for sending verification and password reset emails."""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    """Service for sending emails via SMTP."""

    def __init__(self):
        self.host = settings.SMTP_HOST
        self.port = settings.SMTP_PORT
        self.user = settings.SMTP_USER
        self.password = settings.SMTP_PASSWORD
        self.from_email = settings.get_smtp_from_email()
        self.from_name = settings.SMTP_FROM_NAME
        self.use_tls = settings.SMTP_USE_TLS

    def is_configured(self) -> bool:
        """Check if SMTP is properly configured."""
        return settings.is_smtp_configured()

    def _get_smtp_connection(self) -> smtplib.SMTP:
        """Create and return SMTP connection."""
        
        # Always check for dynamic setting updates (from DB/System Settings)
        if settings.SMTP_HOST:
            self.host = settings.SMTP_HOST
            self.port = settings.SMTP_PORT
            self.user = settings.SMTP_USER
            self.password = settings.SMTP_PASSWORD
            self.from_email = settings.get_smtp_from_email()
            self.from_name = settings.SMTP_FROM_NAME
            self.use_tls = settings.SMTP_USE_TLS

        logger.debug(f"Connecting to SMTP server: {self.host}:{self.port} (TLS: {self.use_tls})")

        if not self.host:
             raise ValueError("SMTP_HOST is not configured (empty or None)")
             
        smtp = None
        try:
            if self.port == 465:
                smtp = smtplib.SMTP_SSL(self.host, self.port)
            else:
                smtp = smtplib.SMTP(self.host, self.port)

            if self.port != 465 and self.use_tls:
                import ssl
                context = ssl.create_default_context()
                smtp.starttls(context=context)

            if self.user and self.password:
                smtp.login(self.user, self.password)

            return smtp
        except Exception as e:
            try:
                if smtp:
                    smtp.quit()
            except Exception:
                pass
            logger.error(f"Failed to connect to SMTP server {self.host}:{self.port} - {e}")
            raise

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: Optional[str] = None,
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
            logger.warning("SMTP is not configured, cannot send email")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{self.from_name} <{self.from_email}>"
            msg["To"] = to_email

            # Plain text version
            if body_text is None:
                # Simple HTML to text conversion
                import re
                body_text = re.sub(r"<[^>]+>", "", body_html)
                body_text = body_text.replace("&nbsp;", " ")
                body_text = body_text.replace("&amp;", "&")
                body_text = body_text.replace("&lt;", "<")
                body_text = body_text.replace("&gt;", ">")

            part1 = MIMEText(body_text, "plain", "utf-8")
            part2 = MIMEText(body_html, "html", "utf-8")

            msg.attach(part1)
            msg.attach(part2)

            with self._get_smtp_connection() as smtp:
                smtp.sendmail(self.from_email, to_email, msg.as_string())

            logger.info(f"Email sent successfully to {to_email}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False

    def send_verification_email(
        self,
        to_email: str,
        verification_token: str,
        verification_url: str,
        username: Optional[str] = None,
    ) -> bool:
        """
        Send email verification email.

        Args:
            to_email: Recipient email address
            verification_token: Verification token
            verification_url: Base URL for verification (token will be appended)
            username: User's name for personalization

        Returns:
            True if email was sent successfully, False otherwise
        """
        full_url = f"{verification_url}?token={verification_token}"
        greeting = f"Hello{', ' + username if username else ''}!"

        subject = "Verify your email address"
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
                    background-color: #007bff;
                    color: white !important;
                    text-decoration: none;
                    border-radius: 5px;
                    margin: 20px 0;
                }}
                .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2>{greeting}</h2>
                <p>Thank you for registering! Please verify your email address by clicking the button below:</p>
                <a href="{full_url}" class="button">Verify Email</a>
                <p>Or copy and paste this link in your browser:</p>
                <p><a href="{full_url}">{full_url}</a></p>
                <p>This link will expire in {settings.get_cabinet_email_verification_expire_hours()} hours.</p>
                <p>If you didn't create an account, you can safely ignore this email.</p>
                <div class="footer">
                    <p>Best regards,<br>{self.from_name}</p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, body_html)

    def send_password_reset_email(
        self,
        to_email: str,
        reset_token: str,
        reset_url: str,
        username: Optional[str] = None,
    ) -> bool:
        """
        Send password reset email.

        Args:
            to_email: Recipient email address
            reset_token: Password reset token
            reset_url: Base URL for password reset (token will be appended)
            username: User's name for personalization

        Returns:
            True if email was sent successfully, False otherwise
        """
        full_url = f"{reset_url}?token={reset_token}"
        greeting = f"Hello{', ' + username if username else ''}!"

        subject = "Reset your password"
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
                <h2>{greeting}</h2>
                <p>We received a request to reset your password. Click the button below to set a new password:</p>
                <a href="{full_url}" class="button">Reset Password</a>
                <p>Or copy and paste this link in your browser:</p>
                <p><a href="{full_url}">{full_url}</a></p>
                <p>This link will expire in {settings.get_cabinet_password_reset_expire_hours()} hour(s).</p>
                <p class="warning">If you didn't request a password reset, please ignore this email or contact support if you're concerned.</p>
                <div class="footer">
                    <p>Best regards,<br>{self.from_name}</p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, body_html)


# Singleton instance
email_service = EmailService()
