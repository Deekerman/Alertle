import html
import logging
import requests
from .base import BaseNotifier

log = logging.getLogger(__name__)


class TelegramNotifier(BaseNotifier):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, title: str, body: str) -> None:
        text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Telegram notification failed: %s", exc)
            return
        log.info("Telegram notification sent: %s", title)
