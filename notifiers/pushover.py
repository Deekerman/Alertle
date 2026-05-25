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

    def send(self, title: str, body: str, image_url: str | None = None) -> None:
        payload = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": body,
            "priority": self.priority,
        }
        files = None
        if image_url:
            try:
                img = requests.get(image_url, timeout=5)
                img.raise_for_status()
                files = {"attachment": ("thumb.jpg", img.content, "image/jpeg")}
            except Exception as exc:
                log.warning("Could not fetch thumb for Pushover: %s", exc)
        try:
            resp = requests.post(PUSHOVER_API, data=payload, files=files, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Pushover notification failed: %s", exc)
            return
        log.info("Pushover notification sent: %s", title)
