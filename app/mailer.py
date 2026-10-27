"""Sending email. Without SMTP settings, messages are logged instead (dev/test)."""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import get_settings

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "web" / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


@dataclass
class Sent:
    to: str
    subject: str
    text: str
    html: str


# Every message is also kept here so tests and the dev console can inspect them.
OUTBOX: list[Sent] = []


def render(template: str, **context) -> tuple[str, str]:
    settings = get_settings()
    context.setdefault("site_name", settings.site_name)
    context.setdefault("base_url", settings.public_base_url)
    html = _env.get_template(f"email/{template}.html").render(**context)
    text = _env.get_template(f"email/{template}.txt").render(**context)
    return text, html


def send(to: str, subject: str, template: str, **context) -> bool:
    settings = get_settings()
    text, html = render(template, **context)
    OUTBOX.append(Sent(to, subject, text, html))
    del OUTBOX[:-200]
    if not settings.email_enabled:
        log.info("Email (not sent, SMTP not configured) to=%s subject=%s\n%s", to, subject, text)
        return True
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
        return True
    except (smtplib.SMTPException, OSError):
        log.exception("Email to %s failed", to)
        return False
