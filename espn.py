"""ESPN public scoreboard API — used to detect whether an EPG broadcast is live or a replay."""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Maps EPG category keywords → ESPN (sport, league) pairs, in priority order.
_LEAGUE_MAP: list[tuple[str, str, str]] = [
    ("american football",        "football",     "nfl"),
    ("college football",         "football",     "college-football"),
    ("basketball",               "basketball",   "nba"),
    ("college basketball",       "basketball",   "mens-college-basketball"),
    ("baseball",                 "baseball",     "mlb"),
    ("ice hockey",               "hockey",       "nhl"),
    ("hockey",                   "hockey",       "nhl"),
    ("mls",                      "soccer",       "usa.1"),
    ("premier league",           "soccer",       "eng.1"),
    ("la liga",                  "soccer",       "esp.1"),
    ("bundesliga",               "soccer",       "ger.1"),
    ("serie a",                  "soccer",       "ita.1"),
    ("ligue 1",                  "soccer",       "fra.1"),
    ("champions league",         "soccer",       "uefa.champions"),
    ("soccer",                   "soccer",       "usa.1"),
    ("football",                 "football",     "nfl"),  # generic fallback
]

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_STOP = {"at", "vs", "the", "city", "fc", "sc", "afc", "nfc", "nfl", "nba",
         "mlb", "nhl", "mls", "and", "de", "united"}

# Simple TTL cache: key → (fetched_at, data)
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 900  # 15 minutes


def _leagues_for_categories(categories: list[str]) -> list[tuple[str, str]]:
    cats = " ".join(categories).lower()
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for keyword, sport, league in _LEAGUE_MAP:
        if keyword in cats and (sport, league) not in seen:
            result.append((sport, league))
            seen.add((sport, league))
    return result


def _sig_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOP}


def _matches_event(epg_title: str, espn_name: str, competitors: list[str]) -> bool:
    """True if the EPG title likely refers to the same game as the ESPN event."""
    epg = _sig_words(epg_title)
    if not epg:
        return False

    # Both competitor names have at least one significant word in the EPG title
    if len(competitors) >= 2:
        hits = sum(bool(epg & _sig_words(c)) for c in competitors)
        if hits >= 2:
            return True
        # Team subscription: one competitor with 2+ shared words
        if any(len(epg & _sig_words(c)) >= 2 for c in competitors):
            return True

    # Fallback: 2+ shared words with the full ESPN event name
    return len(epg & _sig_words(espn_name)) >= 2


def _fetch_events(sport: str, league: str, date_from: datetime, date_to: datetime) -> list[dict]:
    """Fetch and cache ESPN scoreboard events for a date range."""
    from_s = date_from.strftime("%Y%m%d")
    to_s = date_to.strftime("%Y%m%d")
    cache_key = f"{sport}/{league}/{from_s}-{to_s}"

    now = time.monotonic()
    if cache_key in _cache:
        fetched_at, data = _cache[cache_key]
        if now - fetched_at < _CACHE_TTL:
            return data

    url = f"{_ESPN_BASE}/{sport}/{league}/scoreboard"
    try:
        resp = requests.get(url, params={"dates": f"{from_s}-{to_s}", "limit": 200},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        log.warning("ESPN API %s/%s: %s", sport, league, exc)
        return []

    events: list[dict] = []
    for ev in raw.get("events", []):
        comps = ev.get("competitions", [{}])[0].get("competitors", [])
        competitor_names = [c.get("team", {}).get("displayName", "") for c in comps]

        try:
            ev_date = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue

        status = ev.get("status", {}).get("type", {})
        events.append({
            "name":        ev.get("name", ""),
            "date":        ev_date,
            "state":       status.get("state", "pre"),   # "pre" | "in" | "post"
            "completed":   status.get("completed", False),
            "competitors": [n for n in competitor_names if n],
        })

    _cache[cache_key] = (now, events)
    log.debug("ESPN %s/%s: %d events (%s→%s)", sport, league, len(events), from_s, to_s)
    return events


def check_replay(epg_title: str, epg_start: datetime, categories: list[str]) -> Optional[bool]:
    """
    Returns:
        True  — ESPN confirms this is a replay of a completed game.
        False — ESPN confirms this is a live / upcoming broadcast.
        None  — ESPN has no data for this sport, or no matching event found;
                caller should assume live (fail open).

    Strategy: check for any live or scheduled event first. If ESPN shows a
    matching game as upcoming/live, it's a real broadcast. Only when all
    matching events are completed do we check the time gap — >4 hours after
    game start means replay.
    """
    leagues = _leagues_for_categories(categories)
    if not leagues:
        log.info("ESPN: no leagues mapped for categories %s — skipping check for '%s'", categories, epg_title)
        return None

    # Look back 14 days (replays can air weeks later) and forward 7 days
    search_from = epg_start - timedelta(days=14)
    search_to = epg_start + timedelta(days=7)

    for sport, league in leagues:
        matched = [
            ev for ev in _fetch_events(sport, league, search_from, search_to)
            if _matches_event(epg_title, ev["name"], ev["competitors"])
        ]
        if not matched:
            continue

        # If ESPN has a live or upcoming game matching this title, it's a real broadcast
        live_or_upcoming = [ev for ev in matched if ev["state"] in ("pre", "in")]
        if live_or_upcoming:
            closest_live = min(
                live_or_upcoming,
                key=lambda ev: abs((epg_start - ev["date"]).total_seconds()),
            )
            log.info("ESPN: LIVE '%s' | confirmed %s game %s", epg_title,
                     closest_live["state"], closest_live["date"].strftime("%Y-%m-%d %H:%M"))
            return False

        # Only completed games found — determine if this is a replay
        completed = [ev for ev in matched if ev["state"] == "post" and ev["completed"]]
        if not completed:
            log.info("ESPN: unknown state for '%s' — allowing", epg_title)
            return False  # Fail open

        closest = min(completed, key=lambda ev: abs((epg_start - ev["date"]).total_seconds()))
        time_gap = epg_start - closest["date"]

        replay = time_gap > timedelta(hours=4)
        log.info(
            "ESPN: %s '%s' | game %s | EPG %s | gap %.1fh",
            "REPLAY" if replay else "LIVE (same-night)",
            epg_title,
            closest["date"].strftime("%Y-%m-%d %H:%M"),
            epg_start.strftime("%Y-%m-%d %H:%M"),
            time_gap.total_seconds() / 3600,
        )
        return replay

    log.info("ESPN: no matching event found for '%s' — allowing", epg_title)
    return None  # No ESPN match found — fail open (allow notification)


def get_espn_state(epg_title: str, epg_start: datetime, categories: list[str]) -> Optional[str]:
    """
    Return the ESPN status state for a matched event: 'pre', 'in', or 'post'.
    Returns None when ESPN has no data for this sport or no matching event.
    """
    leagues = _leagues_for_categories(categories)
    if not leagues:
        return None

    search_from = epg_start - timedelta(days=1)
    search_to = epg_start + timedelta(days=1)

    for sport, league in leagues:
        matched = [
            ev for ev in _fetch_events(sport, league, search_from, search_to)
            if _matches_event(epg_title, ev["name"], ev["competitors"])
        ]
        if matched:
            closest = min(matched, key=lambda ev: abs((epg_start - ev["date"]).total_seconds()))
            return closest["state"]

    return None


def filter_replays(grouped: list, cfg: dict) -> list:
    """Filter/tag replay entries when espn_verify is enabled.

    If espn_notify_replays is also true, replays are kept but flagged with
    g.is_replay = True so callers can label the notification accordingly.
    """
    if not cfg.get("espn_verify"):
        log.info("ESPN verify OFF — replay filter skipped (%d events pass through)", len(grouped))
        return grouped

    notify_replays = cfg.get("espn_notify_replays", False)
    log.info("ESPN verify ON | notify_replays=%s | checking %d events", notify_replays, len(grouped))
    out = []
    for g in grouped:
        result = check_replay(g.title, g.start, g.categories)
        if result is True:
            if notify_replays:
                g.is_replay = True
                out.append(g)
                log.info("Replay — notifying with [REPLAY] tag: %s @ %s", g.title,
                         g.start.astimezone().strftime("%-I:%M %p %a %b %-d"))
            else:
                log.info("Replay — suppressing notification: %s @ %s", g.title,
                         g.start.astimezone().strftime("%-I:%M %p %a %b %-d"))
        else:
            out.append(g)
    return out
