"""Build Game-Thumbs image URLs from EPG match titles."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matcher import GroupedMatch

log = logging.getLogger(__name__)

# Separators used in sports titles: "vs", "vs.", "v.", "@", "at" (away at home)
_TEAM_SEP    = re.compile(r'\s+(?:vs?\.?|@|at)\s+', re.IGNORECASE)
_TRAILING    = re.compile(r'\s*[-–(].*$')   # strip trailing dash/paren noise
_SHOW_PREFIX = re.compile(r'^[^:]+:\s+')    # strip "Show Name: " prefix


def _extract_game_teams(text: str) -> tuple[str, str] | None:
    """Return (away, home) or None.

    Handles 'at', 'vs', 'vs.', '@' separators and strips leading show-name
    prefixes like 'NHL on ESPN: ' so the team name is clean.
    """
    parts = _TEAM_SEP.split(text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    t1 = _SHOW_PREFIX.sub("", _TRAILING.sub("", parts[0])).strip()
    t2 = _SHOW_PREFIX.sub("", _TRAILING.sub("", parts[1])).strip()
    return (t1, t2) if t1 and t2 else None


def _to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"\s+", name.strip()) if w)


def build_thumb_url(game: "GroupedMatch", cfg: dict) -> str:
    """Return a Game-Thumbs URL, or '' if teams/league/config unavailable."""
    thumbs_cfg = cfg.get("game_thumbs", {})
    if not thumbs_cfg.get("enabled"):
        return ""
    base_url = thumbs_cfg.get("base_url", "").rstrip("/")
    if not base_url:
        log.warning("game_thumbs enabled but base_url not set in Settings")
        return ""
    league_code = getattr(game.subscription, "game_thumbs_league", None)
    if not league_code:
        log.debug("build_thumb_url: no game_thumbs_league on subscription '%s'",
                  game.subscription.label)
        return ""
    teams = _extract_game_teams(game.title)
    if not teams and game.subtitle:
        teams = _extract_game_teams(game.subtitle)
    if not teams:
        log.info("build_thumb_url: could not extract teams from '%s' / '%s'",
                 game.title, game.subtitle or "")
        return ""
    away = _to_pascal(teams[0])
    home = _to_pascal(teams[1])
    url = f"{base_url}/{league_code}/{away}/{home}/thumb.png?style=1&logo=true&aspect=16-9"
    log.info("build_thumb_url: %s", url)
    return url
