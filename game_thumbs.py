"""Build Game-Thumbs image URLs from EPG match titles."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from matcher import extract_teams

if TYPE_CHECKING:
    from matcher import GroupedMatch


def _to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"\s+", name.strip()) if w)


def build_thumb_url(game: "GroupedMatch", cfg: dict) -> str:
    """Return a Game-Thumbs URL, or '' if teams/league/config unavailable."""
    thumbs_cfg = cfg.get("game_thumbs", {})
    if not thumbs_cfg.get("enabled"):
        return ""
    base_url = thumbs_cfg.get("base_url", "").rstrip("/")
    if not base_url:
        return ""
    league_code = getattr(game.subscription, "game_thumbs_league", None)
    if not league_code:
        return ""
    teams = extract_teams(game.title)
    if not teams and game.subtitle:
        teams = extract_teams(game.subtitle)
    if not teams:
        return ""
    away = _to_pascal(teams[0])
    home = _to_pascal(teams[1])
    return f"{base_url}/{league_code}/{away}/{home}/thumb.png?style=1&logo=true&aspect=16-9"
