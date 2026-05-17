"""Match EPG programmes against user subscriptions."""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from epg_scanner import Programme

log = logging.getLogger(__name__)

# Matches separators used in "Team A vs Team B" style titles.
# Handles "vs", "vs.", "v." with surrounding spaces, and the "@" symbol.
# Intentionally avoids bare " v " and " at " to prevent false positives.
_VS_SEP = re.compile(r'\s+(?:vs?\.?|@)\s+', re.IGNORECASE)
# Strip trailing qualifiers like " - ESPN+" or " (HD)"
_TRAILING = re.compile(r'\s*[-–(].*$')


def extract_teams(title: str) -> tuple[str, str] | None:
    """Return (team1, team2) if title looks like 'A vs B', else None."""
    parts = _VS_SEP.split(title.strip(), maxsplit=1)
    if len(parts) == 2:
        t1 = _TRAILING.sub("", parts[0]).strip()
        t2 = _TRAILING.sub("", parts[1]).strip()
        if t1 and t2:
            return t1, t2
    return None


@dataclass
class Subscription:
    label: str
    sport: Optional[str] = None       # substring match against EPG categories
    team: Optional[str] = None        # substring match against title/description + vs-parsing
    keyword: Optional[str] = None     # substring match against title/description
    channel: Optional[str] = None     # substring match against channel name or id
    exclude: list[str] = field(default_factory=list)  # any match here blocks the notification
    lead_time_minutes: int = 30

    def matches(self, prog: Programme) -> bool:
        """Return True if *prog* satisfies all conditions of this subscription."""

        # ── Positive filters (all must pass) ──────────────────────────
        if self.sport:
            if not any(self.sport.lower() in cat.lower() for cat in prog.categories):
                return False

        text = f"{prog.title} {prog.description}".lower()

        if self.team:
            team_l = self.team.lower()
            if team_l not in text:
                # Fall back to vs-parsing: match against either extracted team name
                teams = extract_teams(prog.title)
                if not (teams and any(team_l in t.lower() for t in teams)):
                    return False

        if self.keyword:
            if self.keyword.lower() not in text:
                return False

        if self.channel:
            ch = self.channel.lower()
            if ch not in prog.channel_name.lower() and ch not in prog.channel_id.lower():
                return False

        # ── Negative filters (any match blocks) ───────────────────────
        for term in self.exclude:
            t = term.strip().lower()
            if t and t in text:
                log.debug("'%s' excluded by term '%s'", prog.title, term)
                return False

        return True


@dataclass
class Match:
    programme: Programme
    subscription: Subscription


@dataclass
class GroupedMatch:
    """One event (same title + start time) potentially on multiple channels."""
    title: str
    start: object   # datetime
    stop: object    # datetime
    description: str
    categories: list[str]
    channels: list[tuple[str, str]]   # (channel_number, channel_name) per channel
    subscription: Subscription
    # uid of the first programme — used for notification dedup
    group_uid: str


def build_subscriptions(raw: list[dict], default_lead_time: int) -> list[Subscription]:
    subs: list[Subscription] = []
    for entry in raw:
        exclude_raw = entry.get("exclude", [])
        if isinstance(exclude_raw, str):
            exclude_raw = [x.strip() for x in exclude_raw.split(",") if x.strip()]
        subs.append(Subscription(
            label=entry.get("label", "Unnamed"),
            sport=entry.get("sport"),
            team=entry.get("team"),
            keyword=entry.get("keyword"),
            channel=entry.get("channel"),
            exclude=exclude_raw,
            lead_time_minutes=entry.get("lead_time_minutes", default_lead_time),
        ))
    return subs


def find_matches(programmes: list[Programme], subscriptions: list[Subscription]) -> list[Match]:
    """Return one Match per (programme, first-matching subscription)."""
    matches: list[Match] = []
    for prog in programmes:
        for sub in subscriptions:
            if sub.matches(prog):
                matches.append(Match(programme=prog, subscription=sub))
                break
    return matches


def group_matches(matches: list[Match]) -> list[GroupedMatch]:
    """
    Collapse matches that represent the same event on multiple channels into a
    single GroupedMatch, sorted by start time.
    """
    from collections import defaultdict

    # Key: (normalised title, start datetime, subscription label)
    buckets: dict[tuple, list[Match]] = defaultdict(list)
    for m in matches:
        key = (m.programme.title.lower(), m.programme.start, m.subscription.label)
        buckets[key].append(m)

    grouped: list[GroupedMatch] = []
    for ms in buckets.values():
        rep = ms[0].programme
        channels = [(m.programme.channel_number, m.programme.channel_name) for m in ms]
        grouped.append(GroupedMatch(
            title=rep.title,
            start=rep.start,
            stop=rep.stop,
            description=rep.description,
            categories=rep.categories,
            channels=channels,
            subscription=ms[0].subscription,
            group_uid=f"{rep.title.lower()}|{rep.start.isoformat()}",
        ))

    grouped.sort(key=lambda g: g.start)
    return grouped


def group_programmes(programmes: list[Programme]) -> list[dict]:
    """
    Collapse programmes with the same title and start time into one entry,
    merging channel info. Returns dicts suitable for template rendering.
    """
    from collections import defaultdict

    buckets: dict[tuple, list[Programme]] = defaultdict(list)
    for p in programmes:
        buckets[(p.title.lower(), p.start)].append(p)

    result: list[dict] = []
    for progs in buckets.values():
        rep = progs[0]
        result.append({
            "title": rep.title,
            "start": rep.start,
            "stop": rep.stop,
            "description": rep.description,
            "categories": rep.categories,
            "channels": [(p.channel_number, p.channel_name) for p in progs],
            # For the Subscribe panel: pre-fill with first category + first channel name
            "sport_hint": rep.categories[0] if rep.categories else "",
            "channel_hint": rep.channel_name if len(progs) == 1 else "",
        })

    result.sort(key=lambda x: x["start"])
    return result
