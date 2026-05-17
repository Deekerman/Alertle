import logging
import requests
from .base import BaseNotifier

log = logging.getLogger(__name__)


class DiscordNotifier(BaseNotifier):
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, title: str, body: str) -> None:
        payload = {"content": f"**{title}**\n{body}"}
        resp = requests.post(self.webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Discord notification sent: %s", title)
