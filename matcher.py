"""Match EPG programmes against user subscriptions."""

import concurrent.futures
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from epg_scanner import Programme

log = logging.getLogger(__name__)

_VS_SEP = re.compile(r'\s+(?:vs?\.?|@)\s+', re.IGNORECASE)
_TRAILING = re.compile(r'\s*[-–(].*$')

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
    """Significant words from a title (length >= 3, not stop words)."""
    words = re.findall(r"[a-z0-9']{3,}", title.lower())
    return frozenset(w for w in words if w not in _STOP_WORDS)


def _titles_related(t1: str, t2: str) -> bool:
    """True if the two titles likely refer to the same event."""
    if t1.lower() == t2.lower():
        return True
    if t1.lower() in t2.lower() or t2.lower() in t1.lower():
        return True
    sig1 = _title_sig_words(t1)
    sig2 = _title_sig_words(t2)
    return len(sig1 & sig2) >= 2


def _has_sport_category(prog: Programme) -> bool:
    return any("sport" in cat.lower() for cat in prog.categories)


def filter_categories(cats: list[str]) -> list[str]:
    """Remove categories that are generic prefixes of a more specific one in the same list."""
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


_regex_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="regex")


def _safe_match(pattern: re.Pattern, text: str, label: str) -> bool:
    """Run regex in a worker thread with a 1-second timeout to guard against ReDoS."""
    future = _regex_pool.submit(pattern.search, text)
    try:
        return bool(future.result(timeout=1.0))
    except concurrent.futures.TimeoutError:
        log.warning("Regex timeout in subscription '%s' -- pattern may cause ReDoS", label)
        return False
    except Exception as exc:
        log.warning("Regex error in subscription '%s': %s", label, exc)
        return False


@dataclass
class Subscription:
    label: str
    sport: Optional[str] = None
    team: Optional[str] = None
    keyword: Optional[str] = None
    channel: Optional[str] = None
    title_pattern: Optional[str] = None
    subtitle_pattern: Optional[str] = None
    desc_pattern: Optional[str] = None
    exclude: list[str] = field(default_factory=list)
    require_sport: bool = False
    lead_time_minutes: int = 30
    notify_channels: list[str] = field(default_factory=list)
    notif_title_template: Optional[str] = None
    notif_body_template: Optional[str] = None
    espn_sport: Optional[str] = None
    espn_league: Optional[str] = None
    espn_team: Optional[str] = None
    game_thumbs_league: Optional[str] = None
    require_live: bool = False
    enabled: bool = True
    notify_on_start: bool = False
    start_lead_time_minutes: int = 5
    _title_re: Optional[re.Pattern] = field(default=None, init=False, repr=False, compare=False)
    _subtitle_re: Optional[re.Pattern] = field(default=None, init=False, repr=False, compare=False)
    _desc_re: Optional[re.Pattern] = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        self._title_re = self._compile(self.title_pattern)
        self._subtitle_re = self._compile(self.subtitle_pattern)
        self._desc_re = self._compile(self.desc_pattern)

    def _compile(self, pattern: Optional[str]) -> Optional[re.Pattern]:
        if not pattern:
            return None
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            log.warning("Invalid regex in subscription '%s': %s", self.label, exc)
            return None

    def matches(self, prog: Programme) -> bool:
        if self.sport:
            if not any(self.sport.lower() in cat.lower() for cat in prog.categories):
                return False

        if self.require_sport and not self.sport and not self.team:
            if not _has_sport_category(prog):
                return False

        if self.team:
            team_l = self.team.lower()
            teams = extract_teams(prog.title)
            if teams:
                if not any(team_l in t.lower() for t in teams):
                    return False
            else:
                if not _has_sport_category(prog):
                    return False
                if team_l not in f"{prog.title} {prog.description}".lower():
                    return False

        if self.channel:
            ch = self.channel.lower()
            if ch not in prog.channel_name.lower() and ch not in prog.channel_id.lower():
                return False

        if self.keyword:
            text = f"{prog.title} {prog.description}".lower()
            if self.keyword.lower() not in text:
                return False

        if self._title_re and not _safe_match(self._title_re, prog.title, self.label):
            return False

        if self._subtitle_re and not _safe_match(self._subtitle_re, prog.subtitle, self.label):
            return False

        if self._desc_re and not _safe_match(self._desc_re, prog.description, self.label):
            return False

        if self.exclude:
            text = f"{prog.title} {prog.description}".lower()
            for term in self.exclude:
                t = term.strip().lower()
                if t and t in text:
                    return False

        if self.require_live and not prog.is_live:
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
    subtitle: str
    description: str
    categories: list[str]
    channels: list[tuple[str, str]]
    subscription: Subscription
    group_uid: str
    is_replay: bool = False
    espn_start: Optional[object] = None
    extra_games: list["GroupedMatch"] = field(default_factory=list)


def build_subscriptions(raw: list[dict], default_lead_time: int) -> list[Subscription]:
    subs: list[Subscription] = []
    for entry in (raw or []):
        exclude_raw = entry.get("exclude", [])
        if isinstance(exclude_raw, str):
            exclude_raw = [x.strip() for x in exclude_raw.split(",") if x.strip()]
        subs.append(Subscription(
            label=entry.get("label", "Unnamed"),
            sport=entry.get("sport"),
            team=entry.get("team"),
            keyword=entry.get("keyword"),
            channel=entry.get("channel"),
            title_pattern=entry.get("title_pattern"),
            subtitle_pattern=entry.get("subtitle_pattern"),
            desc_pattern=entry.get("desc_pattern"),
            exclude=exclude_raw,
            require_sport=bool(entry.get("require_sport", False)),
            lead_time_minutes=entry.get("lead_time_minutes", default_lead_time),
            notify_channels=entry.get("notify_channels", []),
            notif_title_template=entry.get("notif_title_template") or None,
            notif_body_template=entry.get("notif_body_template") or None,
            espn_sport=entry.get("espn_sport") or None,
            espn_league=entry.get("espn_league") or None,
            espn_team=entry.get("espn_team") or None,
            game_thumbs_league=entry.get("game_thumbs_league") or None,
            require_live=bool(entry.get("require_live", False)),
            enabled=bool(entry.get("enabled", True)),
            notify_on_start=bool(entry.get("notify_on_start", False)),
            start_lead_time_minutes=entry.get("start_lead_time_minutes", 5),
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


def group_matches(matches: list[Match], grace_window_minutes: int = 20) -> list[GroupedMatch]:
    """Collapse matches for the same event (same start +/- grace window + related title) into one GroupedMatch."""
    grace_secs = grace_window_minutes * 60

    exact: dict[tuple, list[Match]] = defaultdict(list)
    for m in matches:
        key = (m.programme.title.lower(), m.programme.subtitle.lower(), m.programme.start, m.subscription.label)
        exact[key].append(m)

    merged: list[list[Match]] = []
    for ms in exact.values():
        rep = ms[0]
        sub_label = rep.subscription.label
        placed = False
        for group in merged:
            g_rep = group[0]
            time_diff = abs((g_rep.programme.start - rep.programme.start).total_seconds())
            if (
                g_rep.subscription.label == sub_label
                and time_diff <= grace_secs
                and _titles_related(g_rep.programme.title, rep.programme.title)
            ):
                group.extend(ms)
                placed = True
                break
        if not placed:
            merged.append(list(ms))

    grouped: list[GroupedMatch] = []
    for ms in merged:
        main_title = min((m.programme.title for m in ms), key=len)
        earliest_start = min(m.programme.start for m in ms)
        rep = min(ms, key=lambda m: m.programme.start).programme
        cats = filter_categories(rep.categories)
        channels = sorted(
            {(m.programme.channel_number, m.programme.channel_name) for m in ms},
            key=lambda c: (float(c[0]) if c[0].replace(".", "").isdigit() else float("inf"), c[1]),
        )
        subtitle = next((m.programme.subtitle for m in ms if m.programme.subtitle), "")
        grouped.append(GroupedMatch(
            title=main_title,
            start=earliest_start,
            stop=rep.stop,
            subtitle=subtitle,
            description=rep.description,
            categories=cats,
            channels=channels,
            subscription=ms[0].subscription,
            group_uid=f"{main_title.lower()}|{subtitle.lower()}|{earliest_start.replace(second=0,microsecond=0).isoformat()}",
        ))

    grouped.sort(key=lambda g: g.start)
    return grouped


def consolidate_notifications(groups: list[GroupedMatch], grace_window_minutes: int = 20) -> list[GroupedMatch]:
    """Merge GroupedMatches from the same subscription + time window into one notification.

    Each consolidated entry's extra_games holds the additional games so the
    formatter can list them all in a single message body.
    """
    grace_secs = grace_window_minutes * 60
    result: list[GroupedMatch] = []
    for g in groups:
        placed = False
        for primary in result:
            if (
                primary.subscription.label == g.subscription.label
                and _titles_related(primary.title, g.title)
                and abs((primary.start - g.start).total_seconds()) <= grace_secs
            ):
                primary.extra_games.append(g)
                placed = True
                break
        if not placed:
            result.append(g)
    return result


def group_programmes(programmes: list[Programme]) -> list[dict]:
    """
    Collapse programmes with related titles at the same start minute into one
    entry, merging channel info. Returns dicts for template rendering.
    """
    by_minute: dict[object, list[Programme]] = defaultdict(list)
    for p in programmes:
        by_minute[p.start.replace(second=0, microsecond=0)].append(p)

    result: list[dict] = []
    for progs in by_minute.values():
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
            sport_hint = next(
                (c for c in cats if "sport" not in c.lower()),
                cats[0] if cats else "",
            )
            subtitle = next((p.subtitle for p in g if p.subtitle), "")
            result.append({
                "title": min((p.title for p in g), key=len),
                "start": rep.start,
                "stop": rep.stop,
                "subtitle": subtitle,
                "description": rep.description,
                "categories": cats,
                "channels": [(p.channel_number, p.channel_name) for p in g],
                "sport_hint": sport_hint,
                "channel_hint": rep.channel_name if len(g) == 1 else "",
                "is_live": any(p.is_live for p in g),
            })

    result.sort(key=lambda x: x["start"])
    return result
