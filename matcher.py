"""Match EPG programmes against user subscriptions."""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from epg_scanner import Programme

log = logging.getLogger(__name__)

_VS_SEP = re.compile(r'\s+(?:vs?\.?|@)\s+', re.IGNORECASE)
_TRAILING = re.compile(r'\s*[-–(].*$')

# Words too generic to use for fuzzy title grouping
_STOP_WORDS = {
    "the", "from", "live", "at", "in", "on", "a", "an", "and", "of", "to",
    "with", "for", "is", "its", "into", "week", "day", "game", "match",
    "show", "tour", "cup", "open", "season", "episode", "special", "preview",
    "coverage", "highlights", "replay", "extended",
}


def extract_teams(title: str) -> tuple[str, str] | None:
    """Return (team1, team2) if title looks like 'A vs B', else None."""
    parts = _VS_SEP.split(title.strip(), maxsplit=1)
    if len(parts) == 2:
        t1 = _TRAILING.sub("", parts[0]).strip()
        t2 = _TRAILING.sub("", parts[1]).strip()
        if t1 and t2:
            return t1, t2
    return None


def _title_sig_words(title: str) -> frozenset[str]:
    """Significant words from a title (length ≥ 3, not stop words)."""
    words = re.findall(r"[a-z0-9']{3,}", title.lower())
    return frozenset(w for w in words if w not in _STOP_WORDS)


def _titles_related(t1: str, t2: str) -> bool:
    """True if the two titles likely refer to the same event."""
    if t1.lower() == t2.lower():
        return True
    # One is a substring of the other (e.g. "PGA Championship" in "Live From the PGA Championship")
    if t1.lower() in t2.lower() or t2.lower() in t1.lower():
        return True
    # Share at least 2 significant words
    sig1 = _title_sig_words(t1)
    sig2 = _title_sig_words(t2)
    return len(sig1 & sig2) >= 2


def filter_categories(cats: list[str]) -> list[str]:
    """
    Remove categories that are generic prefixes of a more specific one in the
    same list.  E.g. if both "Sports" and "Sports event" exist, drop "Sports".
    """
    result = []
    for cat in cats:
        cat_l = cat.lower()
        dominated = any(
            other.lower() != cat_l and other.lower().startswith(cat_l)
            for other in cats
        )
        if not dominated:
            result.append(cat)
    return result


@dataclass
class Subscription:
    label: str
    sport: Optional[str] = None
    team: Optional[str] = None
    keyword: Optional[str] = None
    channel: Optional[str] = None
    exclude: list[str] = field(default_factory=list)
    require_sport: bool = False   # if True, programme must have a sports-related category
    lead_time_minutes: int = 30

    def matches(self, prog: Programme) -> bool:
        # ── Positive filters ──────────────────────────────────────────
        if self.sport:
            if not any(self.sport.lower() in cat.lower() for cat in prog.categories):
                return False

        if self.require_sport and not self.sport:
            if not any("sport" in cat.lower() for cat in prog.categories):
                return False

        text = f"{prog.title} {prog.description}".lower()

        if self.team:
            team_l = self.team.lower()
            if team_l not in text:
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

        # ── Negative filters ──────────────────────────────────────────
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
    title: str
    start: object
    stop: object
    description: str
    categories: list[str]
    channels: list[tuple[str, str]]
    subscription: Subscription
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
            require_sport=bool(entry.get("require_sport", False)),
            lead_time_minutes=entry.get("lead_time_minutes", default_lead_time),
        ))
    return subs


def find_matches(programmes: list[Programme], subscriptions: list[Subscription]) -> list[Match]:
    matches: list[Match] = []
    for prog in programmes:
        for sub in subscriptions:
            if sub.matches(prog):
                matches.append(Match(programme=prog, subscription=sub))
                break
    return matches


def group_matches(matches: list[Match]) -> list[GroupedMatch]:
    """Collapse matches for the same event (same start + related title) into one GroupedMatch."""
    # First bucket by exact (title, start, subscription)
    exact: dict[tuple, list[Match]] = defaultdict(list)
    for m in matches:
        key = (m.programme.title.lower(), m.programme.start, m.subscription.label)
        exact[key].append(m)

    # Then merge buckets whose titles are related and share the same start minute
    merged: list[list[Match]] = []
    for ms in exact.values():
        rep = ms[0]
        start_min = rep.programme.start.replace(second=0, microsecond=0)
        sub_label = rep.subscription.label
        placed = False
        for group in merged:
            g_rep = group[0]
            g_start_min = g_rep.programme.start.replace(second=0, microsecond=0)
            if (
                g_rep.subscription.label == sub_label
                and g_start_min == start_min
                and _titles_related(g_rep.programme.title, rep.programme.title)
            ):
                group.extend(ms)
                placed = True
                break
        if not placed:
            merged.append(list(ms))

    grouped: list[GroupedMatch] = []
    for ms in merged:
        rep = ms[0].programme
        # Use the shortest title as representative (most likely the "main" event title)
        titles = list({m.programme.title for m in ms})
        main_title = min(titles, key=len)
        cats = filter_categories(rep.categories)
        channels = [(m.programme.channel_number, m.programme.channel_name) for m in ms]
        grouped.append(GroupedMatch(
            title=main_title,
            start=rep.start,
            stop=rep.stop,
            description=rep.description,
            categories=cats,
            channels=channels,
            subscription=ms[0].subscription,
            group_uid=f"{main_title.lower()}|{rep.start.replace(second=0,microsecond=0).isoformat()}",
        ))

    grouped.sort(key=lambda g: g.start)
    return grouped


def group_programmes(programmes: list[Programme]) -> list[dict]:
    """
    Collapse programmes with related titles at the same start minute into one
    entry, merging channel info. Returns dicts for template rendering.
    """
    # Step 1: bucket by start minute
    by_minute: dict[object, list[Programme]] = defaultdict(list)
    for p in programmes:
        by_minute[p.start.replace(second=0, microsecond=0)].append(p)

    # Step 2: within each minute, merge related titles using union-find
    result: list[dict] = []
    for progs in by_minute.values():
        # Build groups via greedy merge
        groups: list[list[Programme]] = []
        for p in progs:
            placed = False
            for g in groups:
                if _titles_related(g[0].title, p.title):
                    g.append(p)
                    placed = True
                    break
            if not placed:
                groups.append([p])

        for g in groups:
            rep = g[0]
            cats = filter_categories(rep.categories)
            # Pick most specific sport hint (longest category that isn't generic)
            sport_hint = next(
                (c for c in cats if "sport" not in c.lower()),
                cats[0] if cats else "",
            )
            result.append({
                "title": min((p.title for p in g), key=len),  # shortest = cleanest title
                "start": rep.start,
                "stop": rep.stop,
                "description": rep.description,
                "categories": cats,
                "channels": [(p.channel_number, p.channel_name) for p in g],
                "sport_hint": sport_hint,
                "channel_hint": rep.channel_name if len(g) == 1 else "",
            })

    result.sort(key=lambda x: x["start"])
    return result
