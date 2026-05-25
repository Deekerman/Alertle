"""Build game thumbnail image URLs from matched EPG events."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

from matcher import extract_teams

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from matcher import GroupedMatch

# ESPN league code → Game-Thumbs league code (all lowercase)
_ESPN_TO_THUMBS_LEAGUE = {
    "nfl":                      "nfl",
    "college-football":         "ncaaf",
    "nba":                      "nba",
    "mens-college-basketball":  "ncaab",
    "mlb":                      "mlb",
    "nhl":                      "nhl",
    "usa.1":                    "mls",
    "eng.1":                    "epl",
    "esp.1":                    "liga",
    "ger.1":                    "bundesliga",
    "ita.1":                    "seriea",
    "fra.1":                    "ligue1",
    "uefa.champions":           "ucl",
}

# Category keyword fallback when no ESPN data is available
_CATEGORY_LEAGUE_MAP = [
    ("american football", "nfl"),
    ("college football",  "ncaaf"),
    ("basketball",        "nba"),
    ("baseball",          "mlb"),
    ("ice hockey",        "nhl"),
    ("hockey",            "nhl"),
    ("premier league",    "epl"),
    ("champions league",  "ucl"),
    ("soccer",            "mls"),
]

_DEFAULT_PATH = (
    "/{league_code}/{away_team_pascal}/{home_team_pascal}"
    "/thumb.png?style=1&logo=true&aspect=16-9"
)


def _to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"\s+", name.strip()) if w)


def build_thumb_url(game: "GroupedMatch", cfg: dict) -> Optional[str]:
    thumbs = cfg.get("game_thumbs", {})
    if not thumbs.get("enabled"):
        return None
    base_url = thumbs.get("base_url", "").rstrip("/")
    if not base_url:
        log.warning("game_thumbs enabled but base_url not configured")
        return None
    path_tpl = thumbs.get("path", _DEFAULT_PATH)

    # League: subscription ESPN league → thumbs code, then API-discovered, then categories
    espn_league = getattr(game.subscription, "espn_league", None) or game.espn_league_code
    if espn_league:
        league = _ESPN_TO_THUMBS_LEAGUE.get(espn_league, espn_league)
    else:
        cats = " ".join(game.categories).lower()
        league = next((code for kw, code in _CATEGORY_LEAGUE_MAP if kw in cats), None)
    if not league:
        log.info("build_thumb_url: no league for '%s' (espn_league=%s categories=%s)",
                 game.title, getattr(game.subscription, "espn_league", None), game.categories)
        return None

    # Teams: prefer ESPN abbreviation/name, fall back to EPG title parsing
    away = game.espn_away
    home = game.espn_home
    if not away or not home:
        teams = extract_teams(game.title)
        if not teams:
            log.info("build_thumb_url: no teams for '%s' (espn_away=%s espn_home=%s)",
                     game.title, game.espn_away, game.espn_home)
            return None
        away, home = _to_pascal(teams[0]), _to_pascal(teams[1])

    path = path_tpl.format(
        league_code=league,
        away_team_pascal=quote(away, safe=""),
        home_team_pascal=quote(home, safe=""),
    )
    url = base_url + path
    log.info("build_thumb_url: %s", url)
    return url
