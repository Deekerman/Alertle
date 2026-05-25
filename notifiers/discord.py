import json
import logging
import requests
from .base import BaseNotifier

log = logging.getLogger(__name__)


class DiscordNotifier(BaseNotifier):
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, title: str, body: str, image_bytes: bytes | None = None) -> None:
        if image_bytes:
            embed = {"title": title, "description": body, "image": {"url": "attachment://thumb.jpg"}}
            try:
                resp = requests.post(
                    self.webhook_url,
                    data={"payload_json": json.dumps({"embeds": [embed]})},
                    files={"file[0]": ("thumb.jpg", image_bytes, "image/jpeg")},
                    timeout=15,
                )
                resp.raise_for_status()
                log.info("Discord notification sent (with image): %s", title)
                return
            except requests.RequestException as exc:
                log.warning("Discord embed+image failed (%s), falling back to text", exc)

        safe_title = title.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
        payload = {"content": f"**{safe_title}**\n{body}"}
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Discord notification failed: %s", exc)
            return
        log.info("Discord notification sent: %s", title)
