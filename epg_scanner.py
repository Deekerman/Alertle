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
    channel_number: str = ""
    subtitle: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)
    uid: str = ""

    def __post_init__(self):
        if not self.uid:
            self.uid = f"{self.channel_id}|{self.start.isoformat()}|{self.title}"


class DispatcharrClient:
    def __init__(self, url: str, token: str, xmltv_url: str = ""):
        self.base = url.rstrip("/")
        self.xmltv_url = xmltv_url.strip()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_programmes(self, start: datetime, stop: datetime) -> list[Programme]:
        """Return all programmes between *start* and *stop* (UTC datetimes)."""
        root = self._fetch_direct() if self.xmltv_url else self._fetch_xmltv()
        channels = self._parse_channels(root)
        programmes = self._parse_programmes(root, channels, start, stop)
        log.info("Fetched %d programmes from Dispatcharr EPG", len(programmes))
        return programmes

    def probe_api(self) -> dict:
        """Hit every candidate path with every auth style and return a summary dict."""
        results = {}
        for path in self._candidate_paths():
            url = self.base + path
            for auth_name, headers in self._auth_variants().items():
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

    def _fetch_direct(self) -> ET.Element:
        """Fetch XMLTV from the exact URL the user configured."""
        try:
            resp = self.session.get(self.xmltv_url, timeout=60)
        except requests.RequestException as exc:
            raise RuntimeError(f"Request to {self.xmltv_url} failed: {exc}") from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"EPG URL returned HTTP {resp.status_code}: {self.xmltv_url}"
            )

        content = resp.content
        snippet = content[:300].decode("utf-8", errors="replace").strip()

        if not content or not snippet:
            raise RuntimeError(f"EPG URL returned an empty response: {self.xmltv_url}")

        if content.lstrip()[:1] in (b"{", b"["):
            return self._json_to_xmltv(json.loads(content))

        if content.lstrip()[:1] != b"<":
            raise RuntimeError(
                f"EPG URL did not return XML or JSON.\n"
                f"Response starts with: {snippet[:200]}"
            )

        try:
            return ET.fromstring(content)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"EPG URL returned invalid XML ({exc}).\n"
                f"Response starts with: {snippet[:200]}"
            ) from exc

    @staticmethod
    def _candidate_paths() -> list[str]:
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

    def _auth_variants(self) -> dict[str, dict]:
        token = self.session.headers.get("Authorization", "").split(" ", 1)[-1]
        return {
            "Bearer": {"Authorization": f"Bearer {token}"},
            "Token":  {"Authorization": f"Token {token}"},
            "none":   {},
        }

    def _fetch_xmltv(self) -> ET.Element:
        """Try each candidate endpoint (with multiple auth styles) and return the first valid XMLTV."""
        last_exc: Optional[Exception] = None
        attempted: list[str] = []

        for path in self._candidate_paths():
            url = self.base + path
            for auth in self._auth_variants().values():
                try:
                    resp = self.session.get(url, headers=auth, timeout=60)
                except requests.RequestException as exc:
                    last_exc = exc
                    log.debug("EPG %s — request failed: %s", url, exc)
                    break

                if resp.status_code in (401, 403):
                    log.debug("EPG %s — auth rejected (%d)", url, resp.status_code)
                    continue

                if resp.status_code != 200:
                    log.debug("EPG %s — HTTP %d", url, resp.status_code)
                    break

                content = resp.content
                snippet = content[:400].decode("utf-8", errors="replace").strip()
                attempted.append(f"{url}  [{resp.status_code}]  {snippet[:120]!r}")

                if not content or not snippet:
                    log.warning("EPG %s — 200 OK but empty body", url)
                    break

                if content.lstrip()[:1] in (b"{", b"["):
                    try:
                        return self._json_to_xmltv(json.loads(content))
                    except Exception as exc:
                        log.warning("EPG %s — JSON parse failed: %s", url, exc)
                        last_exc = exc
                        break

                try:
                    root = ET.fromstring(content)
                    log.info("EPG loaded from %s", url)
                    return root
                except ET.ParseError as exc:
                    log.warning(
                        "EPG %s — 200 OK but not valid XML.\n  Error: %s\n  Response: %s",
                        url, exc, snippet[:300],
                    )
                    last_exc = exc
                    break

        summary = "\n".join(f"  {a}" for a in attempted) or "  (none reached 200 OK)"
        raise RuntimeError(
            f"Could not fetch a valid XMLTV feed from Dispatcharr at {self.base}.\n"
            f"Last error: {last_exc}\n\n"
            f"Endpoints that returned 200 OK:\n{summary}\n\n"
            "Troubleshooting tips:\n"
            "  • Use 'Test connection & probe endpoints' on the Settings page\n"
            "  • In Dispatcharr, check Settings → Output and copy the XMLTV URL exactly\n"
            "  • Paste that full URL into the 'Direct XMLTV URL' field in Settings"
        )

    @staticmethod
    def _parse_channels(root: ET.Element) -> dict[str, tuple[str, str]]:
        """Return mapping of channel id → (display_name, channel_number)."""
        channels: dict[str, tuple[str, str]] = {}
        for ch in root.findall("channel"):
            cid = ch.get("id", "")
            display_names = [el.text.strip() for el in ch.findall("display-name") if el.text]

            number = ""
            text_names: list[str] = []
            for dn in display_names:
                # A display-name that is purely numeric (or decimal like "7.1") is a channel number
                if dn.replace(".", "").isdigit():
                    number = number or dn
                else:
                    text_names.append(dn)

            # Some providers use an <lcn> element
            lcn_el = ch.find("lcn")
            if lcn_el is not None and lcn_el.text:
                number = lcn_el.text.strip()

            name = text_names[0] if text_names else (display_names[0] if display_names else cid)
            channels[cid] = (name, number)
        return channels

    @staticmethod
    def _parse_dt(value: str) -> datetime:
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
        return datetime.strptime(dt_part, "%Y%m%d%H%M%S").replace(tzinfo=tzinfo).astimezone(timezone.utc)

    def _parse_programmes(
        self,
        root: ET.Element,
        channels: dict[str, tuple[str, str]],
        start: datetime,
        stop: datetime,
    ) -> list[Programme]:
        programmes: list[Programme] = []
        for prog in root.findall("programme"):
            try:
                prog_start = self._parse_dt(prog.get("start", ""))
                prog_stop  = self._parse_dt(prog.get("stop",  ""))
            except (ValueError, AttributeError) as exc:
                log.debug("Skipping programme with unparseable time: %s", exc)
                continue

            if prog_stop < start or prog_start > stop:
                continue

            channel_id = prog.get("channel", "")
            ch_name, ch_number = channels.get(channel_id, (channel_id, ""))
            title_el    = prog.find("title")
            subtitle_el = prog.find("sub-title")
            desc_el     = prog.find("desc")
            categories  = [c.text for c in prog.findall("category") if c.text]

            programmes.append(Programme(
                channel_id=channel_id,
                channel_name=ch_name,
                channel_number=ch_number,
                title=title_el.text if title_el is not None else "",
                subtitle=subtitle_el.text if subtitle_el is not None else "",
                start=prog_start,
                stop=prog_stop,
                description=desc_el.text if desc_el is not None else "",
                categories=categories,
            ))
        return programmes

    @staticmethod
    def _json_to_xmltv(data: object) -> ET.Element:
        """Best-effort conversion of a JSON EPG payload to an in-memory XMLTV element tree."""

        def to_xmltv_dt(s: str) -> str:
            s = s.strip()
            if not s:
                return ""
            if len(s) >= 14 and s[:14].isdigit():
                return s if " " in s else s + " +0000"
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
                    return dt.strftime("%Y%m%d%H%M%S") + " +0000"
                except ValueError:
                    continue
            return s

        root = ET.Element("tv")
        items: list = data if isinstance(data, list) else data.get("programmes", data.get("events", []))  # type: ignore[union-attr]

        seen_channels: set[str] = set()
        for item in items:
            ch_id   = str(item.get("channel_id") or item.get("channel") or "unknown")
            ch_name = str(item.get("channel_name") or item.get("channelName") or ch_id)
            ch_num  = str(item.get("channel_number") or item.get("channelNumber") or "")

            if ch_id not in seen_channels:
                ch_el = ET.SubElement(root, "channel", id=ch_id)
                if ch_num:
                    ET.SubElement(ch_el, "display-name").text = ch_num
                ET.SubElement(ch_el, "display-name").text = ch_name
                seen_channels.add(ch_id)

            start_str = to_xmltv_dt(str(item.get("start") or item.get("startTime") or ""))
            if not start_str:
                continue
            stop_str = to_xmltv_dt(str(item.get("stop") or item.get("end") or item.get("endTime") or ""))

            prog_el = ET.SubElement(root, "programme", start=start_str, stop=stop_str, channel=ch_id)
            ET.SubElement(prog_el, "title").text = str(item.get("title") or item.get("name") or "")
            subtitle = str(item.get("subtitle") or item.get("subTitle") or item.get("sub_title") or "")
            if subtitle:
                ET.SubElement(prog_el, "sub-title").text = subtitle
            desc = str(item.get("description") or item.get("desc") or "")
            if desc:
                ET.SubElement(prog_el, "desc").text = desc
            for cat in (item.get("categories") or item.get("genres") or []):
                ET.SubElement(prog_el, "category").text = str(cat)

        return root
