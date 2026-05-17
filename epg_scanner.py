"""Fetch and parse EPG data from Dispatcharr."""

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
        root = self._fetch_xmltv()
        channels = self._parse_channels(root)
        programmes = self._parse_programmes(root, channels, start, stop)
        log.info("Fetched %d programmes from Dispatcharr EPG", len(programmes))
        return programmes

    def probe_api(self) -> dict:
        """Hit every candidate path with every auth style and return a summary dict."""
        token_value = self.session.headers.get("Authorization", "").split(" ", 1)[-1]
        auth_variants = {
            "Bearer": {"Authorization": f"Bearer {token_value}"},
            "Token":  {"Authorization": f"Token {token_value}"},
            "none":   {},
        }
        results = {}
        for path in self._candidate_paths():
            url = self.base + path
            for auth_name, headers in auth_variants.items():
                key = f"{path}  [{auth_name}]"
                try:
                    r = self.session.get(url, headers=headers, timeout=10)
                    snippet = r.text[:200].replace("\n", " ").strip()
                    results[key] = {"status": r.status_code, "snippet": snippet}
                except Exception as exc:
                    results[key] = {"status": None, "error": str(exc)}
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _candidate_paths() -> list[str]:
        # Dispatcharr-specific output endpoints first, then generic XMLTV paths.
        # Dispatcharr (Django-based) typically exposes output at /output/xmltv/
        # and may use a UUID-keyed URL like /output/xmltv/<uuid>/
        return [
            "/output/xmltv/",
            "/output/xmltv",
            "/api/output/xmltv/",
            "/api/epg/xmltv/",
            "/api/epg/xmltv",
            "/epg/xmltv/",
            "/epg/xmltv",
            "/xmltv/",
            "/xmltv",
        ]

    def _fetch_xmltv(self) -> ET.Element:
        """Try each candidate endpoint (with both Bearer and Token auth) and
        return the first response that parses as valid XMLTV."""
        last_exc: Optional[Exception] = None
        attempted: list[str] = []

        # Dispatcharr uses Django REST Framework which accepts "Token <key>"
        # as well as "Bearer <key>". Try both auth styles per URL.
        token_value = self.session.headers.get("Authorization", "").split(" ", 1)[-1]
        auth_headers_to_try = [
            {"Authorization": f"Bearer {token_value}"},
            {"Authorization": f"Token {token_value}"},
            {},  # some output endpoints are unauthenticated
        ]

        for path in self._candidate_paths():
            url = self.base + path
            for auth in auth_headers_to_try:
                try:
                    resp = self.session.get(url, headers=auth, timeout=60)
                except requests.RequestException as exc:
                    last_exc = exc
                    log.debug("EPG %s — request failed: %s", url, exc)
                    break  # network error — no point retrying different auth

                if resp.status_code in (401, 403):
                    log.debug("EPG %s — auth rejected (%d), trying next auth style", url, resp.status_code)
                    continue

                if resp.status_code != 200:
                    log.debug("EPG %s — HTTP %d", url, resp.status_code)
                    break  # non-auth error — skip remaining auth variants

                content = resp.content
                content_type = resp.headers.get("Content-Type", "")
                snippet = content[:400].decode("utf-8", errors="replace").strip()
                attempted.append(f"{url}  [{resp.status_code}]  {snippet[:120]!r}")

                if not content or not snippet:
                    log.warning("EPG %s — 200 OK but empty body", url)
                    break

                # JSON response
                if content.lstrip()[:1] in (b"{", b"["):
                    log.debug("EPG %s — JSON response, converting", url)
                    try:
                        return self._json_to_xmltv(json.loads(content))
                    except Exception as exc:
                        log.warning("EPG %s — JSON parse failed: %s", url, exc)
                        last_exc = exc
                        break

                # XMLTV / XML response
                try:
                    root = ET.fromstring(content)
                    log.info("EPG loaded from %s (auth: %s)", url,
                             list(auth.keys())[0].replace("Authorization", "") if auth else "none")
                    return root
                except ET.ParseError as exc:
                    log.warning(
                        "EPG %s — 200 OK but not valid XML.\n"
                        "  Parse error : %s\n"
                        "  Response    : %s",
                        url, exc, snippet[:300],
                    )
                    last_exc = exc
                    break  # bad content — different auth won't help

        summary = "\n".join(f"  {a}" for a in attempted) or "  (none reached 200 OK)"
        raise RuntimeError(
            f"Could not fetch a valid XMLTV feed from Dispatcharr at {self.base}.\n"
            f"Last error: {last_exc}\n\n"
            f"Endpoints that returned 200 OK:\n{summary}\n\n"
            "Troubleshooting tips:\n"
            "  • Use 'Test connection & probe endpoints' on the Settings page\n"
            "  • Confirm your Dispatcharr URL and API token are correct\n"
            "  • In Dispatcharr, check Settings → Output and copy the XMLTV URL exactly\n"
            "  • If Dispatcharr shows a UUID in the XMLTV URL, set that full path as the URL\n"
            "  • Verify at least one EPG source is mapped in Dispatcharr"
        )

    @staticmethod
    def _json_to_xmltv(data: object) -> ET.Element:
        """
        Best-effort conversion of a JSON EPG payload to an in-memory XMLTV element tree.
        Handles a list-of-programme objects as some providers return.
        """
        root = ET.Element("tv")

        items: list = data if isinstance(data, list) else data.get("programmes", data.get("events", []))  # type: ignore[union-attr]

        seen_channels: set[str] = set()
        for item in items:
            ch_id = str(item.get("channel_id") or item.get("channel") or "unknown")
            ch_name = str(item.get("channel_name") or item.get("channelName") or ch_id)

            if ch_id not in seen_channels:
                ch_el = ET.SubElement(root, "channel", id=ch_id)
                ET.SubElement(ch_el, "display-name").text = ch_name
                seen_channels.add(ch_id)

            # Parse start / stop — accept ISO strings or XMLTV format
            start_raw = str(item.get("start") or item.get("startTime") or "")
            stop_raw  = str(item.get("stop")  or item.get("end") or item.get("endTime") or "")

            def to_xmltv_dt(s: str) -> str:
                s = s.strip()
                if not s:
                    return ""
                # Already XMLTV format
                if len(s) >= 14 and s[:14].isdigit():
                    return s if " " in s else s + " +0000"
                # ISO 8601
                for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
                        return dt.strftime("%Y%m%d%H%M%S") + " +0000"
                    except ValueError:
                        continue
                return s

            start_str = to_xmltv_dt(start_raw)
            stop_str  = to_xmltv_dt(stop_raw)
            if not start_str:
                continue

            prog_el = ET.SubElement(root, "programme",
                                    start=start_str, stop=stop_str, channel=ch_id)
            title = str(item.get("title") or item.get("name") or "")
            ET.SubElement(prog_el, "title").text = title

            desc = str(item.get("description") or item.get("desc") or "")
            if desc:
                ET.SubElement(prog_el, "desc").text = desc

            for cat in (item.get("categories") or item.get("genres") or []):
                ET.SubElement(prog_el, "category").text = str(cat)

        return root

    @staticmethod
    def _parse_channels(root: ET.Element) -> dict[str, str]:
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
        if " " in value:
            dt_part, tz_part = value.split(" ", 1)
            tz_part = tz_part.replace(":", "")
            sign = 1 if tz_part[0] != "-" else -1
            tz_part = tz_part.lstrip("+-")
            tz_h, tz_m = int(tz_part[:2]), int(tz_part[2:4])
            offset_minutes = sign * (tz_h * 60 + tz_m)
            tzinfo = timezone(timedelta(minutes=offset_minutes))
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
                prog_stop  = self._parse_dt(prog.get("stop", ""))
            except (ValueError, AttributeError) as exc:
                log.debug("Skipping programme with unparseable time: %s", exc)
                continue

            if prog_stop < start or prog_start > stop:
                continue

            channel_id = prog.get("channel", "")
            title_el   = prog.find("title")
            desc_el    = prog.find("desc")
            categories = [c.text for c in prog.findall("category") if c.text]

            programmes.append(Programme(
                channel_id=channel_id,
                channel_name=channels.get(channel_id, channel_id),
                title=title_el.text if title_el is not None else "",
                start=prog_start,
                stop=prog_stop,
                description=desc_el.text if desc_el is not None else "",
                categories=categories,
            ))
        return programmes
