"""Fetch and parse EPG data from Dispatcharr."""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)


@dataclass
class Programme:
    channel_id: str
    channel_name: str
    title: str
    start: datetime
    stop: datetime
    description: str = ""
    categories: list[str] = field(default_factory=list)
    # Unique key used for deduplication in storage
    uid: str = ""

    def __post_init__(self):
        if not self.uid:
            self.uid = f"{self.channel_id}|{self.start.isoformat()}|{self.title}"


class DispatcharrClient:
    def __init__(self, url: str, token: str):
        self.base = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_programmes(self, start: datetime, stop: datetime) -> list[Programme]:
        """Return all programmes between *start* and *stop* (UTC datetimes)."""
        xmltv = self._fetch_xmltv()
        channels = self._parse_channels(xmltv)
        programmes = self._parse_programmes(xmltv, channels, start, stop)
        log.info("Fetched %d programmes from Dispatcharr EPG", len(programmes))
        return programmes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_xmltv(self) -> ET.Element:
        """Download the XMLTV EPG from Dispatcharr and return the root element."""
        # Dispatcharr exposes its combined EPG at /api/epg/xmltv/
        # Try the most common endpoint patterns.
        endpoints = [
            "/api/epg/xmltv/",
            "/epg/xmltv",
            "/xmltv",
        ]
        last_error: Optional[Exception] = None
        for path in endpoints:
            url = self.base + path
            try:
                resp = self.session.get(url, timeout=60)
                if resp.status_code == 200:
                    log.debug("EPG fetched from %s (%d bytes)", url, len(resp.content))
                    return ET.fromstring(resp.content)
                log.debug("EPG endpoint %s returned %d", url, resp.status_code)
            except requests.RequestException as exc:
                last_error = exc
                log.debug("EPG endpoint %s failed: %s", url, exc)

        raise RuntimeError(
            f"Could not fetch XMLTV from Dispatcharr. Last error: {last_error}"
        )

    @staticmethod
    def _parse_channels(root: ET.Element) -> dict[str, str]:
        """Return mapping of channel id → display name."""
        channels: dict[str, str] = {}
        for ch in root.findall("channel"):
            cid = ch.get("id", "")
            name_el = ch.find("display-name")
            channels[cid] = name_el.text if name_el is not None else cid
        return channels

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        """Parse an XMLTV datetime string (e.g. '20240101120000 +0000') to UTC datetime."""
        value = value.strip()
        # Format: YYYYMMDDHHmmss [+HHMM]
        if " " in value:
            dt_part, tz_part = value.split(" ", 1)
            tz_part = tz_part.replace(":", "")
            sign = 1 if tz_part[0] != "-" else -1
            tz_part = tz_part.lstrip("+-")
            tz_h, tz_m = int(tz_part[:2]), int(tz_part[2:4])
            offset_minutes = sign * (tz_h * 60 + tz_m)
            from datetime import timedelta, timezone as tz
            tzinfo = tz(timedelta(minutes=offset_minutes))
        else:
            dt_part = value
            tzinfo = timezone.utc

        dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S").replace(tzinfo=tzinfo)
        return dt.astimezone(timezone.utc)

    def _parse_programmes(
        self,
        root: ET.Element,
        channels: dict[str, str],
        start: datetime,
        stop: datetime,
    ) -> list[Programme]:
        programmes: list[Programme] = []
        for prog in root.findall("programme"):
            try:
                prog_start = self._parse_dt(prog.get("start", ""))
                prog_stop = self._parse_dt(prog.get("stop", ""))
            except (ValueError, AttributeError) as exc:
                log.debug("Skipping programme with unparseable time: %s", exc)
                continue

            if prog_stop < start or prog_start > stop:
                continue

            channel_id = prog.get("channel", "")
            title_el = prog.find("title")
            desc_el = prog.find("desc")
            categories = [c.text for c in prog.findall("category") if c.text]

            programmes.append(
                Programme(
                    channel_id=channel_id,
                    channel_name=channels.get(channel_id, channel_id),
                    title=title_el.text if title_el is not None else "",
                    start=prog_start,
                    stop=prog_stop,
                    description=desc_el.text if desc_el is not None else "",
                    categories=categories,
                )
            )
        return programmes
