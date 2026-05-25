"""Base notifier interface and shared message formatting."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matcher import GroupedMatch

DEFAULT_TITLE_TEMPLATE = "[{rule}] {title}"
DEFAULT_BODY_TEMPLATE = "{description}\nTime: {time}\nChannel(s):\n{channels}"

_AVAILABLE_VARS = (
    "{rule}", "{title}", "{subtitle}", "{time}", "{date}",
    "{channels}", "{duration}", "{description}", "{thumb_url}",
)


def build_preview_vars(g: "GroupedMatch", show_channel_nums: bool = False,
                       thumb_url: str = "") -> dict:
    local_start = g.start.astimezone()
    if show_channel_nums:
        ch_parts = [f"{num} - {name}" if num else name for num, name in g.channels]
    else:
        ch_parts = [name for _, name in g.channels]
    return {
        "title": g.title,
        "subtitle": g.subtitle or "",
        "time": local_start.strftime("%a %b %-d  %-I:%M %p"),
        "date": local_start.strftime("%a %b %-d"),
        "channels": "\n".join(ch_parts),
        "rule": g.subscription.label,
        "duration": str(int((g.stop - g.start).total_seconds() / 60)),
        "description": g.description or "",
        "thumb_url": thumb_url,
    }


def render_template(template: str, vars_: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: vars_.get(m.group(1), m.group(0)), template)


def format_grouped_message(
    games: "list[GroupedMatch]",
    title_tpl: str = DEFAULT_TITLE_TEMPLATE,
    body_tpl: str = DEFAULT_BODY_TEMPLATE,
    show_channel_nums: bool = False,
    thumb_url: str = "",
) -> tuple[str, str]:
    if len(games) == 1:
        vars_ = build_preview_vars(games[0], show_channel_nums=show_channel_nums,
                                   thumb_url=thumb_url)
        return render_template(title_tpl, vars_), render_template(body_tpl, vars_).rstrip()

    # Multi-game: title from first game's vars, body lists each game
    vars_ = build_preview_vars(games[0], show_channel_nums=show_channel_nums,
                               thumb_url=thumb_url)
    title_str = render_template(title_tpl, vars_) + f" — {len(games)} games"

    lines = []
    for game in games:
        local = game.start.astimezone()
        if show_channel_nums:
            ch_parts = [f"{n} - {name}" if n else name for n, name in game.channels]
        else:
            ch_parts = [name for _, name in game.channels]
        duration = int((game.stop - game.start).total_seconds() / 60)
        lines.append(f"• {game.subtitle or game.title}")
        lines.append(f"  {local.strftime('%-I:%M %p')}  ·  {duration} min  ·  {', '.join(ch_parts)}")
        lines.append("")

    return title_str, "\n".join(lines).rstrip()


class BaseNotifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str, thumb_url: str = "") -> None: ...
