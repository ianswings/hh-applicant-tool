"""Уведомления в Telegram (эскалации + отчёты по прогонам).

Ходит через ОБЩИЙ прокси PROXY_URL (тот же, что у Anthropic-капчи) — Telegram
из РФ может резаться, поэтому шлём через VPS. Ошибки глотаем: упавшее
уведомление не должно ронять прогон.

Секреты — в .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__package__)


def proxy_from_env() -> Optional[str]:
    """Общий прокси: PROXY_URL (новое имя) или ANTHROPIC_PROXY_URL (совместимость)."""
    return (
        os.environ.get("PROXY_URL")
        or os.environ.get("ANTHROPIC_PROXY_URL")
        or None
    )


@dataclass
class TelegramNotifier:
    bot_token: str
    chat_id: str
    proxy_url: Optional[str] = None
    timeout: float = 15.0
    session: requests.Session = field(default_factory=requests.Session)

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            logger.info(
                "[notify] TELEGRAM_BOT_TOKEN/CHAT_ID не заданы — уведомления off"
            )
            return None
        return cls(bot_token=token, chat_id=chat, proxy_url=proxy_from_env())

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
        try:
            self.session.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                proxies=proxies,
                timeout=self.timeout,
            )
        except requests.RequestException as ex:
            logger.warning("[notify] не отправилось в Telegram: %s", ex)
