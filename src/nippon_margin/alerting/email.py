"""SMTP delivery.

Host, port and addresses are config (they are not secret); the username and
password come from `SMTP_USERNAME` / `SMTP_PASSWORD` in the environment.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..config import EmailConfig
from . import env_id, env_secret

log = logging.getLogger(__name__)


def configured(cfg: EmailConfig) -> bool:
    return bool(cfg.smtp_host and cfg.from_addr and cfg.to_addr and env_secret("SMTP_PASSWORD"))


def send(cfg: EmailConfig, *, subject: str, body: str, html_body: str | None = None) -> bool:
    if not configured(cfg):
        log.warning("email not configured (need smtp_host/from/to in config.yaml + SMTP_PASSWORD)")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.from_addr
    message["To"] = cfg.to_addr
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(env_id("SMTP_USERNAME") or cfg.from_addr,
                         env_secret("SMTP_PASSWORD"))
            server.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("SMTP send failed: %s", exc)
        return False
