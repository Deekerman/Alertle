"""Match EPG programmes against user subscriptions."""

import logging
import re
import signal
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
    """Significant words from a title (length ≥ 3, not stop words)."""
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


def _safe_match(pattern: re.Pattern, text: str, label: str) -> bool:
    """Run regex with a 1-second timeout to guard against ReDoS."""
    def _handler(signum, frame):
        raise TimeoutError
    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(1)
    try:
        return bool(pattern.search(text))
    except TimeoutError:
        log.warning("Regex timeout in subscription '%s' — pattern may cause ReDoS", label)
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@dataclass
class Subscription:
    label: str
    sport: Optional[str] = None
    team: Optional[str] = None
    keyword: Optional[str] = None
    channel: Optional[str] = None
    title_pattern: Optional[str] = None    # regex matched against programme title only
    subtitle_pattern: Optional[str] = None # regex matched against programme sub-title only
    desc_pattern: Optional[str] = None     # regex matched against programme description only
    exclude: list[str] = field(default_factory=list)
    require_sport: bool = False
    lead_time_minutes: int = 30
    notify_channels: list[str] = field(default_factory=list)  # empty = all enabled
    notif_title_template: Optional[str] = None  # None = use global default
    notif_body_template: Optional[str] = None
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
        # ── Category / sport checks (no text building needed) ──────────
        if self.sport:
            if not any(self.sport.lower() in cat.lower() for cat in prog.categories):
                return False

        # require_sport only adds value for keyword/channel/pattern subs;
        # team matching already enforces sport category for non-vs titles.
        if self.require_sport and not self.sport and not self.team:
            if not _has_sport_category(prog):
                return False

        # ── Team matching ──────────────────────────────────────────────
        if self.team:
            team_l = self.team.lower()
            teams = extract_teams(prog.title)
            if teams:
                # "A vs B" title — team must be one of the two sides
                if not any(team_l in t.lower() for t in teams):
                    return False
            else:
                # Non-vs title — require sport category to avoid reality shows etc.
                if not _has_sport_category(prog):
                    return False
                if team_l not in f"{prog.title} {prog.description}".lower():
                    return False

        # ── Channel matching ───────────────────────────────────────────
        if self.channel:
            ch = self.channel.lower()
            if ch not in prog.channel_name.lower() and ch not in prog.channel_id.lower():
                return False

        # ── Text-based checks ─────────────────────────────────────────
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
            title_pattern=entry.get("title_pattern"),
            subtitle_pattern=entry.get("subtitle_pattern"),
            desc_pattern=entry.get("desc_pattern"),
            exclude=exclude_raw,
            require_sport=bool(entry.get("require_sport", False)),
            lead_time_minutes=entry.get("lead_time_minutes", default_lead_time),
            notify_channels=entry.get("notify_channels", []),
            notif_title_template=entry.get("notif_title_template") or None,
            notif_body_template=entry.get("notif_body_template") or None,
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
    exact: dict[tuple, list[Match]] = defaultdict(list)
    for m in matches:
        key = (m.programme.title.lower(), m.programme.start, m.subscription.label)
        exact[key].append(m)

    merged: list[list[Match]] = []
    for ms in exact.values():
        rep = ms[0]
        start_min = rep.programme.start.replace(second=0, microsecond=0)
        sub_label = rep.subscription.label
        placed = False
        for group in merged:
            g_rep = group[0]
            if (
                g_rep.subscription.label == sub_label
                and g_rep.programme.start.replace(second=0, microsecond=0) == start_min
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
        main_title = min((m.programme.title for m in ms), key=len)
        cats = filter_categories(rep.categories)
        channels = sorted(
            [(m.programme.channel_number, m.programme.channel_name) for m in ms],
            key=lambda c: (float(c[0]) if c[0].replace(".", "").isdigit() else float("inf"), c[1]),
        )
        subtitle = next((m.programme.subtitle for m in ms if m.programme.subtitle), "")
        grouped.append(GroupedMatch(
            title=main_title,
            start=rep.start,
            stop=rep.stop,
            subtitle=subtitle,
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
            })

    result.sort(key=lambda x: x["start"])
    return result
