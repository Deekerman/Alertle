"""Base notifier interface and shared message formatting."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matcher import GroupedMatch

DEFAULT_TITLE_TEMPLATE = "[{rule}] {title}"
DEFAULT_BODY_TEMPLATE = "Time    : {time}  ({duration} min)\nChannel : {channels}\n{description}"

_AVAILABLE_VARS = (
    "{rule}", "{title}", "{subtitle}", "{time}", "{date}",
    "{channels}", "{duration}", "{description}",
)


def build_preview_vars(g: "GroupedMatch", show_channel_nums: bool = False) -> dict:
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
        "channels": ", ".join(ch_parts),
        "rule": g.subscription.label,
        "duration": str(int((g.stop - g.start).total_seconds() / 60)),
        "description": g.description or "",
    }


def render_template(template: str, vars_: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: vars_.get(m.group(1), m.group(0)), template)


def format_grouped_message(
    g: "GroupedMatch",
    title_tpl: str = DEFAULT_TITLE_TEMPLATE,
    body_tpl: str = DEFAULT_BODY_TEMPLATE,
    show_channel_nums: bool = False,
) -> tuple[str, str]:
    vars_ = build_preview_vars(g, show_channel_nums=show_channel_nums)
    return render_template(title_tpl, vars_), render_template(body_tpl, vars_).rstrip()


class BaseNotifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str) -> None: ...
