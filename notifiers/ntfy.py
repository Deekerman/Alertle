import logging
import requests
from .base import BaseNotifier

log = logging.getLogger(__name__)


class NtfyNotifier(BaseNotifier):
    def __init__(self, url: str, topic: str, token: str = ""):
        self.endpoint = f"{url.rstrip('/')}/{topic}"
        self.token = token

    def send(self, title: str, body: str, image_url: str | None = None) -> None:
        headers = {"Title": title}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if image_url:
            headers["Attach"] = image_url
        try:
            resp = requests.post(self.endpoint, data=body.encode(), headers=headers, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Ntfy notification failed: %s", exc)
            return
        log.info("Ntfy notification sent: %s", title)
