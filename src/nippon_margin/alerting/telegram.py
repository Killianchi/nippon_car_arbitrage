"""Telegram delivery.

Credentials come from the environment only -- `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`, set as GitHub secrets in CI and in `.env` locally. They
are never read from config.yaml, so a config commit can never leak a token.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
#: Telegram rejects anything longer; we split rather than truncate.
MAX_LEN = 4000


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str, *, disable_preview: bool = True) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset)")
        return False

    ok = True
    for chunk in _split(text):
        try:
            resp = httpx.post(
                f"{API}/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": disable_preview,
                },
                timeout=20,
            )
            if resp.status_code != 200:
                log.error("Telegram rejected the message: %s %s", resp.status_code, resp.text[:300])
                ok = False
        except Exception as exc:  # noqa: BLE001 - a dead alert channel is not a dead run
            log.error("Telegram send failed: %s", exc)
            ok = False
    return ok


def _split(text: str) -> list[str]:
    """Split on line boundaries so a message never breaks mid-listing."""
    if len(text) <= MAX_LEN:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > MAX_LEN and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
