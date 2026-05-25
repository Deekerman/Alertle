"""Build game thumbnail image URLs from matched EPG events."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from matcher import extract_teams

if TYPE_CHECKING:
    from matcher import GroupedMatch

_CATEGORY_LEAGUE_MAP = [
    ("american football", "NFL"),
    ("college football",  "NCAAF"),
    ("basketball",        "NBA"),
    ("baseball",          "MLB"),
    ("ice hockey",        "NHL"),
    ("hockey",            "NHL"),
    ("premier league",    "EPL"),
    ("champions league",  "UEFA"),
    ("soccer",            "MLS"),
]

_DEFAULT_PATH = (
    "/{league_code}/{away_team_pascal}/{home_team_pascal}"
    "/thumb.png?style=1&logo=true&aspect=16-9"
)


def _to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"\s+", name.strip()) if w)


def _league_code(game: "GroupedMatch") -> Optional[str]:
    if game.subscription.espn_league:
        return game.subscription.espn_league.upper()
    cats = " ".join(game.categories).lower()
    for keyword, code in _CATEGORY_LEAGUE_MAP:
        if keyword in cats:
            return code
    return None


def build_thumb_url(game: "GroupedMatch", cfg: dict) -> Optional[str]:
    thumbs = cfg.get("game_thumbs", {})
    if not thumbs.get("enabled"):
        return None
    base_url = thumbs.get("base_url", "").rstrip("/")
    if not base_url:
        return None
    path_tpl = thumbs.get("path", _DEFAULT_PATH)
    league = _league_code(game)
    if not league:
        return None
    teams = extract_teams(game.title)
    if not teams:
        return None
    path = path_tpl.format(
        league_code=league,
        away_team_pascal=_to_pascal(teams[0]),
        home_team_pascal=_to_pascal(teams[1]),
    )
    return base_url + path
