import logging
import smtplib
from email.mime.text import MIMEText
from typing import List
from .base import BaseNotifier

log = logging.getLogger(__name__)


class SmtpNotifier(BaseNotifier):
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: List[str],
        use_tls: bool = True,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        self.use_tls = use_tls

    def send(self, title: str, body: str) -> None:
        msg = MIMEText(body)
        msg["Subject"] = title
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)

        with smtplib.SMTP(self.host, self.port) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.sendmail(self.from_addr, self.to_addrs, msg.as_string())
        log.info("SMTP notification sent: %s", title)
