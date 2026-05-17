"""Base notifier interface and shared message formatting."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matcher import GroupedMatch


def format_grouped_message(g: "GroupedMatch") -> tuple[str, str]:
    """Return (notification_title, body) for a grouped match."""
    local_start = g.start.astimezone()
    time_str = local_start.strftime("%a %b %-d  %-I:%M %p")
    duration_min = int((g.stop - g.start).total_seconds() / 60)

    notif_title = f"[{g.subscription.label}] {g.title}"

    ch_parts: list[str] = []
    for num, name in g.channels:
        ch_parts.append(f"{name} ({num})" if num else name)

    lines = [
        f"Time    : {time_str}  ({duration_min} min)",
        f"Channel : {', '.join(ch_parts)}",
    ]
    if g.description:
        lines.append(f"\n{g.description}")

    return notif_title, "\n".join(lines)


class BaseNotifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str) -> None: ...
