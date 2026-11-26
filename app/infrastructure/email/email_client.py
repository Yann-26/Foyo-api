"""Transactional e-mail.

EMAIL_BACKEND=log   writes the message to the server log (development).
EMAIL_BACKEND=smtp  sends it through SMTP — configured for Gmail by default (see README.md).
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.core.config import Settings, get_settings

log = logging.getLogger("foyo.email")


class EmailConfigError(RuntimeError):
    pass


def check_config(s: Settings | None = None) -> None:
    """Raises EmailConfigError when the chosen backend can't work (called at start-up)."""
    s = s or get_settings()
    if s.email_backend == "log":
        return
    if s.email_backend != "smtp":
        raise EmailConfigError(f"Unknown EMAIL_BACKEND {s.email_backend!r} (use 'log' or 'smtp')")
    if not s.smtp_username or not s.smtp_password.get_secret_value():
        raise EmailConfigError("EMAIL_BACKEND=smtp needs SMTP_USERNAME and SMTP_PASSWORD (a Gmail App Password)")


def build_message(s: Settings, to: str, subject: str, body: str) -> EmailMessage:
    sender = s.smtp_from or s.smtp_username
    msg = EmailMessage()
    msg["From"] = formataddr((s.smtp_from_name, sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    msg.set_content(body)
    return msg


def _send_smtp(s: Settings, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    password = s.smtp_password.get_secret_value()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds, context=context) as smtp:
            smtp.login(s.smtp_username, password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds) as smtp:
        smtp.starttls(context=context)
        smtp.login(s.smtp_username, password)
        smtp.send_message(msg)


def send_email(to: str, subject: str, body: str) -> None:
    s = get_settings()
    if s.email_backend == "log":
        log.warning("E-MAIL (not delivered, EMAIL_BACKEND=log) to=%s subject=%r\n%s", to, subject, body)
        return
    check_config(s)
    _send_smtp(s, build_message(s, to, subject, body))
    log.info("E-mail sent to=%s subject=%r", to, subject)


def deliver(to: str, subject: str, body: str) -> None:
    """Background-task wrapper: a delivery failure is logged, never shown to the requester."""
    try:
        send_email(to, subject, body)
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP login refused for %s: check SMTP_USERNAME and the Gmail App Password", get_settings().smtp_username)
    except Exception:
        log.exception("E-mail to %s could not be sent", to)
