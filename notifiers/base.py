"""Base notifier interface and shared message formatting."""

from abc import ABC, abstractmethod
from datetime import timezone

from epg_scanner import Programme
from matcher import Subscription


def format_message(prog: Programme, sub: Subscription) -> tuple[str, str]:
    """Return (title, body) for the notification."""
    local_start = prog.start.astimezone()  # local system timezone
    time_str = local_start.strftime("%a %b %-d  %-I:%M %p")
    duration_min = int((prog.stop - prog.start).total_seconds() / 60)

    title = f"[{sub.label}] {prog.title}"
    lines = [
        f"Channel : {prog.channel_name}",
        f"Time    : {time_str}  ({duration_min} min)",
    ]
    if prog.description:
        lines.append(f"\n{prog.description}")

    return title, "\n".join(lines)


class BaseNotifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str) -> None: ...
