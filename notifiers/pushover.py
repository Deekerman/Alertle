import logging
import requests
from .base import BaseNotifier

log = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


class PushoverNotifier(BaseNotifier):
    def __init__(self, app_token: str, user_key: str, priority: int = 0):
        self.app_token = app_token
        self.user_key = user_key
        self.priority = priority

    def send(self, title: str, body: str, thumb_url: str = "") -> None:
        payload = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": body,
            "priority": self.priority,
        }
        if thumb_url:
            payload["url"] = thumb_url
            payload["url_title"] = "Matchup Image"
        try:
            resp = requests.post(PUSHOVER_API, data=payload, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Pushover notification failed: %s", exc)
            return
        log.info("Pushover notification sent: %s", title)
